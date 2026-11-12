# 配置系统

Yuxi 的配置分成两类：启动进程时读取的环境变量，以及运行中由管理员在页面维护的系统配置。区分这两类，可以判断修改后需要保存、清缓存还是重启服务。

## 配置来源和优先级

对支持运行时修改的字段，生效顺序是：

```text
代码默认值 < 环境变量 < PostgreSQL 中保存的系统配置
```

数据库中保存的非空值优先于环境变量；布尔值和列表即使是 `false` 或空列表，也会被视为管理员明确保存的值。没有数据库值时，系统才读取对应环境变量或代码默认值。

启动期环境变量由 Docker Compose 注入 API、worker、provisioner 和依赖服务。它们决定服务地址、密钥、存储位置和沙盒承载方式。修改环境变量后需要重新创建读取它的容器；只执行 `docker compose restart` 不会更新容器环境：

```bash
docker compose up -d --force-recreate api worker
```

如果改的是 provisioner 变量，把服务名换成 `sandbox-provisioner`；生产环境还要带上 `--env-file .env.prod -f docker-compose.prod.yml`。

## 管理员系统配置

管理员在“设置 → 基本设置”中修改系统配置。当前配置项由 [`options.py`](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/config/options.py) 定义，包含：

- 默认对话模型、快速响应模型、嵌入模型和重排模型；
- 默认 OCR 解析引擎。

系统配置保存到 PostgreSQL 的 `config_options` 表。API 和 worker 通过 Redis 保存短期缓存；保存成功后缓存会失效，下一次读取从数据库取得新值。Redis 不可用时，读取路径回源 PostgreSQL，不把缓存当作最终事实。

运行中的已初始化组件不保证热更新。例如模型供应商使用的 API Key 环境变量、OCR 服务地址、沙盒连接和数据库地址变化后，应重新创建实际读取这些变量的 API、worker 或 provisioner。

## 模型和 OCR

- 模型供应商、聊天模型、嵌入模型和重排模型的配置见[模型配置](../intro/model-config.md)。
- OCR 引擎和各服务凭证的配置见[文档处理与 OCR](./document-processing.md)。
- 语音识别的服务地址与模型见[语音识别](./speech-interaction.md)。
- 沙盒应用层与 provisioner 的配置见[沙盒配置与运维](../agents/sandbox-architecture.md)。

## Agent 并发容量

Compose 默认按单 worker、100 个同时运行的 AgentRun 配置。执行槽、API/worker Redis 和 PostgreSQL 池、LangGraph checkpoint 池、PostgreSQL 服务端上限、Sandbox 地址池与清理并发必须联动核算，不能只扩大其中一个值。

推荐值、1/10/20/50/100 并发的延迟和资源实测、取消 key 轮询的负载模型及完整验证命令见 [Agent 并发容量](./agent-concurrency-capacity.md)。所有可覆盖变量仍集中列在仓库根 `.env.template`，不另外维护重复配置清单。

## 故障溯源

故障溯源的运行参数分属两类：图连接与图片识别服务走管理员系统配置（`fault_trace_opts`、`fault_image_opts`），环境变量只在系统配置留空时兜底；实时值 Redis 只读环境变量。所有可覆盖变量的默认值集中列在仓库根 `.env.template` 的 `fault trace` 分区。

| 变量 | 读取者 | 缺省与条件 |
|---|---|---|
| `FAULT_TRACE_GRAPH_URI`、`FAULT_TRACE_GRAPH`、`FAULT_TRACE_GRAPH_USERNAME`、`FAULT_TRACE_GRAPH_PASSWORD` | `fault_trace_opts` → `TuGraphClient` | 系统配置优先；`FAULT_TRACE_GRAPH` 只用于故障溯源，缺省回退 `kg3`；地址、图名、用户名、密码任一为空时工具返回“故障溯源知识库未配置” |
| `FAULT_TRACE_REDIS_URL` | `fault_trace/realtime.py` | 为空时回退应用 `REDIS_URL`；两者都为空时属性状态为 `not_configured` |
| `FAULT_TRACE_REDIS_DB` | `fault_trace/realtime.py` | 缺省 `0`；非整数按 `0` 处理 |
| `FAULT_TRACE_REDIS_PREFIX` | `fault_trace/realtime.py` | 缺省空串；key 形如 `{prefix}{台账ID}:{属性}` |
| `YOLO_FAULT_BASE_URL` | `fault_image_opts` → `yolo_service` | 系统配置优先；两者都为空时识别返回“故障识别服务未配置” |
| `TUGRAPH_CONNECT_TIMEOUT`、`TUGRAPH_QUERY_TIMEOUT` | `agents/toolkits/tugraph.py` | 连接缺省 5 秒、查询事务超时缺省 10 秒；查询超时以 bolt 事务配置下发，是否执行取决于 TuGraph 对 bolt 事务配置的支持 |

`FAULT_TRACE_REDIS_URL` 与 `YOLO_FAULT_BASE_URL` 在 API 与 worker 的容器环境里都必须存在，否则容器读不到宿主机的同名变量。

## 处置方案生成

处置方案读 TuGraph `kg4` 的 `设备实例 / 设备类型 / 检修作业场景 / 检修步骤 / 安全措施 / 资源需求 / 人员需求`，连接参数与故障溯源共用 `fault_trace_opts` 的地址与凭据，图名单独配置。分支优先读故障溯源 Redis 的 `{台账ID}:{分支判定维度}`；Redis 未配置、无 key 或值非法时工具降级为让用户确认分支。工器具与人员 JSON 从 MinIO 读取，读取失败只在方案“数据说明”中标注，不阻断图谱内容。

| 变量 | 读取者 | 缺省与条件 |
|---|---|---|
| `MAINTENANCE_PLAN_GRAPH` | `fault_trace_opts` 的 `maintenance_plan_graph` 字段 → `maintenance_plan/tool.py` | 系统配置优先；缺省回退 `kg4`；地址、用户名、密码任一为空时工具返回“处置方案知识库未配置” |
| `FAULT_TRACE_REDIS_URL`、`FAULT_TRACE_REDIS_DB`、`FAULT_TRACE_REDIS_PREFIX` | `maintenance_plan/branch.py` 复用 `fault_trace/realtime.py` | key 形如 `{prefix}{台账ID}:{分支判定维度}`；value 必须精确等于场景的 `分支1条件` 或 `分支2条件`；未配置/缺失/值非法时降级为用户确认，不默认分支 |
| `MAINTENANCE_PLAN_TOOLS_BUCKET`、`MAINTENANCE_PLAN_TOOLS_OBJECT` | `maintenance_plan/sources.py` | 缺省 `maintenance-plan` / `tools.json`（与 `fault-assets` 平级的 bucket）；结构为 `{"items":[{"name":...,"quantity":...}]}`，`quantity>0` 判在库；读取失败时资源清单不标注库存 |
| `MAINTENANCE_PLAN_PERSONNEL_BUCKET`、`MAINTENANCE_PLAN_PERSONNEL_OBJECT` | `maintenance_plan/sources.py` | 缺省 `maintenance-plan` / `personnel.json`；结构为 `{"班组":[{"班组名称":...,"人员":[...]}]}`；读取失败时只输出岗位与人数；人员池不足时返回固定“人员数据不足”文案 |

kg4 的 `设备实例.台账ID列表` 为 `补充数据` 等占位时，`sources.resolve_ledger_ids` 会按设备名称与层级回查 `fault-assets/ledger/devices.json` 补全台账 ID；设备缺少 `属于` 边时，只对台账分类精确命中的线路/间隔/PT 做有限类型回退。两类回退都只补检索键，不改变图谱节点或 Redis 数据。

工器具与人员数据由数据团队维护在 `maintenance-plan` bucket；它与故障溯源的 `fault-assets` 平级，代码上也分别由 `maintenance_plan` / `fault_trace` 两个平级包拥有。当前 `tools.json` 为 304 条工器具库存、`personnel.json` 为 8 个班组 106 人。读取按进程内 300 秒缓存，更新对象后无需重启，最多 5 分钟后生效。

处置方案与故障溯源共用同一 TuGraph 服务与账号，但图名不同；如果部署把两者拆到不同实例，需要先扩展 `fault_trace_opts` 的连接字段，不能只改图名。

## 设备预测维护

设备预测维护的图谱只读 TuGraph `kg5`。设备识别用 `设备实例`（名称、层级路径、电压等级、id、台账ID列表，经 `属于` 关联 `设备类型`）；维护计划建议按预测故障类型匹配到 `设备类型 -[具有现象]-> 故障现象`，从现象取 `所需资源` 与 `注意事项`。设备按名称与位置线索召回；发送给预测服务的设备 ID 先取设备节点的 `台账ID列表`（跳过 `补充数据` 等占位值），节点没有可用 ID 时按**精确名称 + 层级祖先**回查三区台账 `fault-assets/ledger/devices.json`（与故障溯源共用同一对象，只接受精确同名行）。健康评估与故障预测分别调用两个外部 HTTP 服务，服务地址走管理员系统配置 `device_prediction_opts`，环境变量只在配置项字段留空时兜底。

| 变量 | 读取者 | 缺省与条件 |
|---|---|---|
| `DEVICE_PREDICTION_GRAPH` | `fault_trace_opts` 的 `device_prediction_graph` 字段 → `device_prediction/tool.py` | 系统配置优先；缺省回退 `kg5`；连接地址、用户名、密码任一为空时工具返回“设备预测维护知识图谱未配置或不可用” |
| `DEVICE_PREDICTION_BASE_URL` | `device_prediction_opts` 的 `prediction_base_url` → `device_prediction/services.py` | 系统配置优先；系统配置与环境变量都未填时工具返回“故障预测服务未配置”；请求路径 `GET /api/prediction/predict/{deviceId}` |
| `DEVICE_HEALTH_BASE_URL` | `device_prediction_opts` 的 `health_base_url` → `device_prediction/services.py` | 系统配置优先；系统配置与环境变量都未填时工具返回“设备健康评估服务未配置”；请求路径 `GET /api/device-health/{deviceId}` |
| `DEVICE_PREDICTION_INTENT_ENABLED` | `device_prediction/intent.py` | 缺省 `true`；设为 `0`/`false`/`no` 时设备语义解析与状态解读都走确定性路径 |

走管理员系统配置时不需要环境变量；环境变量只在对应配置项字段留空时兜底，已保存的配置不会被环境变量覆盖。`kg5` 不可达或未建图时，图连接失败在设备检索阶段返回“设备预测维护知识图谱未配置或不可用”；图可达但没有匹配设备时返回“未在设备台账中找到该设备”；设备节点没有可用 `台账ID列表` 且三区台账里也没有精确同名行时返回“未找到该设备的台账 ID”。设备类型下没有匹配的故障现象时，报告只标注所需资源与注意事项未匹配。三区台账由运维更新到 MinIO，进程内缓存 5 分钟。字段口径、服务响应与未联调范围见[设备预测维护智能体](../develop-guides/decisions/implemented/2026-11-09-device-prediction-agent.md)。

## 电力规范问答图谱

电力规范检索在意图确认后、常规召回之前查询独立 TuGraph 图 `power_spec_qa`。该图只保存测评集的“问题—答案—依据条款”映射，条款正文仍从当前可见知识库的 `processing_params["power_spec"]["clauses"]` 读取。管理员系统配置项 `power_spec_graph_opts` 拥有连接参数，环境变量只在配置项字段留空时兜底。导入命令、schema 与匹配规则见[电力规范检索](../mechanisms/power-spec-retrieval.md)。

| 变量 | 读取者 | 缺省与条件 |
|---|---|---|
| `POWER_SPEC_GRAPH_URI`、`POWER_SPEC_GRAPH`、`POWER_SPEC_GRAPH_USERNAME`、`POWER_SPEC_GRAPH_PASSWORD` | `power_spec_graph_opts` → `qa_graph.py` | 系统配置优先；图名缺省 `power_spec_qa`；地址、图名、用户名、密码任一为空时运行时跳过图谱召回 |
| `POWER_SPEC_QA_GRAPH_ENABLED` | `qa_graph.py` | 缺省 `true`；设为 `0`/`false`/`no` 时不查图谱 |
| `POWER_SPEC_QA_GRAPH_MATCH_THRESHOLD` | `qa_graph.py` | 缺省 `0.85`；精确命中优先，模糊命中还必须通过标准编号、电压等级与否定词 guard |
| `TUGRAPH_CONNECT_TIMEOUT`、`TUGRAPH_QUERY_TIMEOUT` | `agents/toolkits/tugraph.py` | 与故障溯源共用；连接缺省 5 秒、查询事务超时缺省 10 秒 |

图谱导入是运维动作，不在 API 或 worker 启动时执行。开发环境在 `api` 服务挂载只读 `./srs:/app/srs:ro`，导入命令见[电力规范检索](../mechanisms/power-spec-retrieval.md#测评图谱导入与调试)；生产环境由运维提供可访问的测评集路径，并只授予 TuGraph 管理员建图与写图权限。

## 修改后的确认方式

修改系统配置后，重新打开配置页面确认保存值，并用一次真实请求确认行为。只看到 HTTP 200 或提示“保存成功”不能证明模型、OCR 或检索链路已经可用。

- 模型：在供应商页面执行连接测试，再发起一次真实对话。
- OCR：在 OCR 配置中查看健康状态，再解析一份测试文件。
- 知识库：回到知识库页面检查文件状态和检索结果。
- 沙盒：检查 provisioner `/health`，再执行一次文件读写或命令操作。

## 历史配置

旧版 `base.toml` 和 `SAVE_DIR` 不属于当前运行时配置来源。受支持的历史数据由一次性 storage migrator 处理；日常部署不要手动把旧文件复制回运行目录。升级步骤见[生产部署指南](./deployment.md)。
