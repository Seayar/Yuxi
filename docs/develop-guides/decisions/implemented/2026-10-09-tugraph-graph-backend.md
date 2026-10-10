# 增加 TuGraph 图数据库后端适配

状态：implemented
类型：architecture
Owner：backend/package/yuxi/knowledge/graphs/graph_stores/tugraph_store.py

## 问题

知识图谱此前只写 Neo4j，且把知识库标识编码为节点标签（`:Chunk:MilvusKB:`kb_id``）。
目标客户指定使用 TuGraph。TuGraph 虽兼容 Bolt 协议（可复用 Neo4j 驱动），但有两个
硬约束：每个点/边只能有一个标签，且使用强 Schema（写入前必须存在标签定义）。因此
既有 Cypher 无法直接复用。

## 决策

1. 新增 `GRAPH_BACKEND` 环境变量选择后端，默认 `neo4j`，未识别取值显式失败。
   Neo4j 路径与既有数据保持不变，无需迁移。
2. 新增 `yuxi.storage.graph_connection`：按后端选择连接管理器（`Neo4jConnectionManager`
   / `TugraphConnectionManager`）并共享 Bolt 读写、标签校验与生命周期。原
   `yuxi.storage.neo4j` 包被替代删除。
3. 新增 `yuxi.knowledge.graphs.graph_stores`，把后端专属 Cypher 收敛到 `GraphStore`
   实现：`Neo4jGraphStore`（沿用多标签隔离）与 `TugraphGraphStore`（`Chunk`/`Entity`
   单标签 + `kb_id` 属性隔离，单一 `default` 子图）。`MilvusGraphService` 只保留抽取、
   并发与进度编排并委托 store。
4. `TugraphGraphStore.ensure_ready` 在首次写入前幂等创建 `Chunk`/`Entity` 点标签与
   `MENTIONS`/`RELATION` 边标签。Cypher 面向 **TuGraph 4.5.x Bolt 实测子集**编写：
   建标签字段类型用**带引号字符串**（`'STRING'`，裸标识符会触发 `visit(...) failed`）；
   `MERGE` 属性图只使用**单属性主键**（`MENTIONS` 用 `mention_id` 组合键；多属性图触发
   `pattern_graph_maker` 失败）；**只读查询**单条语句最多一个 `MATCH`（避免“Not support
   more than one match clause”），写入语句（`MATCH` + `MERGE`）不受限；支持查询参数、
   逗号多属性 `SET`、`toLower`/`coalesce`、`id(n) IN`/`euid(r) IN`；`nodes(p)`/
   `relationships(p)` 返回**列表的字符串表示**，二次抓取获得节点/边属性。
5. 孤儿清理以 **PostgreSQL 为准**：`delete_file_graph(orphan_entity_ids, orphan_triple_ids)`
   由 service 层基于真实数据库计算并传入；TuGraph 按 `entity_id IN` / `triple_id IN` 精确
   删除（其不支持模式谓词 `NOT (e)<-[:MENTIONS]-()`）。`RELATION` 边以 `triple_id` 为主键
   单条存储，只有三元组失去全部文件引用时才删边，不按 `file_id` 删除，避免误删其它文件
   仍在引用的关系。
6. Compose 增加 `tugraph` 服务（`profiles: ["tugraph"]`），默认镜像
   `tugraph/tugraph-runtime-centos7:4.5.2`；**在 64K 页主机上需用
   `docker/tugraph/build-64k-aarch64.sh` 自建 `:4.5.2-64k` 并用
   `TUGRAPH_VERSION=4.5.2-64k` 覆盖镜像 tag（`TUGRAPH_IMAGE` 只放仓库名）**（见该 README）。

## 替代方案

- **整体替换 Neo4j**：会废弃既有 Neo4j 图谱数据并移除既有部署能力；"增加适配"的诉求不支持破坏性替换。
- **把 Neo4j 也改为单标签 + `kb_id` 属性**：需要数据迁移，旧标签数据会变成孤儿。
- **用 TuGraph 子图按知识库隔离**：Schema 需按子图重复创建，删库语义更重，收敛成本高于 `kb_id` 过滤。
- **改用 TuGraph RESTful/RPC 客户端**：引入额外依赖且 Bolt 已兼容，收益不足。
- **锁定 4.1.0 / 官方 4.5.x 镜像**：官方 arm64 镜像（4.1.0）在本机 64K 页因 jemalloc
  编译期固化 4K 页而崩溃，且 4.1.0 Bolt 不支持查询参数；官方 arm64 编译镜像仅 1.2.7
  （不含 vsag/faiss）。故在本机自编 4.5.2（保留 vsag/faiss，其它消费方可能仍用 TuGraph
  内置向量索引），并解决 braft 旧库缺 HA 首快照 gflags 的问题（提供最小符号桩，HA 默认关闭）。

## 后果

- 后端与运维新增一个可切换的外部依赖；`.env`、Compose、初始化/导出脚本需同步维护镜像版本。
- 两种后端的关系边粒度不同：Neo4j 每个 chunk 一条 `RELATION` 边，TuGraph 每条三元组一条
  （与 PostgreSQL 三元组模型一致）；`get_stats` 边数口径与文件删除语义已在 store 内按各自
  模型隔离，`get_labels` 两种后端都返回 `Chunk`/`Entity`（业务类型存于属性）。
- 文件图谱删除以 PostgreSQL 已提交的孤儿清单为准：若 PostgreSQL 提交后任一图侧步骤失败，重试
  得到的是空清单，TuGraph 侧可能残留孤儿边/实体，需由图谱 `reset` 收敛（Neo4j 可在图内自愈）；
  这是该设计的已知代价。
- TuGraph 的 Cypher 是 4.5.2 Bolt 真机验证子集，与文档描述的完整 openCypher 有差距；
  升级 TuGraph 大版本需按 `docker/tugraph/README.md` 的约束清单在本机重新验证适配器。
- 生产 Compose 不为 TuGraph 提供默认口令，选用时必须显式设置 `TUGRAPH_PASSWORD`。

## 验证

- Inspected：`docker-compose -f docker-compose.yml config --quiet` 与
  `docker-compose.prod.yml`（`.env.prod` 占位）解析通过；`--profile tugraph config` 能解析出
  `tugraph` 服务与 `GRAPH_BACKEND`/`TUGRAPH_*` 注入，`TUGRAPH_VERSION=4.5.2-64k` 覆盖出合法镜像引用。
- Run（单元）：`test/unit -m "not slow"` → 2444 passed / 54 skipped，含单标签 MERGE、Schema
  并发容错、查询规避限制、`exclude_chunk` 限定 RELATION 遍历、按 `kb_id` + 孤儿清单作用域删除、
  `labels(n)`/`nodes(p)` 列表字符串解析与业务边类型展示的回归断言。
- Run（真实 TuGraph 4.5.2 与真实 Neo4j 对齐）：同一工作负载双后端对比，基础 17 项与扩展
  15 项检查（共享三元组按文件删除、`exclude_chunk` 不穿越 Chunk、边业务类型、节点 labels、
  stats、seed 子图等）全部一致；`test/integration/graphs` 在两个后端下各 3 passed / 1 skipped
  （skip 为另一后端的专属用例；TuGraph 用例断言共享三元组边在删除单个文件后必须保留）。
- Run（4.5.2 能力 probe）：参数化 Cypher、逗号多属性 SET、`toLower`/`coalesce`、`id(n) IN`、
  `euid(r) IN`、`[:RELATION*0..n]` 边类型限定可用；多属性 MERGE、模式谓词、只读查询多 MATCH
  不可用（据此编写适配器）；对已存在标签重复建报 `LabelExist`，store 以重查容错。
- 复现脚本：`docker/tugraph/build-64k-aarch64.sh`（jemalloc→TuGraph 4.5.2 全链路构建）；
  `docker/tugraph/README.md` 记录 64K jemalloc 根因、适配器约束与镜像选择。
