# 电力规范检索的模型参与答案组织

状态：implemented
类型：architecture
Owner：backend/package/yuxi/knowledge/power_spec/tool.py

模型参与的组织链路与发布门落在多个文件：编排与审查解析在 `backend/package/yuxi/knowledge/power_spec/answer.py`，意图识别在 `intent.py`，解析在 `ingestion.py`，发布门在 `backend/package/yuxi/services/chat_service.py`。本记录只解释取舍，不作为运行时事实源。

## 问题

电力规范链路的卡片事实字段、条款标题修正、正文片段挑选和 Markdown 格式原先全部由程序确定性生成，大模型只把工具返回的 `answer_markdown` 原样搬运，`chat_service` 的发布门还会用工具结果覆盖模型输出。这一取舍压住了模型编造和长文输出，但带来三个能力缺口：

- 知识库解析不准确时无法纠正。OCR 造成的条款编号与标题错位只能按固定规则回退成编号，无法结合上下文判断。
- 无法按问题挑取相关正文，卡片正文经常包含整张表而非用户需要的条目。
- 无法跨条款综合，只能罗列最多 5 张条款卡片。

同时链路缺少用户要求的环节：验证失败后的模型自迭代、以及迭代超限后询问用户是否接受；验证失败直接返回「未检索到资料」，把「无证据」和「证据存在但组织失败」混为一谈。

## 决策

把答案组织权交给模型，用真实的整理与审查两个子智能体形成闭环，确定性代码只保留廉价的编造前置门；意图识别与答案组织在当前运行中收敛为「整理一次、审查一次、合并一次、直接发布」的单一路径，**不再重试、不再有 acceptance interrupt**。

- **意图识别**：模型输出结构化意图（问题重述、设备、电压等级、问题维度、检索关键词），规则解析 `parse_power_spec_query` 降为模型失败时的兜底。意图确认卡片展示模型理解；用户否定并给出修正时带上一轮意图与修正重新识别（最多 3 次）。意图识别固定 `temperature=0`。
- **答案组织**：整理子智能体（`power-spec-organizer`）接收用户问题、已识别维度与受限条款原文，逐条输出 `body`，要求带 `[source:document_id#clause_no]` 引用；审查子智能体（`power-spec-reviewer`）同时接收原文与整理结果，**只输出审查意见** `{"issues":[...]}`，不再给 pass/fail；主智能体（父 Agent 模型，经 `_context_model_call` 非流式调用一次）以整理内容为基础结合审查意见产出最终 `body`，然后直接发布。
- **关闭重试与 acceptance interrupt 的原因**：resume 会把整个 LangGraph 节点从头重放，而「是否弹接受卡片」依赖整理/审查结果 ⇒ 用户作答后整理+审查会完整重跑，且重放可能改变分支而静默丢弃用户选择。改单一路径后节点内不再有依赖模型结果的用户确认分支。
- **检查降级为审查意见**：`ungrounded_numbers` 与 `placeholder_clause_keys` 的结果各作为一条 `ReviewIssue` 交给合并步骤，不再阻断发布。`parse_review_issues` 只能接受带 `issues` 数组的对象——解析出对象但缺 `issues` 时记「格式」意见，**不得当作审查通过**（`answer_verified=true`）。
- **JSON 容错提取**：`model_output.parse_model_json` 提供提取式解析（```json 围栏倒序 → 整段 → 括号配对），各调用点共用，避免「散文+JSON」被严格整段解析静默丢弃。
- **关注维度由意图识别给出**并直接带入整理任务，整理者不再自行判断；整理子智能体固定 `temperature=0`（声明字段，未声明的子智能体保持服务默认）。
- **标准文档解析**：条款优先按 Markdown 标题（`## 6.1 …`）切分，无此类标题的旧式 PDF 文本回退行首编号；从 `## 目 次` 到同级标题整块跳过目录；条款页码取标题前最近的 `source_pdf_page` 标记；编号在封面只写「代替」时用发布年推导；名称优先取「代替《名称》」。
- **缺重排模型不伪装成无资料**：知识库未配置重排模型时返回专用文案 `该知识库未配置重排模型，无法检索电力规范`，与「未检索到资料」分开，且该配置类失败不写入结果缓存。
- **发布门只发布本轮的检索结果**：`_resolve_power_spec_output` 只认本轮工具产生的 `answer_markdown`——归属优先按当前运行 `current_tool_operation_ids` 对账，运行归属不可用时按最后一条用户消息划界；本轮没有检索结果时回退模型自己的内容。`answer_source` 区分为 `model_composed` / `fixed_card`。
- **可观测开关**：`POWER_SPEC_INTENT_ENABLED`、`POWER_SPEC_KEYWORD_QUERY_TERMS`、`POWER_SPEC_CLAUSE_REVIEW_ENABLED` 等环境变量控制模型路径开关与召回参数。

## 替代方案

1. 维持程序确定性生成。实现最简单、结果完全确定，但正文只能是原文片段堆叠，表格整块出现在卡片里，可读性差且无法按问题浓缩，因此被用户明确否决。
2. 用代码做逐字回溯校验。能保证文字未被改动，但要求展示正文逐段逐字来自原文，因此禁止改写，与「压缩到可读」直接冲突。
3. 单个模型调用同时负责整理与自查。少一次调用，但同一上下文里的自查容易放过自己的错误，且没有独立审查记录。
4. 整理与审查都由代码实现，模型只做措辞。无法处理解析错位和跨条款综合，等于回到方案 1。
5. 早期「审查判定 + 最多 3 次重试 + 超限询问用户」。在 resume 重放下会造成重复启动与分支漂移，改为单一路径后废弃。

## 后果

- 每次问答固定为 2 次子智能体运行（整理、审查）+ 3 次主模型调用（意图识别、条款校对、最终整理）；任一环节失败都回退固定卡片，最坏情况仍能返回可用结果。
- 审查意见由主模型结合，而主模型只看到整理内容与意见、看不到原文；能否拒绝错误意见取决于合并提示词中的「不盲从」规则与不同角色提示词。整理/审查共享父模型盲区。
- LangGraph 每次 resume 重新执行整个节点 ⇒ 意图确认循环中先前轮次的识别会重跑（确认 1 次 = 2 次调用，3 轮全否定 = 9 次），只影响被否定路径。
- `runtime.context` 每次 resume 重建 ⇒ 工具内 `setattr` 缓存不跨 resume，发布门与结果缓存只能在同一节点执行内起作用；展示与执行依赖模型确定性（识别 `temperature=0`）。
- **预设只在首次落库时写入**：`ensure_preset` 保留已存在的用户配置。修正已发布预设的提示词/温度后，**已落库环境不会自动获得新值**，必须执行预设刷新脚本（见验证），否则改动是 no-op。本机数据库已刷新并回读一致。
- 该服务把输入与输出一起计入 `max_model_len`，超出上限返回 400 而不是截断；输出额度必须为提示词留出空间，当前默认 65536。
- 子智能体在工具内启动并同步等待，父运行在整个等待期间占用一个 worker 槽位；默认 `max_jobs` 10，并发用户较多时父运行会挤占槽位。子 run 依赖父运行的 lease，父运行缺少 lease 时会被执行树回收逻辑取消，因此必须真实页面验证。
- 本地 Qwen3 系列模型默认先产出思考内容；该服务只识别 `chat_template_kwargs.enable_thinking`，请求体顶层 `enable_thinking` 无效。不关闭思考会因额度耗尽返回空 `content` 并使模型路径静默回退规则解析。
- 关键词由模型生成会扩大召回方差；需要日志记录实际检索语句使召回异常可归因。
- 整理者被要求「不确定时保留而不是删除」后卡片正文可能更长；占位正文前置门对「条款确实无关」的情况也要求重写。
- 早期「缺少条款就重试」已取消：整理者遗漏条款时不会再被要求补齐，卡片可能少于检索到的条款数，只能从日志观察。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 意图由模型识别并展示确认卡片 | 模型不可用或返回非法 JSON | `power_spec/intent.py` | `test_power_spec_intent.py`、`test_power_spec_intent_interrupt_loop.py` | 模型返回截断 JSON 时回退规则解析 | 通过 |
| 用户否定意图后重新识别 | 用户修正为空 | `power_spec/tool.py` | `test_power_spec_tool_intent.py`：否定后出现新确认卡片且意图随修正变化 | 连续否定 3 次后终止；修正追问无文本时立即终止 | 通过 |
| 整理子智能体列出与问题相关的内容 | 子智能体不可用或输出为空 | `power_spec/answer.py` | `test_power_spec_organize.py` | 不可用、不可解析或没有条款时返回失败，由调用方回退固定卡片 | 通过 |
| 审查子智能体只产出审查意见，且缺 `issues` 的对象按格式意见处理 | 审查输出非法 JSON 或对象缺少 `issues` | `power_spec/answer.py` | `test_power_spec_organize.py`：意见按 `issues` 读取；`parse_review_issues('见表 {1}…{"x":1}…') == ['格式']` | `{"issues":[]}` 视为无意见；缺 `issues` 键的脏对象记「格式」意见，不当作审查通过 | 通过 |
| 整理、审查、合并各只执行一次 | 编排回退到重试，重复启动子智能体 | `power_spec/answer.py` | `test_power_spec_organize.py`：子智能体调用序列恒为 `[organizer, reviewer]`，最终整理只调一次 | 任一步失败即整条失败，不重试 | 通过 |
| 模型输出「散文 + 末尾 JSON」不被丢弃 | 严格整段解析误判为不可解析 | `power_spec/model_output.py` | `test_power_spec_model_output.py`；真实样本 11707 字符解析出 `file_fc77df#6.1` 条款 | json_repair 对整段返回 list 时仍可提取出对象 | 通过 |
| 数字类编造与占位正文转为审查意见 | 检查结果被静默丢弃 | `power_spec/answer.py` | `test_power_spec_organize.py`：`999` 与占位正文都作为意见进入合并提示词 | 含意见时 `answer_verified=false`，但不再阻断发布 | 通过 |
| 问题维度由意图识别带入 | 整理者重新判断维度 | `power_spec/answer.py` | `test_power_spec_organize.py`：整理消息带关注维度段 | 未识别到维度时写入「未识别到明确维度」 | 通过 |
| 整理子智能体固定采样温度 | 温度未下发或波及未声明子智能体 | `agents/buildin/subagent/graph.py`、`presets/subagents/power_spec_organizer.py` | `test_subagent_model_temperature.py`、`test_power_spec_presets.py`；请求体实测 `temperature: 0.0` | 未声明的模型参数键被丢弃；未声明温度的子智能体请求体不带 `temperature` | 通过 |
| 固定卡片作为降级输出 | 降级路径与模型结果混淆 | `power_spec/tool.py` | 模型不可用、输出为空或用户拒绝时 `answer_source=fixed_card` | 发布门仍只发布工具给出的 `answer_markdown` | 通过 |
| 标准文档按 Markdown 标题切分且跳过目录 | 目录行被当成条款，正文跨章节累积 | `power_spec/ingestion.py` | `test_power_spec_standard_markdown.py`；真实文档 415 条、最大正文 3877 | 目录区块内的标题不得成为条款；无 Markdown 标题时回退旧规则 | 通过 |
| 知识库缺重排模型时不被伪装成无资料 | 用户无法区分配置缺失与真的没有资料 | `power_spec/tool.py` | `test_power_spec_answer_organization.py`：缺重排返回专用文案，且有重排但无命中才返回「未检索到资料」 | 该文案必须原样透传，不被模型改写 | 通过 |
| 发布门只发布本轮的检索结果 | 第二轮未调用工具时重复发布上一轮卡片 | `services/chat_service.py` | `test_chat_service_power_spec_scope.py`：本轮无工具调用时回退模型内容；有调用时只用本轮结果 | 按原始实现注入缺陷（扫描整条历史）后该测试失败 | 通过 |
| 真实运行中子智能体产出可用结论 | 父运行上下文不完整导致子 run 被回收 | `services/subagent_run_service.py` | 需要一次真实页面运行 | 父运行缺少 lease 时子 run 被取消 | 未验证 |

关键真实证据（非步骤日志）：

- 真实 Postgres `agent_runs`：`370a94c4` 与 `75d81bc3` 共享同一个 `tool_call_id`（`...#organize#1` 至 `#organize#3`、`#review#1` 至 `#review#3`）——证明 resume 会完整重放整理与审查，这是取消重试/acceptance 的直接依据。
- 真实会话 189：第二轮提问「测试」工具调用数为 0，但发布的助手文本与第一轮卡片 md5 完全相同（`a565ea89…`）——发布门改按 `current_tool_operation_ids` 归属轮次。
- 真实文档重建结果：条款 11 → **415**，最大正文 112855 → **3877**，标准编号 `DL/T393—2021`，名称 `输变电设备状态检修试验规程`；端到端检索（真实 Milvus + 重排 + 投影校验）召回到 `6.1.2.2 油中溶解气体分析` 等条款。
- 单元回归：`pytest test/unit/knowledge test/unit/agents test/unit/services`（2026-10-09 全量 `test/unit -m "not slow"`）→ **2406 passed / 54 skipped**；`ruff check` 与 `ruff format --check` 通过。
- 预设刷新脚本（本机已执行）：把仓库预设的 `system_prompt` / `temperature` 逐字写回已落库的 `power-spec-organizer` / `power-spec-reviewer` 行，保留用户配置字段，回读确认一致：

```bash
docker-compose exec -T api python - <<'PY'
import asyncio

from yuxi.agents.presets import discover_agent_presets
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.storage.postgres.manager import pg_manager

SLUGS = ("power-spec-organizer", "power-spec-reviewer")
KEYS = ("system_prompt", "temperature")

async def main():
    presets = {item.slug: item for item in discover_agent_presets()}
    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        repository = AgentRepository(db)
        for slug in SLUGS:
            agent = await repository.get_by_slug(slug)
            config = dict(agent.config_json or {})
            context = dict(config.get("context") or {})
            for key in KEYS:
                if key in presets[slug].context:
                    context[key] = presets[slug].context[key]
            config["context"] = context
            agent.config_json = config
        await db.commit()

asyncio.run(main())
PY
```

未验证：真实页面的端到端运行（登录后主流程、SSE 长连接）、依赖 `backend/test/.env.test` 的 HTTP integration 用例（本机未配置，整批 skip）。