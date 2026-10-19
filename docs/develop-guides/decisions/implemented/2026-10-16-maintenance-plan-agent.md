# 处置方案生成智能体（maintenance-plan-agent）

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/maintenance_plan/tool.py

## 问题

`需求_处置方案生成.md` 与 `ref/原型_V0.1.0.0.html` 定义了「处置方案生成」需求：用户以文字/语音输入运维任务
（例如“更换#3开闭所3k18断路器”），系统在设备台账中定位目标设备，检索作业模板与规程条款，**按确定性规则**
装配出六段式标准化处置方案（检修前准备 / 检修步骤 / 安全措施 / 所需工器具及材料 / 责任班组与资源安排 /
验收标准），前端用卡片展示。SRS 要求信息不完整时按方案二（多候选引导选择）/方案三（反问澄清）继续对话，
且方案中每个数值与条款号都必须可追溯到图谱证据。

当前平台已上线「电力规范检索」（`power-spec-agent`）与「故障原因溯源」（`fault-trace-agent`）两个智能体，
但没有处置方案能力：没有处置方案 Agent（技能/预设/工具/发布门），知识图谱 `kg` 中也没有作业模板、
安全距离、班组、备品备件四类节点与相应关系数据（需由 `seed.py` 补齐）。

## 决策

按故障溯源的同款装配路径新增「处置方案生成智能体」，四条链路：

1. **意图链路**：工具 `generate_maintenance_plan`（`yuxi/knowledge/maintenance_plan/tool.py`）接收用户任务原文，
   经 `intent.extract_plan_intent` 解析任务类型（更换/检修/试验/巡视）、设备名、设备类型、电压等级、作业对象、
   位置线索（模型结构化 JSON + 规则兜底）；两项都缺 → 返回固定澄清文案（方案三），缺任务类型 → 走 `interrupt`
   选择卡片（方案二）。方案装配**不调用大模型**。
2. **图谱链路**：`graph_plan.search_devices` 在 `设备实例` 台账中召回打分（名称/层级路径/设备类型/电压综合），
   候选不唯一时 `interrupt` 让用户选；`graph_plan.collect_plan_evidence` 收集层级祖先、停电范围、作业模板与
   依据条款、试验/巡视项目、安全距离、管辖班组、备品备件，每步写 `retrieval_log`。设备定位与证据收集是
   确定性只读查询，中断重放不产生副作用。kg 连接复用管理员已配置的 `fault_trace_opts`（同一个 `kg` 图）。
3. **装配链路**：`answer.produce_maintenance_plan_answer` 用模板 + 证据确定性装配六段式方案、生成 Markdown 与
   “思考过程”，并由 `verify_report` 做前置校验门（报告里每个数字必须作为完整数字出现在证据中，条款号必须来自
   模板的依据条款）；校验不过不通过发布门。
4. **发布链路**：工具返回 `report_payload(answer_source="maintenance_plan", report, answer_markdown,
   answer_verified)`；`chat_service` 新增 `_is_maintenance_plan_context` / `_resolve_maintenance_plan_output`
   发布门（按 `"maintenance-plan" in context.skills` 判定），用工具产出的 `answer_markdown` 覆盖模型最终消息。
   **发布门只发布通过证据校验的方案**：本轮工具返回的报告载荷必须显式声明 `answer_source="maintenance_plan"`
   且 `answer_verified is True`（严格布尔判断）才采用其 Markdown；`answer_verified` 为 `False`、缺失或非布尔
   （如字符串 `"true"`）一律返回固定文案 `PLAN_VERIFICATION_FAILED_MESSAGE`
   （“处置方案证据校验未通过，无法生成可发布的标准化方案”），绝不把未校验的方案发出去。
   固定文案（未配置 / 未找到设备 / 无模板 / 校验未通过 / 澄清 / 交互过多）原样透传，
   其他普通文本与非报告 JSON 返回固定失败文案，不回退发布模型内容；只读本轮工具结果，复用既有
   `_current_turn_tool_messages` 过滤，不对整段历史做额外遍历。
   `toolkits/__init__.py` 增加工具注册 import。工具侧边界（T9）同步阻止直接返回未校验报告，
   与发布门形成两道各自带负例的防线。

预设与技能：`agents/presets/maintenance_plan.py`（`slug="maintenance-plan-agent"`，`name="处置方案生成"`，
`backend_id="ChatbotAgent"`，技能 `knowledge-base` + `maintenance-plan`）、
`agents/skills/buildin/maintenance-plan/SKILL.md`（`tool_dependencies: ["generate_maintenance_plan"]`）。
中断载荷沿用 Yuxi 既有选择卡片格式（`source + questions[]`，与故障溯源确认卡片一致），不需要新的中断渲染器。

前端范围：**本期不实现网页**。前端卡片任务（T12b）已取消，本变更只交付后端接口与卡片数据契约，
验收只在后端接口与 API 层进行（中断载荷、发布门输出、`answer_markdown` 与 `report` 契约）。

## 替代方案

- **方案内容交给大模型润色/生成**：方案内容是安全规程，必须逐条可追溯。实际选择是只从图谱证据确定性装配，
  模型仅用于意图解析（temperature 0），装配后再用 `verify_report` 做完整数字集合与条款来源校验。
- **用电连接拓扑推导停电范围**：`kg` 台账的电连接边几乎为空（只有 3 条演示边），无法支撑拓扑分析。实际选择是
  按设备台账层级推导，并在方案中固定注明“依据设备台账层级推导，执行前须以调度下达的停电范围为准”。
- **新增独立的 kg 连接配置项**：会与故障溯源重复维护同一张 `kg` 图的连接信息。实际选择是复用 `fault_trace_opts`。
- **重名设备由模型自行挑选或默认取第一条**：台账重名普遍（如 `992SVG开关` 当前在 35kV#1、#2、#3 变电站各一台），
  静默选一个会给出错误对象的作业方案。实际选择是候选不唯一时走 `interrupt` 让用户确认（SRS 方案二）。
- **班组/备件直接对接生产管理系统**：本期无接口。实际选择是先入演示数据并在方案中如实标注
  “班组与备件为演示数据，待接入生产管理系统后替换”，外部接口留作后续适配点。
- **新建中断渲染器（T8 的 `plan_choice` 自定义格式）**：会引入第二套中断协议。实际选择是改用既有
  `source + questions[]` 格式，与故障溯源确认卡片一致；网页渲染本期不做（T12b 已取消），只保证中断载荷
  与既有协议兼容，并在 API 层验收。
- **只靠装配阶段的 `verify_report`、发布门不再校验**：装配校验与发布是两条边界，工具返回与最终消息之间
  还隔着模型与 checkpoint 历史，单点校验不足以阻止未校验 Markdown 外泄。实际选择是两道边界都拦：
  工具侧不返回未校验报告，发布门也不发布未校验 Markdown，两者各自带负例。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 预设 `maintenance-plan-agent`、技能 `maintenance-plan` 与工具 `generate_maintenance_plan` 被平台发现并注册 | preset/skill 未被发现，或工具未注册（slug 冲突、`__init__` 循环导入导致导入失败） | `presets/maintenance_plan.py`、`skills/buildin/maintenance-plan/SKILL.md`、`toolkits/__init__.py` | 容器内 `cd /app && python -m pytest -q -p no:cacheprovider test/unit/knowledge -k maintenance_plan`；重启后日志确认 preset/skill/tool 加载 | 未注册时该智能体对话中无工具可调；`__init__` 导入 tool 会与 toolkits 循环导入 | Passed |
| 发布门用本轮工具 `answer_markdown` 覆盖模型最终消息 | 模型改写/摘要卡片结构后仍被展示 | `chat_service._resolve_maintenance_plan_output` | unit（已实测）：补丁应用后按 AST 提取真实函数执行 `test_maintenance_plan_gate.py`（离线两种后端布局各 29 passed）；真实 HTTP run result 与审计中工具 Markdown 逐字比较一致 | 工具返回非白名单纯文本 → 固定失败文案；上一轮工具卡片不被采用 | Passed |
| **未通过证据校验的方案不被发布门采用** | `answer_verified` 为 False / 缺失 / 非布尔（如 `"true"`）时仍把 Markdown 发出去 | `chat_service._resolve_maintenance_plan_output` | unit（已实测）：`test_unverified_report_is_not_published`、`test_missing_verification_flag_is_not_published`、`test_non_boolean_verification_flag_is_not_published[6 组]`、`test_report_without_source_marker_is_not_published`；独立复核 `work/test_gate_verification_review.py`（1 passed） | 上一轮未校验报告不影响本轮；本轮无可信结果时返回固定失败文案；本轮未校验报告按 `tool_call_id` 取用时同样拒绝 | Inspected |
| 固定文案（未配置/未找到设备/无模板/校验未通过/澄清/交互过多）原样透传 | 模型在固定文案后补充推测、建议或替代答案 | `chat_service`、`contracts.PLAIN_TOOL_MESSAGES` | unit（已实测）：按当前契约 `PLAIN_TOOL_MESSAGES` 逐条断言原样返回（8 条，参数化自契约而非硬编码）；联调用例 5/7/8/9 | 故障溯源的固定文案不在本模块白名单，不能作为本模块结果发布 | Inspected |
| 方案装配不调大模型，数值与条款号可追溯 | 出现证据中不存在的数字/条款，或模型自造步骤 | `answer.py`、`graph_plan.py` | 有报告的真实 HTTP 用例逐条检查 `answer_verified=true`、六段齐全、计数与模板一致；负向结果为固定文案，原用例 8/9 的边界见正式验收节 | 把“60”改成“6”这类篡改必须被 `verify_report` 拦下 | Passed |
| 设备/任务类型中断沿用既有选择卡片格式（`source + questions[]`） | 自定义 `plan_choice` 载荷与既有协议不一致，中断无法被既有通道恢复 | `tool.py`、`contracts` 中断常量 | 联调用例 2/3/4 走完整中断—选择—恢复链路；API 层核对中断载荷键名 | 中断载荷缺 `source`/`questions[]`，或恢复后重复调用工具 | Passed |
| kg 缺数据由 seed 幂等补齐，重名区域/未匹配条款必须报告 | 重复导入产生重复节点/边；重名区域静默任选一个；未匹配条款静默跳过 | `seed.py`、`data/`（T10） | 真实 `kg` 回读：同一 seed 导入两次节点/边计数不变；`unmatched_clauses`/`unmatched_areas` 清单 | 条款号后缀归一后仍不中 → 记入清单而不是报错中断 | Passed |
| 本变更不改动既有智能体行为 | 补丁修改/删除 power-spec、fault-trace 既有分支，或改动共享 `_resolve_tool_card_answer` | `patches/chat_service.patch`、`patches/toolkits__init__.patch` | 已实测：`patch --dry-run` 对 ref 副本成功；补丁只有新增行；AST 逐字节比较证明 `_resolve_tool_card_answer`/`_current_turn_tool_messages`/`_message_field` 与 power-spec、fault-trace 四个发布门函数均未被改动 | 补丁含删除行，或 elif 链顺序改变导致既有智能体命中错误分支 | Inspected |

“当前结果”列取值：**Passed** = 真实环境联调通过；**Inspected** = 已在本地/离线实际运行证据覆盖
（补丁后真实源码的函数级测试、两种后端布局、独立复核用例），但未做真实服务器联调；
**Not run** = 未做。未做真实联调的一律不写 Passed，由 T13 联调后更新。

## 后果

方案质量完全取决于 `kg` 中的作业模板与规程条款数据质量：模板缺口（避雷器缺检修，绝缘子缺更换/巡视，开闭所缺更换）
会表现为 `NO_TEMPLATE_MESSAGE`，属如实报告而非降级伪造。班组与备品备件为演示数据，方案中已标注
“演示数据，待接入生产管理系统后替换”，不可当作生产依据。停电范围只能按台账层级推导（电连接拓扑几乎为空），
方案中已注明“以调度下达的停电范围为准”。

最终集成版 `graph_plan.py` 的 13 个查询调用点已在真实 kg 只读实测，展开为 17 个场景，全部成功，最大耗时 114.59 ms。这是 seed 前的语句和设备证据验证；新增模板、安全距离、班组、备件的数据正确性仍须正式 seed 后回读。654 条模板引用中，当前 kg 有 73 条无法匹配（32 个不同的标准/条款组合），须由 seed 如实报告，不强行关联其他条款。

**未校验方案外泄**是本次审核发现并已修复的风险：装配校验门（`verify_report`）与发布门之间原本存在缝隙——
只要工具返回里带 `answer_markdown` 就会被发布，`answer_verified=False` 也不例外。现在发布门按
`answer_source="maintenance_plan"` + `answer_verified is True`（严格布尔）双条件放行，其余一律返回
`PLAN_VERIFICATION_FAILED_MESSAGE`；工具侧（T9）同步阻止直接返回未校验报告，两道边界各自带负例。
残余风险是 `answer_verified` 的语义依赖 `verify_report` 的实现质量（数值完整集合比对、条款来源比对），
该部分由 T7/T12a 的测试与 T13 联调覆盖。

部署只允许重启 `yuxi-api-1` 与 `yuxi-worker-1`。API 实际启用 `--reload`，写入挂载源码本身可能触发服务重载，因此运行目录部署与重启前须获得用户确认。服务器 `/opt/Yuxi` 为 dirty 状态，已备份到 `/raid/backup/yuxi_pre_mp_20261016_102346`；正式部署前再次比对两个集成点，若出现新差异则停止覆盖。

本期不交付网页（T12b 取消），因此“用户能否在界面上看到方案卡片与选择卡片”不在本次验收范围；
发布门与中断载荷只保证后端接口与 API 契约正确，界面呈现留待后续前端任务。

## 部署前已验证范围（Codex）

- 本地集成全量 357 passed；额外4条独立反例通过。
- API容器的隔离 /tmp 后端使用真实 Yuxi 依赖，357 passed in 8.42s，无测试桩、无 skip。
- 隔离包真实工具注册恰好一次，preset发现 maintenance-plan-agent；运行实例尚未部署。
- 同名992SVG开关当前实际有3台。修正选定设备后按ID查询父级/电连接的身份隔离，真实kg回查三台设备的停电范围各自归属本厂站。
- 电压按设备属性、最近祖先逐级解析；同一祖先属性优先于名称，远站属性不能覆盖近层电压区域。
- 工具和本模块发布门只接受 answer_verified is True 的报告，失败返回契约固定文案 PLAN_VERIFICATION_FAILED_MESSAGE。
- seed CLI在隔离后端 dry-run 通过：59模板、14安全距离、10班组、37备件；尚未正式写图。
- 本节是部署前历史快照；当前正式部署与验收结果以后节为准。前端不在本组职责。

## 正式部署与验收（Codex，2026-10-16）

已获得用户明确授权，完成正式后端部署与真实 kg seed。先备份到 /raid/backup/yuxi_pre_mp_deploy_20261016_133459；联调修正逐次做哈希检查和增量备份。最终交付 28 文件，源码位于 /app/package、测试位于 /app/test，仅重启 yuxi-api-1、yuxi-worker-1，两者 healthy。API 与 worker 均监视源码自动重载；未动 R14 或模型服务。

本地全量 368 passed，独立反例 4 passed；服务器最终知识模块、注册发现与既有故障溯源发布范围合计 755 passed。新增 8 个业务回归与 3 个发布旁路负例，同时同步既有注册发现测试清单。

seed 实际回读 120 节点、851 关系，两次导入数量一致：59 模板、14 安全距离、10 班组、37 备件；59 适用模板、581 依据条款、165 管辖、46 配备备件。73 次条款引用仍未匹配并留清单；3 个区域用真实线路路径消歧后未匹配归零。最终图查询 17 场景全成功，最大 122.32ms。

联调修复：seed 参数保留字；模型扩写设备编号导致唯一设备多确认；安全距离备注丢失；信息全缺时模型自行反问。SafetyDistance.remark 为默认空的兼容字段，有备注时随报告和 Markdown 发布并进入校验语料。专用发布门没有可信工具结果时只返回固定澄清或失败文案，不再回退模型内容；原 6 条回退断言据此更新，旧轮隔离目的保持不变。

原用例 1/2/3/4/5/6/7/10 已通过工具与 HTTP 联调；原用例 8 的真实避雷器台账不存在，输入返回未找到，原无模板断言不可达，补充真实绝缘子更换验证无模板分支通过。用例 9 缺配置和不可达在独立工具进程通过，在线 API 未改变共享配置做故障注入。上表保留相关负向发布主张为 Inspected，不冒充全部真实线上故障联调完成。

所有有报告的 HTTP 结果均 answer_verified=true、六段齐全，最终正文与本轮工具 Markdown 逐字一致，工序数与资源项数一致；重复请求排除系统计时后业务内容相同。无工具时的固定澄清已通过真实 HTTP 验证。示例验收线程、运行及审计保留于平台，可通过 thread history/audits 与 run result 接口复核。
