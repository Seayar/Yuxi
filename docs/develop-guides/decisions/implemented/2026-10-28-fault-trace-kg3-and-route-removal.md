# 故障图谱迁移到 kg3 并移除公开故障路由

状态：implemented
类型：architecture
Owner：backend/package/yuxi/knowledge/fault_trace/graph_kg3.py

图谱读取在 `graph_kg3.py`，设备图片与台账在 `fault_trace/images.py`、`fault_trace/ledger.py`，实时值语义在 `fault_trace/realtime.py`，编排在 `workflow.py`，路由注册在 `server/routers/__init__.py`。本记录取代 [故障溯源智能体](./2026-10-12-fault-trace-agent.md) 中关于旧 `kg` 图谱与 `POST /api/fault/images`、`GET /api/fault/health` 的路由描述，并与 [处置方案智能体](./2026-10-16-maintenance-plan-agent.md) 共用 kg3 的预检与写入口径。

## 问题

故障溯源早期实现依赖旧 `kg` 图谱与一个独立的 HTTP 入口（`POST /api/fault/images`、`GET /api/fault/health`）：

- 旧适配器 `graph_kg.py` 读取的标签与关系名已被 kg3 数据取代，设备类型与故障模式挂载点不同。
- 公开路由需要单独的鉴权、参数校验与测试，但它做的只是把图片交给 YOLO 再返回识别结果；智能体自己在同一次 Run 里就能调用同一服务。
- 图片在不同智能体里有两套处理路径（模型直读、服务端识别），责任边界不清晰。

## 决策

- 图谱读取只走 `graph_kg3.py`：设备、设备类型、故障模式、现象、原因、触发条件、措施、后果与电气拓扑均按 kg3 schema 查询，标签与关系名取自 `contracts` 白名单，变量参数化。
- 处置方案的 seed 默认写入 kg3，且预检缺少 `kg_ref.json` 时判为未通过，不再静默跳过班组管辖区域检查。
- 删除 `backend/server/routers/fault_router.py` 与对应集成测试；仓库内不再注册 `/api/fault/*`，`server/routers` 不感知故障溯源。
- 图片由智能体配置决定：`image_input_mode="server"` 时平台把上传图片保留在会话状态，`AgentImageInputMiddleware` 不把图片交给模型，工具从运行状态读取字节并调用 YOLO；台账图片按设备名称从 MinIO 取图。
- 工具不再接受调用方传入的图片识别结果，图片结论只能来自服务端识别；实时值读取区分 `missing`、`invalid`、`unavailable`、`not_configured`，`nan`/`inf` 判为非法值。

## 替代方案

- **保留 `/api/fault/images` 供前端直接识别**：需要再维护一套鉴权、限流与错误语义，且与智能体链路重复，已删除。
- **保留 `graph_kg.py` 兼容旧图谱**：两套标签体系需要长期同步，且没有仍在读 `kg` 的部署证据，已删除。
- **图片交给模型自己读**：大图与多图会挤占上下文，且模型可能转述图片内容作为结论，已放弃。

## 后果

- 故障溯源只依赖 kg3 与 TuGraph 连接配置；管理员在系统配置里填写连接，缺失时返回固定文案而不是静默降级。
- 前端不再有故障图片的独立接口，图片随聊天请求上传，由 Run 内的工具消费。
- 设备类型解析要求目标设备或台账 ID 至少有一个非空标识；空值不参与等值匹配，避免命中其他设备的类型。
- 实时值告警只在服务不可用（`unavailable`/`not_configured`）时触发，缺 key 只表示该设备缺这项数据。

## 验证

- 图谱适配与排序：`backend/test/unit/knowledge/test_fault_trace_graph_kg3.py`（含空台账 ID 与他设备“属于”边的负向用例）。
- 编排与发布门：`backend/test/unit/knowledge/test_fault_trace_tool.py`、`backend/test/unit/knowledge/test_fault_trace_gate.py`。
- 实时值语义：`backend/test/unit/knowledge/test_fault_trace_realtime.py`、`backend/test/unit/knowledge/test_fault_trace_rules.py`。
- 路由删除：`backend/server/routers` 不再注册故障路由，`backend/test/integration/api` 不再包含故障路由用例；仓库内 `/api/fault` 只剩 YOLO 上游服务自身的 `DETECT_PATH`（`backend/package/yuxi/services/yolo_service.py`），它不是本仓库暴露的路由。
- 命令：`docker compose exec api uv run --group test pytest test/unit -m "not slow"`；真实链路另需一套 kg3 与 Redis 实例，见[故障溯源机制](../../../mechanisms/fault-trace.md)。
