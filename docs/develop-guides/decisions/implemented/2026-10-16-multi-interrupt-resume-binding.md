# 一轮多个同源中断恢复时按中断 id 投递恢复值

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/base.py

## 问题

`search_power_spec` 用 LangGraph 的 `interrupt()` 弹意图确认卡片。LangGraph 把**同一条
assistant 消息里的多个工具调用当作同一 superstep 的多个并行任务**执行，因此含两个子问题
的多跳题会让模型在同一轮里并行调用两次 `search_power_spec`，两个任务各自 `interrupt()`，
checkpoint 因而留下**多个待恢复中断**。

此时前端/客户端提交的是裸恢复值（`Command(resume="confirm")`）。LangGraph 只在 `resume`
是「dict 且所有 key 都是 xxh3_128 十六进制摘要（即中断 id）」时才当作按中断寻址的映射，
否则一旦 `len(pending_interrupts) > 1` 就直接抛错：

```
RuntimeError: When there are multiple pending interrupts, you must specify the
interrupt id when resuming.
```

整次 Run 因此 `failed`，用户看到的是运行错误而不是答案。

实测（`srs/power_rag_testset-v20汇总.xlsx` 50 条用例，重启前后的两轮共 21 次运行失败里）：

- 失败线程 `5442032c-076a-49d1-845f-46a67d81ac19` 的 `checkpoint_writes` 中
  `channel='__interrupt__'` 有 **2 行、`task_id` 不同**（`f0ec4c02-…` 与 `e8b7b2d4-…`），
  载荷都是 `power_spec_intent_confirmation`，但问题文本不同（一条问「发变组A级检修…
  变压器绕组全部更换后…」，另一条问「220kV发电机组-变压器组纵联差动保护…」）；其他线程
  都只有 1 行。
- 集中在多跳组：`MULTI_05`/`MULTI_10`/`MULTI_15` 稳定复现（70～2455 秒后失败）。

## 决策

**在恢复入口把裸恢复值绑定到每一个待恢复中断上。** `BaseAgent.stream_resume_with_state`
是所有恢复链路的唯一入口（`services/chat_service.py` 的 `stream_agent_resume` 传的就是
`Command(resume=...)`），因此在这一层加一步 `_bind_resume_to_pending_interrupts`：

1. 只处理 `Command` 且 `resume` 不是 `dict` 的情况；已是 dict 的恢复值原样透传，不改写既有
   调用方的语义。
2. 用 `graph.aget_state({"configurable": {"thread_id", "uid"}}).interrupts` 读取待恢复中断
   （该字段是各任务中断的展平列表）；读取异常只记 warning 并回退到原行为，不让读取失败
   阻断恢复。
3. **仅当待恢复中断数 > 1 且它们的载荷声明了同一个 `source` 时**才改写成
   `Command(resume={interrupt.id: value})`。语义等价于用户对这些卡片做了同一选择。
4. 其余情况（单个中断、来源不一致、无 source）保持原行为不变。

## 替代方案

- **禁止并行工具调用（`parallel_tool_calls=false`）**：需要把它加进
  `models/providers/service.py` 的 `ALLOWED_EXTRA_BODY_FIELDS` 白名单并按模型写
  `request_body_overrides`，或用中间件改写模型请求；它只避免出现多中断，不修复「已经是
  多中断」的存量线程，且对全部 Agent 改变工具调用行为。本记录不采用；作为降低多跳题
  重复跑整条流水线成本的后续项另行评估。
- **只在前端/驱动侧按中断 id 提交**：`resume` 需要携带中断 id，第三方与 CLI 调用方仍会
  踩同一个坑，且「用户确认一次」的语义被推给每个调用方实现。不采用。
- **改工具契约，不在工具里 `interrupt()`**：把确认挪出图节点是更大的改动，且当前意图确认
  需要暂停图状态。不采用。
- **让两个并行工具调用只中断一次**：工具无法知道同一 superstep 里是否还有别的调用。不采用。

## 后果

- 多跳题的两次意图确认由同一次恢复完成；用户不再看到运行错误。
- 恢复路径多一次 `aget_state` 读取（每个恢复 Run 一次），仅在有 `Command(resume=...)` 时发生。
- 来源一致性判据是「所有中断都在 `value` 里声明了同一个非空 `source`」，因此同时挂起
  「意图确认」与「工具审批」这类异源中断时仍按原行为报错——本记录不去猜这种歧义。
- 并行工具调用仍会让多跳题把整条检索/整理/审查流水线跑两遍，单题耗时可达 1000～2400 秒；
  这是独立问题，不在本记录范围内。

## 验证

- 单元测试 `backend/test/unit/agents/test_base_resume_pending_interrupts.py`（6 passed）：
  - `test_multiple_same_source_interrupts_are_bound_by_interrupt_id`：真实 `StateGraph` +
    `InMemorySaver`，一轮并行两次 `interrupt()`。**负向守护**：先断言裸值恢复必然抛
    `RuntimeError: multiple pending interrupts`（复现线上缺陷），再断言按 id 绑定后两个任务
    分别拿到 `confirm` 并跑完。
  - `test_single_interrupt_keeps_bare_resume_value`：单中断不做改写且仍能跑完。
  - `test_different_source_interrupts_keep_bare_resume_value`：异源多中断不改写。
  - `test_resume_value_already_keyed_by_interrupt_id_is_untouched`：已按 id 组织的 dict 透传。
  - `test_state_read_failure_falls_back_to_original_command`：`get_graph` 抛错时回退为原命令。
  - `test_non_resume_input_is_passed_through`：非恢复输入原样返回。
- 回归：`test/unit/agents` + `test/unit/services` 共 1038 passed。
- 端到端：`MULTI_05`/`MULTI_10`/`MULTI_15` 重跑待补（修复前的失败原因文本已固定为上述
  `RuntimeError`，可据此核对）。
