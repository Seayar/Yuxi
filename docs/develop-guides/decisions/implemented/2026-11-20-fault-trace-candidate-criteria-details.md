# 故障溯源推理依据展示候选判据明细

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/fault_trace/answer.py

## 问题

故障溯源未定位原因时，③ 推理依据只输出“全部候选原因的实时判据均不成立”。已有成立原因时，模型虽然会提到“其他候选原因不成立”，但提示词只提供原因名和状态，最终文本没有 rejected/unknown 候选的表达式、实时值、求值结果和缺数原因。用户无法判断候选原因为什么被排除，也无法识别实时数据质量问题。

## 决策

由确定性报告为当前 ③ 范围内的每个 rejected/unknown 候选逐原因列出全部触发判据：

- 展示触发条件名称和表达式；
- 按表达式引用属性展示实时值及单位，缺失属性明确写“缺失”；
- 展示 `true`/`false`/`unknown` 求值结果；
- 对 `missing_key`、`redis_unavailable` 等非正常结果展示原因；
- 定性条件没有表达式时明确标注“无表达式，无法求值”。

完全未定位时，该明细作为完整推理依据，仍不采用模型推理文本。已有成立原因时，明细追加在模型推理之后，模型不能省略或改写这些判据事实。

## 替代方案

- **只列表达式不列值**：用户仍无法核对实时数据为什么导致 false，拒绝。
- **只列 rejected 原因**：unknown 和缺数原因不可见，无法区分“判据不成立”和“数据缺失”，拒绝。
- **让模型组织未定位推理**：当前没有定位原因作为证据门锚点，模型文本容易省略或改写判据，拒绝。
- **只依赖模型列举其他候选**：已定位场景的现有提示词只有原因名和状态，没有表达式与实时值，拒绝。

## 后果

- 推理依据会明显变长，但包含可复核的实时判据，不再只有结论或成立原因。
- 已定位场景保留模型对成立原因的解释，并追加确定性未成立候选明细。
- 报告 schema 不变，`location.reasoning` 与 `answer_markdown` 内容更完整。

## 验证

- `docker compose exec -T api sh -lc 'python -m pytest test/unit/knowledge/test_fault_trace_*.py test/unit/services/test_yolo_service.py test/unit/services/test_chat_service_fault_trace_scope.py -q'`：190 passed。
- `test_payload_unlocated_reasoning_lists_expressions_and_realtime_values`：断言完全未定位时 false、unknown、表达式、实时值、单位和 `missing_key` 均出现在 `location.reasoning` 与 Markdown 中。
- `test_payload_located_reasoning_lists_rejected_candidate_criteria`：断言已有成立原因时 rejected 候选仍追加表达式、实时值和 false 结果。
- `git diff --check` 与工程契约检查通过。
