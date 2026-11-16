# 故障溯源

本页解释设备故障图片怎样被识别、故障图谱与实时值怎样进入原因定位，以及一次故障描述怎样变成五段式溯源报告。它面向需要阅读、评审或修改该链路的开发者，重点是每层的 Owner、数据形态、失败语义和可逐步执行的调试入口。

## 适用范围

故障溯源由六条相互独立的链路组成，修改其中一条不会自动修复另外五条。

- **图片识别链路**：故障溯源 Agent 的 `image_input_mode="server"` 由 `AgentImageInputMiddleware` 执行：模型调用前移除图片块，原始图片保留在会话状态里；`trace_fault` 从 `runtime.state` 读取图片并调用 YOLO，模型只接收文字。没有上传图时，`fault_trace/images.py` 按台账设备名称从 MinIO 取图（`fault-assets/device-images/{设备名称}.{jpg|jpeg|png|webp}`）再交给同一个 YOLO 客户端。没有独立的图片上传或健康探针 HTTP 接口。
- **图谱数据链路**：数据团队维护独立的 TuGraph 知识图谱 `kg5`，运行时由 `fault_trace/graph_kg3.py` 只读查询；`kg3` 与 `kg4` 分别保留为故障溯源、处置方案的历史图，默认不连接，管理员显式配置图名时可回指。当前代码不再读取 `故障模式`，旧图里依赖模式层挂现象的数据不会恢复原语义。`seed.py` 仅用于历史 `kg` 数据迁移对照，必须显式指定目标图名，不参与当前运行。
- **台账链路**：`fault_trace/ledger.py` 从 MinIO `fault-assets/ledger/devices.json` 加载三区台账，按设备名与祖先路径找到台账设备 ID，作为实时值 key 前缀。
- **实时值链路**：`fault_trace/realtime.py` 按设备台账 ID 从 Redis（`FAULT_TRACE_REDIS_URL`）只读批量读取属性；缺值/超时映射为 `unknown`。
- **文本生成链路**：`fault_trace/report_text.py` 让模型生成故障现象简述与原因定位推理依据，两段文本都要通过证据门；模型失败或证据门不通过时回退确定性文案。
- **溯源链路**：`fault_trace/workflow.py` 用 `FaultTraceState` 固化解析、因果链、拓扑、报告四步，`tool.py` 只做参数校验、配置、缓存与工作流调用；`chat_service` 发布门用工具产出的 `answer_markdown` 覆盖最终消息。

## 组件与依赖

```mermaid
graph TD
  A[前端] -->|用户消息 + 上传图片(base64)| D[trace_fault<br/>工具入口]
  D --> W[fault_trace/workflow.py<br/>四步状态机]
  W --> C[yolo_service]
  W --> E[fault_trace/intent.py<br/>意图识别与确认]
  W --> F[fault_trace/graph_kg3.py<br/>故障图适配器]
  F --> G[(TuGraph kg5<br/>中文 schema)]
  W --> M[fault_trace/ledger.py<br/>MinIO 三区台账]
  M --> N[(MinIO fault-assets)]
  W --> O[fault_trace/images.py<br/>设备图片集]
  O --> P[(MinIO device-images)]
  O --> C
  W --> Q[fault_trace/report_text.py<br/>摘要/推理依据模型]
  W --> H[fault_trace/realtime.py<br/>Redis 实时值]
  H --> I[(故障实时值 Redis)]
  W --> J[fault_trace/rules.py<br/>表达式求值/原因聚合]
  W --> K[fault_trace/answer.py<br/>五段式报告]
  K --> L[chat_service<br/>发布门]
```

依赖方向只有一条：`tool` 只做参数校验、配置、缓存与四步工作流调用；`workflow` 持有 `FaultTraceState` 和固定控制流；意图、图谱查询、实时值、规则求值与报告装配各自拥有模块，跨模块流动的数据形状统一由 `fault_trace/contracts.py` 拥有。TuGraph 连接生命周期由 `yuxi/agents/toolkits/tugraph.py` 提供，故障溯源与处置方案共用；`fault_trace/graph_kg3.py` 只拥有故障图查询语句与中文节点映射。

## 权限与隔离

工具从 `runtime.context` 取当前用户的运行上下文；没有上下文时直接返回“无法获取当前用户的知识库权限”，不继续查图。故障溯源只读外部图谱与 Redis，不写 PostgreSQL 业务表，也不校验知识库可见性：它消费的是管理员配置的共享图谱与台账，用户权限在会话与 Run 的接入层判定。

发布链路的隔离按 Run 归属执行：`chat_service` 只读本轮 `tool_call_id` 对应的工具结果，历史轮次卡片不跨轮；报告只在 `answer_source="fault_trace"` 且 `answer_verified is True` 时发布，澄清载荷发布工具给出的固定问句，模型文本不能替换这两类内容。设备图片与台账都从 `fault-assets` 桶按固定前缀读取，用户输入只作为对象名前缀参与匹配，不拼接宿主机路径。

## 配置边界

图谱连接、图片识别服务地址与实时值 Redis 的变量、缺省值与回退顺序见[配置参考](../advanced/configuration.md#故障溯源)。运行期的关键约束：系统配置优先于同名环境变量；图谱地址、图名、用户名或密码任一缺失时工具返回固定文案，不静默换图；Redis 地址缺失记为 `not_configured` 而不是 `missing`。`FAULT_TRACE_INTENT_ENABLED` 只决定意图识别是否调用模型（关闭后走规则解析），`FAULT_TRACE_INTENT_TIMEOUT` 与 `FAULT_TRACE_INTENT_MAX_TOKENS` 作用于意图识别与两段文本生成的模型调用，都不改变控制流。

智能体侧只有一个开关影响链路形状：预设里的 `image_input_mode="server"`。改成 `"model"` 会把图片交给模型，同时 `trace_fault` 仍只读会话状态里的上传图片，两条链路不共享识别结果。

## 图片识别链路

图片不经过独立的上传接口。故障溯源 Agent 声明 `image_input_mode="server"`，`AgentImageInputMiddleware` 在每次模型调用前把图片块替换为占位文本，原始图片仍保留在 LangGraph 会话状态；`trace_fault` 先从 `runtime.state` 最近一条用户消息读取 base64 调用 `yuxi/services/yolo_service.py`，识别结果再参与意图解析；会话中没有上传图片时，`fault_trace/images.py` 按台账设备名称从 MinIO 取图。工具不接受调用方传入的识别结果，图片结论只来自服务端识别。

`yolo_service` 识别到故障时返回 `fault_type`/`fault_name`，未识别到返回空类别；服务不可达、非 200、响应不合法一律抛 `YoloServiceError`，工具降级到文字现象并在报告 `meta.warnings` 记录。图片本身不可单独诊断：文字与图片都没有现象时返回 `image_not_found` 澄清。

## 图谱数据链路

连接由管理员配置 `fault_trace_opts`（`server_url`/`graph`/`username`/`password`），运行时只读；当前只使用 `kg5`，`kg3`/`kg4` 为历史图。kg5 不再包含 `故障模式` 与 `故障后果`；适配器只按 `设备类型 -[具有现象]-> 故障现象 -[具有原因]-> 故障原因` 读取。现象与设备类型是多对多：一个设备类型可以有多个现象，同一现象名也可以出现在不同设备类型上；kg5 为每个设备类型建立自己的现象节点（即使名称相同），原因挂在各自现象节点下，适配器按节点 `id` 取原因，不按名称跨类型取原因。若数据仍把多个设备类型指向同一个现象节点，原因池会重新共享；这属于图数据待拆分，不是适配器按名称匹配导致的。

| 中文节点标签 | 内部实体类型 | 主键 |
|---|---|---|
| `设备实例` | `device` | `id`（`台账ID列表` 是台账 ID 的兜底来源） |
| `设备类型` | `device_type` | `id`（如 `DEV#架空线路`） |
| `故障现象` | `phenomenon` | `id`（各设备类型独立，如 `PH-04`） |
| `故障原因` | `cause` | `id`（如 `CA-04`） |
| `触发条件` | `trigger` | `id`（如 `TC-R03`，属性含 `表达式`） |
| `处置措施` | `measure` | `id`（如 `MM-03`） |

触发条件展示优先使用 `判据口径`，其次 `说明`，最后才是 `名称`（部分数据的 `名称` 是 `C1-1` 这类短编号）。

| 中文关系 | 内部关系类型 | 方向 |
|---|---|---|
| `属于` | `belongs_to_device_type` | 设备→设备类型 |
| `具有现象` | `has_phenomenon` | 设备类型→该类型自己的现象 |
| `具有原因` | `has_cause` | 现象→原因 |
| `具有触发条件` | `has_trigger_condition` | 原因→触发条件 |
| `具有措施` | `has_measure` | 原因→处置措施 |
| `电气连接` | `electrically_connected_to` | 设备→设备；出边为下游，入边为上游 |

设备类型解析优先走 `属于` 边；没有该边时按台账 `设备分类` 做 v1 映射（`输电线路`/`配电线路`/`中压线路`/`线路` → `架空线路`，`杆塔单元` → `杆塔`，`箱式变电站` → `箱式变电站`）。`站`/`变电站` 不映射，返回 `device_type_missing` 固定文案，不猜测。

因果链只走 `故障现象 -[具有原因]-> 故障原因`；现象与原因按节点 `id` 查询，不按名称跨类型串链。一个设备类型有多个现象时，只有命中现象进入推理表与定位范围，原因仍按各自现象节点挂载；共享名称的现象如果确实拆成了多个节点，就不会互相带入原因。

拓扑只读设备之间的一跳 `电气连接`：以当前设备为起点，出边指向的设备是下游，入边来自的设备是上游；不读取 `供电于`，不做多跳扩展，也不按设备分类推断方向。没有入边或出边时对应列表为空并写入 `warnings`；故障后果已移除，影响范围不再输出后果节点。

## 实时值与规则求值

实时值 key 约定为 `{FAULT_TRACE_REDIS_PREFIX}{redis_device_id}:{attribute}`。`redis_device_id` 先用 MinIO 台账按设备名 + 祖先路径匹配（同名设备靠上级变电站区分），台账未命中时回退故障图的 `台账ID列表`（缺省时去掉 `LGR#` 前缀）。只读、`MGET` 批量、单次 1 秒超时、最多 20 个属性；不写 Redis、不遍历 key。

| attribute | 类型 | 单位 | 来源 |
|---|---|---|---|
| `crossarm_temp_rise` | float | K | 红外热像仪 |
| `foreign_object_temp_rise` | float | K | 红外热像 |
| `contact_point_temp_rise` | float | K | 红外热像 |
| `leakage_current` | float | mA | 泄漏电流在线监测 |
| `uv_photon_rate` | float | 光子/s | 紫外成像仪 |
| `line_protection_trip` | bool | — | 保护装置（SCADA） |

属性名来自触发条件 `表达式` 中出现的标识符，上表只是当前线路数据集的示例，不是白名单；表达式引用未列出的属性同样读取。`image_*` 属性不读 Redis：配置了实时值地址时状态为 `not_collected`，未配置地址时与其他属性一样返回 `not_configured`；缺失时按 `image_required` 处理。读取结果按状态区分：`missing`（key 不存在）、`invalid`（值无法解析或为 `nan`/`inf`）、`unavailable`（连接失败或超时）、`not_configured`（未配置地址）、`not_collected`（图像类属性不来自 Redis）；除 `ok` 外都映射为 `unknown`，**不当作 `false`**。`unavailable` 与 `not_configured` 会触发“实时数据服务暂时不可用”告警，`missing` 只表示该设备缺这项数据。

触发条件的 `表达式` 使用受限 AST：标识符、数字/布尔字面量、比较、`and`/`or`/`not`、括号与四则运算；不支持函数调用、属性访问或下标，不使用 `eval`。任一必需属性不 `ok` 时结果为 `unknown`；表达式非法或超长（>500 字符）为 `expression_invalid`。

原因聚合使用 AND：全部可求值触发条件 `true` → `established`；任一可求值条件 `false` → `rejected`；没有 `false` 但有 `unknown` → `unknown`；只有定性条件时 `unknown`。多个原因同时成立时全部列出；首要原因按「true 条件数降序、`cause_id` 升序」排序，只用于卡片主字段和证据。

## 溯源链路

`trace_fault` 工具只做参数校验、配置读取、缓存与调用；`fault_trace/workflow.py` 的 `run_fault_trace` 按固定四步推进，步骤间只共享 `FaultTraceState`。工具输入只有 `query_text`；确认结论只来自确认卡片的恢复值，模型无法通过工具参数传入图片结果或跳过确认。

1. 意图与设备校验（`fault_trace/intent.py`、`graph_kg3.find_devices`）：规则解析兜底、模型识别为增强（关闭思考、temperature 0）。设备名先展开成图谱写法后再查：去掉 `110kV` 等电压前缀，`1号主变` 同时尝试 `#1主变`/`1#主变`/`1主变`。先用设备名称查图谱，未命中返回 `device_not_found`、多候选返回 `device_ambiguous` 澄清；校验通过后才弹意图确认选项卡，用户确认后继续。确认期间不查图、不生成摘要。用户关闭确认卡片或修正追问卡片时按取消终止，返回固定取消文案；解码时先判取消再取修正文本，避免把裸 `reject` 当成用户补充的设备或现象。重新识别后的确认卡片在问题文本前显示上一轮补充，识别结果没有变化时用户也能确认输入已被采用。

   设备类型按“设备 id 的 `属于` 边 → 台账分类映射 → 台账 ID 的 `属于` 边”解析；补充数据设备共用占位台账 ID（`补充数据`，kg5 中 301 个），台账回退只有命中唯一类型时才采用，否则按 `device_type_missing` 返回固定文案，不猜类型。确认卡片的设备类型以模型识别结果为主体，图谱归类不同时并列展示，例如 `设备类型=输电线路（图谱归类：架空线路）`；因果链、现象与原因仍按图谱归类读取，用户确认不改变图谱事实。
2. 图片与故障现象（`fault_trace/workflow.py`、`yolo_service`）：先识别会话状态里最近一条用户消息的上传图片并调用 YOLO；没有上传图时按台账设备名称从 MinIO `fault-assets/device-images/{设备名称}` 取图再识别。文字和图片都没有故障现象时返回 `image_not_found` 澄清；有图但 YOLO 不可用时降级到文字现象并记录警告。
3. 故障摘要（`fault_trace/report_text.compose_summary_text`）：模型结合用户问题、设备与故障现象生成一句简述，设备字段取第 1 步结果；文本必须包含设备名并覆盖已知现象，否则回退确定性模板。
4. 因果链与实时判据（`graph_kg3.collect_kg3_evidence`、`realtime`、`rules`）：按设备类型取该类型自己的现象、原因与触发条件（`设备类型 -[具有现象]-> 故障现象 -[具有原因]-> 故障原因`），读取实时值并求值，聚合每个原因的状态与缺失属性。现象匹配以图片识别类别为准（没有图片类别时用文字现象），推理表只展示命中现象；③ 也只使用这些现象可达的成立原因。图片与文字都没有对上图谱现象时，推理表列出该设备类型的全部现象并写入“未匹配”告警，定位按成立原因反推现象。
5. 拓扑追溯（`fault_trace/graph.py` 的 `trace_topology_evidence`）：按一跳 `电气连接` 的出边/入边输出下游与上游设备、路径与缺失方向警告。
6. 原因定位推理依据（`fault_trace/report_text.compose_location_reasoning`）：模型只能引用定位原因、成立现象、命中/未命中判据和实时值；文本必须覆盖定位原因，并在有成立现象时提及至少一个现象，否则回退确定性文案。
7. 报告装配（`fault_trace/answer.py`）：确定性装配五段式报告（故障摘要、推理过程、原因定位、影响范围、处置建议），`schema_version="fault-trace-report/v1"`；`chat_service._resolve_fault_trace_output` 只认本轮工具结果的 `answer_markdown`，且只在 `answer_verified is True` 时发布，未通过校验时返回固定失败文案；澄清载荷发布其固定问句，固定文案原样透传。

图片用途由智能体配置决定：`image_input_mode="model"`（默认）时图片随消息交给模型；`"server"` 时平台把图片留在会话状态，由智能体自己的工具消费。`chat_service` 不包含任何具体智能体的图片分支，新增智能体只改自己的配置和工具。

## 五段式报告

- ① **故障摘要**：只输出故障现象简述与涉及设备，不再附带故障现象、图片识别等额外字段。
- ② **推理过程**：按现象分组的 HTML 表格，列只有「故障现象 / 可能原因」；同一现象的多条原因用 `rowspan` 合并现象单元格。表格只列命中现象（图片识别类别优先，其次文字现象）；没有任何命中现象时才列出设备类型的全部现象并带“未匹配”告警。只展示图谱查询到的内容，不写判定结果。
- ③ **原因定位**：列出命中现象范围内全部成立原因、判定成立的故障现象、每个原因的命中规则（命中表达式 → 原因）与模型推理依据；多个原因并存时逐条列出。“判定成立的故障现象”只取本次命中且已有成立原因的现象；未命中现象时按成立原因反推现象集合并带 warning，不列出没有成立原因的现象。命中现象但没有任何原因边时显示 `—`。
- ④ **影响范围**：上游根源设备、下游受影响设备与拓扑路径。
- ⑤ **处置建议**：只列成立原因及其处置措施，措施取图谱节点的 `说明` 内容（不是节点名称），换行在表格内转为 `<br>`；没有成立原因时显示“未定位成立原因，暂无处置建议”。

报告 JSON 的 `reasoning[].causes[]` 带 `measures`，`location` 带 `causes`、`phenomena` 与 `rule`，`suggestions` 带成立原因与措施说明；`answer_markdown` 由这些字段确定性渲染，卡片与发布消息共用同一份内容。

`answer_verified` 的含义是“报告里没有未经证据校验的定位结论”，不是“报告已给出结论”。每个定位原因都必须由非定性、结果为真的判据支持；未定位任何原因时没有待校验的结论，该字段仍为 `true`，报告按确定性推理发布。模型文本只能替换故障现象简述与推理依据，且必须通过各自的证据门；未定位时模型推理依据一律不采纳。

## 四步思考过程

| index | step_id | 数据来源 | `data` 关键字段 |
|---:|---|---|---|
| 1 | `parse_fault` | 意图识别、图谱设备校验、图片/YOLO、故障摘要模型、确认卡片 | `device`、`candidates`、`image`、`phenomena`、`summary`、`needs_clarification` |
| 2 | `match_causal_chain` | 故障图谱的设备类型、现象、原因、触发条件、实时值与求值 | `device_type`、`phenomena`、`causes`、`triggers`、`missing_data`、`warnings` |
| 3 | `trace_topology` | 一跳 `电气连接`（出边下游、入边上游） | `upstream`、`downstream`、`paths`、`consequences`、`warnings` |
| 4 | `generate_report` | 原因定位、推理依据模型、五段式报告与发布门 | `located_cause`、`verdict`、`reasoning`、`report`、`answer_markdown` |

确认卡片中断期间第 1 步停在 `waiting`，用户确认后同一步更新为 `completed`，不新增步骤。设备未命中/多候选时第 1 步 `waiting`，后续步骤不执行；图谱连接/查询失败时第 1 或第 2 步 `failed` 并返回固定文案；因果链为空时第 2 步 `failed`；实时值缺失或超时仍 `completed`，原因状态为 `unknown` 并记录 `missing_data`。

## 源码定位与验证

| 关注点 | Owner |
|---|---|
| 工具入口、配置读取、结果缓存 | `backend/package/yuxi/knowledge/fault_trace/tool.py` |
| 四步控制流、确认卡片与澄清 | `backend/package/yuxi/knowledge/fault_trace/workflow.py` |
| 意图识别、确认解析、澄清话术常量 | `backend/package/yuxi/knowledge/fault_trace/intent.py`、`contracts.py` |
| 故障图查询与设备类型解析 | `backend/package/yuxi/knowledge/fault_trace/graph_kg3.py` |
| 实时值读取与状态词表 | `backend/package/yuxi/knowledge/fault_trace/realtime.py` |
| 表达式求值与原因聚合 | `backend/package/yuxi/knowledge/fault_trace/rules.py` |
| 报告装配与发布载荷 | `backend/package/yuxi/knowledge/fault_trace/answer.py`、`report_text.py` |
| 报告发布门 | `backend/package/yuxi/services/chat_service.py` 的 `_resolve_fault_trace_output` |
| 前端思考面板 | `web/src/components/AgentChatComponent.vue`、`web/src/utils/thinkingSteps.js` |

验证命令与分层见[测试规范](../develop-guides/testing-guidelines.md)。最小相关集合：

```bash
docker compose exec api uv run --group test pytest test/unit/knowledge/test_fault_trace_*.py
docker compose exec api uv run --group test pytest test/unit/services/test_chat_service_fault_trace_scope.py
docker compose exec web pnpm run test:unit
```

真实 TuGraph、Redis 与 MinIO 上的一次端到端溯源仍需要人工确认报告内容：单测只能证明装配、状态映射与发布门，不能证明图谱数据本身正确。

## 调试入口

- 图谱：`CALL db.vertexLabels()`、`MATCH (n) WHERE labels(n)[0]='故障现象' RETURN n` 验证数据；设备/类型/因果链查询见 `fault_trace/graph_kg3.py`。
- 设备图片：`mc ls <alias>/fault-assets/device-images/`；对象名必须是台账设备名称，扩展名 `.jpg`/`.jpeg`/`.png`/`.webp`，缺图时第 1 步按 `image_not_found` 澄清。
- 实时值：`redis-cli -p 6380 GET '<台账ID>:leakage_current'` 验证 key 与类型；`FAULT_TRACE_REDIS_URL` 留空时回退应用 `REDIS_URL`，指向不可达地址时属性状态为 `unavailable`，指向错误 db 时 key 不存在，状态为 `missing`。
- 规则：`fault_trace/rules.py` 的 `expression_invalid`、`missing_key`、`redis_unavailable`、`invalid_value`、`image_required` 对应不同数据缺口；不要在缺值时手工改判 `false`。
- 发布：`answer.py` 生成的五段式 Markdown 应包含 ①–⑤；`chat_service` 日志中的发布门覆盖表示最终消息来自工具结果。
- 思考过程：SSE 抓包按 `status="thinking"` 检查四个 `step_id` 的顺序与状态，工具结果与消息 `extra_metadata.thinking_steps` 应一致。
