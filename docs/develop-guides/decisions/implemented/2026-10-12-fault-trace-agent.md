# 故障溯源智能体与 YOLO 图片识别接口

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/fault_trace/tool.py

> 现状：2026-10-27 起运行时使用 TuGraph `kg3` + Redis 实时判据 + 五段式报告，`kg`、`kg2` 已废弃；当前机制见[故障溯源](../../../mechanisms/fault-trace.md)；本文保留首次交付的决策背景。`POST /api/fault/images` 与 `GET /api/fault/health` 均已移除，上传图片由故障溯源智能体从会话状态读取并调用 YOLO。

## 问题

`需求宣讲V0.1.0.0.xlsx`「故障原因溯源」与 `srs/原型V0.1.0.0.html` 模块 `fault` 定义了故障溯源需求：用户以文字/语音/图像描述故障现象，系统经图片视觉识别（YOLO）与统一语义表示层融合后，识别涉及设备与故障现象，沿知识图谱因果链（`has_phenomenon`/`has_cause`/`has_trigger_condition`/`has_measure`）匹配可能原因，按规则与证据定位原因，沿 `electrically_connected_to` 做上下游拓扑追溯，最终生成五段式溯源报告（故障摘要、推理过程、原因定位、影响范围、处置建议）。SRS 还要求图像不可单独诊断、设备名必须具体、信息不完整时按方案二（多候选引导选择）/方案三（反问澄清）继续对话。

当前平台只有针对电力规范检索的 `search_power_spec` 智能体，没有故障溯源能力：没有受理前端图片并调用 YOLO 服务的接口，没有故障溯源 Agent（技能/预设/工具/发布门），知识图谱中也没有故障模式、原因、处置措施节点与电工拓扑关系数据。

## 决策

按电力规范检索智能体的同款装配路径新增「故障溯源智能体」，分四条链路：

1. **图片识别链路**：新增 `POST /api/fault/images`，受理前端上传的故障图片（`image/*`、10MB 上限），转发到 YOLO 服务 `POST {YOLO_FAULT_BASE_URL}/api/fault/detect`（管理员经配置项 `fault_image_opts` 配置），把 `fault`/`fault_type`/`fault_name` 返回给前端；另增 `GET /api/fault/health` 透传 YOLO 健康状态供前端诊断。失败（不可达、非 200、响应不合法）结构化投影为 `YoloServiceError`，**不把失败伪装成“未识别到故障”**。
2. **意图链路**：新增故障溯源工具 `trace_fault`（`yuxi/knowledge/fault_trace/`），接收用户问题与图片识别结果，复用电力规范检索的模型意图识别 + 确认卡片 interrupt 模式；实现 SRS 输入约束（图像不可单独诊断、设备名必须具体）与方案二/方案三反问话术；语音输入复用既有 `/api/speech/transcriptions` ASR 管线。
3. **图谱溯源链路**：故障图谱数据由人工录入到**独立 TuGraph `kg` 图**（`bolt://<tugraph-host>:7688`，图名 `kg`，独立于 Yuxi 自管的 `default` 图；管理员可经 `fault_trace_opts` 配置连接）。新增专用适配器（当时位于 `fault_trace/graph_kg.py`，现为 `yuxi/agents/toolkits/tugraph.py`）直连该图：设备实例按 名称/显示名/name 匹配，沿中文关系 `存在故障模式`/`表现为`/`起因`/`处理`/`诱发条件` 取因果链、沿 `电连接` 取电气拓扑，映射为内部英文类型（`has_fault_mode`/`has_phenomenon`/`has_cause`/`has_measure`/`has_trigger_condition`/`electrically_connected_to`）的 `FaultGraphEvidence`；装配逻辑只消费内部类型。`fault_trace/seed.py` 提供中文 schema 的幂等导入（节点按主键 MERGE、边按两端主键 MERGE，重复导入不产生重复记录），已用 1号主变冷却系统示例在真实 `kg` 图验证（13 节点/12 边，导入两次不变）。
4. **发布链路**：工具返回五段式报告契约（含 `answer_markdown`）；`chat_service` 新增 `_is_fault_trace_context`/`_resolve_fault_trace_output` 发布门，用工具产出的卡片覆盖模型最终消息（与 `search_power_spec` 同款，避免模型改写报告结构）；报告装配用单模型 + 代码前置校验门（实测值、设备名、原因、措施必须来自图谱证据，不允许模型自造编号/数值）。

前端范围：团队自研 `new-web` 负责故障溯源页面与图片上传 UI，本变更只负责后端接口与 SSE/卡片数据契约，不改 Yuxi `web/` 前端。

## 替代方案

- 把图片直接以 `multimodal_image` 消息投给主模型做图像理解：本地主模型非 VLM（`ImageInputCompatibilityMiddleware` 已对图像走 OCR 兜底可证），无法满足 SRS 的 YOLO 视觉识别路径，因此图片支路采用“YOLO 类别 + 用户文字描述”融合，多模态大模型语义理解列为后续增强。
- 故障数据写入 Yuxi 自管图谱（`write_chunk_graph` + kb_id 命名空间，经 `MilvusGraphService` 读取）：需要把故障数据混入文档抽取图谱或另建知识库，且与人工录入的 `kg` 台账图分叉；实际选择是直连外部 `kg` 图（用户已录入台账/规程数据），用专用适配器隔离双语 schema 差异。
- 在 `GraphStore` 协议上新增有向关系遍历方法：两颗后端（Neo4j/TuGraph）都要实现且扩协议维护面；V1 用现有 `query_nodes` 子图 + 客户端方向/类型重组即可满足最多 3 跳的因果链与拓扑，判断不足再扩展。
- 报告装配用 organizer/reviewer 子智能体拆分（电力规范检索路径）：故障报告天然结构化、证据规模小，V1 用单模型 + 代码门更快收敛且成本低；子智能体拆分留待 V1 效果验证后按需引入。
- 图片接口后端保存图片与识别结果，工具按 run 取用：引入无必要的服务端状态与身份管理；V1 让接口直接返回结果、前端把结果拼入下一条消息，图片同时走既有 attachments 流留痕。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| `/api/fault/images` 受理 `image/*` 图片并返回 YOLO 识别结果 | 只调 mock、不透传真实 multipart，或把 YOLO 失败当作“未识别到故障” | `yolo_service.py`、`fault_router.py`（已移除，改用智能体工具路径） | 真实 HTTP integration：进程内桩上游 + 真实上传图片后回读响应 JSON | 上游返回 500/非 JSON/字段缺失时返回结构化错误（502/400）且数据库无业务状态写入 | Passed |
| YOLO 客户端按接口文档解析 `fault/fault_type/fault_name` 且“识别到故障但缺类别”拒绝 | 把未识别到故障误判为有类别，或反之 | `yolo_service.py` | unit：`httpx_mock` 覆盖 6 类推类与无故障分支 | `fault:true` 缺 `fault_name`、`fault:false` 却带类别、响应非 JSON 均抛 `YoloServiceError` | Passed |
| 未配置 YOLO 服务时给出可解释文案且不落库 | 未配置时静默返回“未识别到故障” | `yolo_service.py` | unit：`fault_image_opts` 无 `server_url` 时抛配置错误 | 配置缺失用例断言返回 `错误 / 未配置` 文案而不返回空结果 | Passed |
| 故障图谱数据经 seed 幂等导入外部 `kg` 图（中文 schema） | 重复导入产生重复节点/边，或设备未复用台账节点 | `fault_trace/seed.py` | 真实 TuGraph 回读：同一 seed 导入两次后节点/边计数不变；台账已存在设备按 id 复用 | 同名节点/同两端关系重复导入只能有一条记录；不存在的设备才新建 | Passed |
| `trace_fault` 直连 `kg` 图组装五段式报告且代码门拒绝自造内容 | 模型输出不在证据中的设备/原因/措施/数值仍被展示；kg 连接失败伪装成“未检索到” | `yuxi/agents/toolkits/tugraph.py`、`answer.py`、`chat_service._resolve_fault_trace_output` | 真实 `kg` 图全链路：意图→证据（13 节点/12 边）→报告（含上下游拓扑）；发布门取工具 JSON 覆盖最终消息 | 负向：润色含图谱外名词被允许词门拒绝；kg 连接失败返回“图谱暂时不可用”文案并与“未配置”区分 | Passed |
| 缺设备名/仅图片时触发 SRS 方案二/三澄清话术 | 直接跳过澄清生成报告 | `fault_trace/intent.py`、`tool.py` | unit：意图契约 + interrupt 确认重放 | 无设备名、仅图像无文字、多候选设备三类输入均进入澄清而非出报告 | Inspected |
| 图片识别结果与用户问题共同参与意图识别 | 图片类别未进入模型输入 | `fault_trace/workflow.py` | unit：从会话上传图片识别后，意图调用载荷包含类别文本 | 去掉上传图片时意图断言变红 | Passed |

## 后果

故障图谱数据完全依赖人工录入质量：节点/关系缺失、方向录反会直接表现为溯源结果错误。导入脚本只保证幂等写入与基本的节点/关系存在性校验，语义正确性靠人工维护与真实图谱回读验证，不自动推断缺失数据。`kg` 图为独立中文 schema（与 Yuxi `default` 图的 Chunk/Entity 模型不同），适配器已隔离差异；TuGraph 只读查询限制（单 MATCH、中文标签注册后才可 pattern 匹配）已在适配器与导入器中处理。

YOLO 服务是本机自研服务（接口文档 `srs/YOLO设备图片故障类型识别服务 · 接口文档V1.0.0.pdf`，实测契约正确）；依赖外部可选服务，验证环境无该服务时记录 `Not run`，不把降级文案当作识别通过。拓扑追溯当前为静态图查询（上游/下游最多 3 跳），动态实时数据评估属 SRS 低优先级项，V1 不强做。