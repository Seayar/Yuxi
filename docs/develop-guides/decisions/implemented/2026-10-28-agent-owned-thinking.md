# 智能体拥有的四步思考过程

状态：implemented
类型：feature
Owner：backend/package/yuxi/agents/thinking.py

事件与发射器在 `agents/thinking.py`；步骤注册表与类型在 `thinking.py`，步骤清单的注册点在两个知识模块的 `contracts.py`。

## 问题

电力规范检索与故障溯源需要在聊天流里展示各自的四步处理过程。早期设计把步骤数量、标题和状态写在平台侧（`chat_service` 与通用 SSE 描述里），产生两个后果：平台反向拥有智能体行为，且工具内部的意图识别、条款校对、整理与审查模型输出会作为顶层步骤或正文漏到用户界面。

## 决策

前端思考过程固定为每个智能体最多四个顶层步骤，步骤清单由智能体模块注册，平台只做通用传输、校验与持久化。

- 注册与类型在 `yuxi/thinking.py`：`ThinkingStepSpec` 描述 `step_id`、标题与说明，`register_thinking_steps` 拒绝空清单、重复 `step_id` 和超过四步的注册。
- 发射与校验在 `agents/thinking.py`：`ThinkingEmitter` 以 `yuxi.thinking_step` 自定义事件写出 `{version, agent, step_id, step_index, title, state, content, data, started_at, finished_at, elapsed_ms, error}`；状态取 `started / waiting / completed / failed / skipped`。
- 步骤归属：电力规范在 `knowledge/power_spec/contracts.py` 注册四个步骤，故障溯源在 `knowledge/fault_trace/contracts.py` 注册四个步骤；两者同时由 `agents/presets/*` 注册到 Agent slug。
- 平台侧：`chat_service` 只接受已注册 slug 的步骤，用注册表标题覆盖事件标题并校验 `step_index`，把事件转发为 `status="thinking"` 的分片；整轮步骤在 Run 终态提交时写入输出消息的 `extra_metadata.thinking_steps`，中断与异常分支的部分输出同样携带已产生的步骤。
- 内部模型隔离：内部模型调用与子智能体输出以 `yuxi_internal_model` 标记（metadata 或 tags），`chat_service` 过滤这些分片，使其不进入主回答流。
- 前端：`web/src/components/ThinkingTimeline.vue` 渲染步骤；流式期间思考面板绑定当前 `activeRunId` 命中会话，不回落到最后一条 AI 消息；流结束时残留的 `started`/`waiting` 步骤标记为 `skipped`。

当前不强制 `data` 尺寸上限；如需限制，应在 `normalize_thinking_event` 增加校验并同步此记录。

## 替代方案

- **平台统一 7/8 步流程**：步骤与内部实现一一对应，用户无法按原型理解，且平台必须知道每个智能体的内部操作，已放弃。
- **前端按工具名自行推断步骤**：前端需要维护与后端一致的步骤语义，任一新增智能体都要改前端，已放弃。
- **只把思考过程存进日志**：用户无法看到处理进度，`waiting` 状态也无法驱动确认卡片，已放弃。

## 后果

- 新增智能体只需注册自己的步骤并在工具内发射事件，平台侧（`chat_service`）不需要改动。前端有一处例外：`web/src/utils/messageGrouping.js` 的 `THINKING_TOOL_NAMES` 目前列了 `search_power_spec`/`trace_fault`，用于隐藏重复的工具卡片；新增思考型智能体时要同步这份清单，否则会同时出现工具卡片与思考面板。
- 步骤清单成为前端契约：标题与顺序由后端注册表覆盖，前端伪造或错序的步骤会被拒绝。
- 整轮思考绑定在本 Run 的输出消息上，跨轮次不会复用上一轮的步骤。
- 事件写入失败只记录日志，不影响业务结果；流结束时的遗留步骤由前端收尾为 `skipped`。

## 验证

- 注册表与发射器：`backend/test/unit/agents/test_thinking.py`（注册约束、事件写出、快照）。
- 平台校验与持久化：`backend/test/unit/services/test_chat_service_thinking.py`、`backend/test/unit/services/test_chat_service_sync.py`（末态绑定、异常分支携带步骤）。
- 工具侧四步：`backend/test/unit/knowledge/test_fault_trace_tool.py`、`backend/test/unit/knowledge/test_power_spec_tool_intent.py`。
- 前端：`web/test/unit/thinkingSteps.test.js`、`web/test/unit/messageGrouping.test.js`。
- 命令：`docker compose exec api uv run --group test pytest test/unit -m "not slow"`；`docker compose exec web pnpm run test:unit`。
