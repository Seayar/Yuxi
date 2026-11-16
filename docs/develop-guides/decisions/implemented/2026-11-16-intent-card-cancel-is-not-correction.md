# 意图确认卡片的关闭与取消不再当作修正文本

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/utils/question_utils.py

> 消费方：`backend/package/yuxi/knowledge/power_spec/intent.py`、`backend/package/yuxi/knowledge/fault_trace/intent.py` 的确认解析，以及两个工具的确认循环与固定文案。

## 问题

生产数据（`agent_runs` 57518f9f、ba17a129、47d6471f 等）显示：用户点“不正确，我要补充/修改”后看到补充修正卡片，关闭该卡片时前端把裸 `reject` 作为恢复值提交。`extract_correction_text` 只过滤 `__other__`、`other`、`其他`，于是 `"reject"` 被当成用户补充文本写进 `feedback`，工具带着无意义反馈重新识别并再次展示确认卡片；识别计划没有变化，用户看到同一张卡片反复出现。

LangGraph 在同一次任务内按顺序累积恢复值，误判会把取消值追加进恢复列表。之后每次恢复都从这张变长的列表取前几个值，中断序号与实际卡片错位，用户后续的正常输入可能落到别的卡片上，表现为“输入没有被接收”。

## 决策

- 问题卡片的关闭值 `reject`（以及 `rejected`、`cancel`、`cancelled`、`取消`、`关闭`）是取消，不是自由文本。`yuxi.utils.question_utils.is_question_cancel` 统一识别它，电力规范与故障溯源在两个卡片入口先判取消。
- `parse_intent_confirmation` 增加 `cancel` 决定；`extract_correction_text` 对取消值返回 `None`。关闭确认卡片直接结束确认，不再追问修正内容；关闭修正追问卡片同样结束。
- 取消结果返回固定文案 `INTENT_CANCELLED_MESSAGE`，并加入各模块的 `PLAIN_TOOL_MESSAGES`，由发布门原样透传，不交给模型改写。故障溯源原有的超轮次文案一并收敛为 `INTENT_ATTEMPTS_EXHAUSTED_MESSAGE` 并进入白名单，避免发布门把它替换成报告失败文案。
- 重新识别后的确认卡片在问题文本前显示上一轮补充（`intent_plan["correction"]`），模型重识别结果没有变化时用户仍能确认输入已作为反馈参与识别。
- 只匹配整个取值：`取消 110kV 这一项` 这类长文本仍是修正内容，不按取消处理。

## 替代方案

- **前端改提交结构化取消值**：协议更精确，但设备/场景选择卡片（`maintenance_plan`、`device_prediction`）共用同一个关闭按钮，改取值会让它们的 `_parse_choice` 把未知对象当成“未选择”并重新询问，需要同步改四个知识工具的取消语义；本次只修既有 `reject` 的语义，放弃。
- **前端隐藏或禁用关闭按钮**：用户失去取消能力，第三方调用方仍会提交 `reject`，缺陷依旧存在。
- **只在 `extract_correction_text` 里把 `reject` 加进忽略集合**：确认卡片上的取消仍会进入补充追问；取消必须作为独立结果贯穿两个卡片。

## 后果

- 关闭任一意图卡片立即结束本次确认，返回固定取消文案；不重新识别、不再弹窗。
- 历史 interrupted 线程恢复时按新语义读取当前卡片：关闭即取消，不再把累积的旧取消值当成新的修正。
- 用户只选“不正确”且不填文本时仍追问补充内容；正常修正文本与最多 3 轮重新识别不变。
- 确认选项值里多携带一个 `correction` 字段；`decode_confirmed_plan`/`query_from_confirmed_plan` 只读已知字段，检索条件不受影响。
- `maintenance_plan`、`device_prediction` 的选择卡片取消语义不在本次范围，行为未变。

## 验证

- `test/unit/knowledge/test_power_spec_intent_interrupt_loop.py`：真实 LangGraph 复现线上序列（确认卡片 → `correct` → 补充卡片 → `reject`），断言没有新中断、结果为取消文案、只有 attempt 1 的模型调用；另有确认卡片直接关闭和真实 `create_agent` + ToolNode 装配下的同一序列。
- `test/unit/knowledge/test_power_spec_tool_intent.py`、`test/unit/knowledge/test_fault_trace_tool.py`：工具级取消用例断言只展示 1 张确认卡片和 1 张补充卡片、只识别一次、返回取消文案且在 `PLAIN_TOOL_MESSAGES` 中。
- `test/unit/knowledge/test_power_spec_tool_intent.py`、`test/unit/knowledge/test_fault_trace_intent.py`：解析器用例断言 `reject` 与 `{"<question_id>": "reject"}` 为取消、`extract_correction_text` 返回 `None`、长文本仍是修正内容。
- 执行记录（`docker compose exec -T -w /app api uv run --no-sync --group test pytest ...`）：上述意图用例 76 passed；`test/unit/knowledge` 907 passed、5 failed（`test_power_spec_formatting.py` 的 PDF 标记 4 个、`test_device_prediction_intent.py` 的设备名解析 1 个，均为工作区既有失败，不经过本次改动面）；`test/unit/agents`（`-m 'not slow'`）230 passed；`test/unit/services/test_chat_service_fault_trace_scope.py`、`test_chat_stream_interrupt.py`、`test/unit/agents/toolkits/buildin/test_ask_user_question.py` 30 passed。
- `ruff check` 与 `ruff format --check` 对本次改动文件通过；`scripts/verify_engineering_contracts.py` 通过（148 decisions / 201 docs），其自测 62 passed；`docs` 构建通过。
- 未验证：真实浏览器页面上的关闭按钮交互与真实模型重新识别，依赖本地前端与模型服务，未在本次执行。
