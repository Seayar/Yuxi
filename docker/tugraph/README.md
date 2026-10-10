# 在 64K 页 aarch64 主机上运行 TuGraph

## 问题

官方 arm64 镜像（`tugraph/tugraph-runtime-centos7`）在**非 4K 页内核**（例如 Kunpeng /
统信 UOS，`getconf PAGESIZE` = 65536）上启动即崩溃：

```
<jemalloc>: Unsupported system page size      （重复多次）
terminate called without an active exception
```

根因：jemalloc 在**编译期**通过 `configure` 探测并固化页大小（`LG_PAGE`）。官方 arm64
二进制在 4K 页主机上构建，`LG_PAGE=12`；在 64K 页内核上运行时校验失败并 `abort()`。
jemalloc 被**静态链接**进 `lgraph_server`（`malloc/free/calloc/realloc/posix_memalign`
都是可执行文件内的 `T` 符号），因此 `LD_PRELOAD` / `MALLOC_CONF` 都无法绕过。

上游跟踪：<https://github.com/TuGraph-family/tugraph-db/issues/477>（官方答复：在目标机重编
jemalloc 后重新链接 TuGraph）。

## 解决

在目标机（64K 页）重编 jemalloc，再用它重新链接 TuGraph，产出可直接运行的镜像。

- 官方 4.1.0 arm64 镜像的 jemalloc 是 4K 版，在本机（64K 页）上启动即崩 → 用它重编即可修复，
  但 **4.1.0 的 Bolt 不支持查询参数**，适配器只能退化内联字面量。
- 推荐直接编 **TuGraph 4.5.2**：Bolt 支持参数化 Cypher，适配器模板（参数化单属性 MERGE +
  逗号 SET）可直接使用。4.5.x 原文只随仓库源码提供 amd64 编译镜像，arm64 编译镜像 1.2.7
  缺 vsag/faiss（4.5.x 强依赖），需在镜像内自行构建 OpenBLAS → faiss → vsag（boost/其他
  依赖走 gh-proxy，OpenBLAS 用本机 64K 静态库）。构建脚本已把这些固化为可断点重跑的整链：
  执行 `bash docker/tugraph/build-64k-aarch64.sh` 会产出自带 vsag/faiss 的
  **`tugraph/tugraph-runtime-centos7:4.5.2-64k`**（已在 64K 页主机实测运行）。
  让 Compose 使用该镜像时设置 `TUGRAPH_VERSION=4.5.2-64k`（`TUGRAPH_IMAGE` 只放仓库名，
  tag 由 `TUGRAPH_VERSION` 拼接）。

本目录构建脚本逻辑：

- jemalloc 用官方 **release 包**（自带 `configure`），在本机 `configure` 会探测到
  `LG_PAGE=16`（64K）。
- 复用官方 arm64 **编译镜像**（含大部分静态库/头文件），仅在其内补编 OpenBLAS/faiss/vsag
  与 TuGraph C++，无需从零构建全部第三方依赖。
- `cmake` 关闭 `-Werror`（`-DENABLE_WALL=OFF`）。
- 4.5.2 的 `lgraph_server` 最终链接需补 `gfortran`（静态 OpenBLAS）、braft/brpc（HA），
  并为编译镜像自带的旧 `libbraft.a` 缺失的 HA 首快照 gflags 提供一个最小符号桩
  （`braft_flag_stubs.cpp`，HA 默认关闭）。

验证（本机 64K 页实测）：`4.5.2-64k` 镜像启动后日志出现 `bolt server run` / `Server
started.`，Bolt 与 REST 均在监听。

## 与 Yuxi 适配器相关的 TuGraph 现实约束（实测 4.5.2 Bolt）

`TugraphGraphStore` 按以下**真机验证子集**编写：

- ✅ 支持查询参数 `$param`（MERGE/SET/WHERE/IN/边属性）；`LIMIT $param` 可用。
- ✅ 建标签 `CALL db.createVertexLabel(label, primary, field, 'STRING', optional, ...)`：
  字段类型必须是**带引号的字符串**（裸标识符在 4.5.2 Bolt 会 `visit(...) failed`；
  4.1.0 则相反要裸标识符）。
- ✅ `MERGE (n:L {主键: $id})` + 逗号多属性 `SET n.a=$a, n.b=$b`（单属性主键图）。
- ❌ 多属性 MERGE 图 `MERGE (n:L {a:.., b:..})`（`pattern_graph_maker` 失败）→ 用单属性
  主键 + `SET` 其余字段（`MENTIONS` 用 `mention_id` 组合键）。
- ❌ 只读查询单条语句最多一个 `MATCH`（“Not support more than one match clause”）→ 查询
  拆成多条单 MATCH 语句；写入语句不受此限（`RELATION` 模板即两个 `MATCH` + `MERGE`）。
- ❌ 模式谓词 `NOT (e)<-[:MENTIONS]-()`（`bad any_cast`）→ 孤儿实体/三元组由 service 基于
  PostgreSQL 计算后按 `entity_id IN` / `triple_id IN` 精确删除。
- ✅ `toLower`/`coalesce`、`id(n)`/`id(n) IN`、`euid(r)`/`euid(r) IN` 可用。
- ⚠️ `nodes(p)`/`relationships(p)` 返回**列表的字符串表示**（如 `"[0,1]"`），需解析后
  二次按 `id(n) IN`/`euid(r) IN` 抓取节点/边属性。
- ⚠️ 字符串字面量无转义机制，参数化可避免内联文本的注入风险。

升级 TuGraph 大版本后，需在本机按上述子集清单重新验证 Bolt 能力（本仓库不附带探针脚本），
再决定是否调整适配器。
