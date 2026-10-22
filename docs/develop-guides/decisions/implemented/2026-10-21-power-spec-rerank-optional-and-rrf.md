# 电力规范检索重排可选与 RRF 排序

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/power_spec/retrieval.py

## 问题

知识库没有配置 `reranker_model` 时，工具直接返回「该知识库未配置重排模型，无法检索电力规范」，检索整体不可用。重排是可选能力，缺配置时仍应返回可追溯的条款，只是排序质量下降。

同时，重排分数（logits）与召回分数（0–1 相似度）尺度不同，直接相加会互相压制。改为按名次融合（RRF）后，人工测试的检索准确率明显提升；但关键词路独有条目的 `rerank_score` 原本会被压到问题路最低分，其召回分数没有被压低，RRF 会把它重新抬到问题路之上，重新引入「10kV 开关柜装设接地线」事故中的关键词路劫持。

## 决策

1. **重排可选**：缺少 `reranker_model` 时两路召回都传 `use_reranker=False`，继续检索；工具结果增加 `warnings`（「未配置重排模型，已按召回分数排序」），日志用 warning 记录。检索为空时仍返回「未检索到资料」，不把降级伪装成配置错误。
2. **RRF 名次融合**：以 `RRF_K=60` 融合 `rerank_score` 与 `match_score` 两路名次；问题维度命中词保留小幅加权。
3. **关键词路仍受 floor 约束**：`_merge_results` 已把关键词路独有条目的 `rerank_score` 压到问题路最低分；检索层让这类条目只按已压低的分数参与融合，召回名次固定排在问题路之后，RRF 不得把它抬回问题路之上。
4. **清理旧契约**：删除 `RETRIEVAL_NOT_CONFIGURED_MESSAGE`、`PLAIN_TOOL_MESSAGES` 中的对应值、电力规范预设与 SKILL 中的透传要求、Web 组件的文案映射，并同步检索机制文档。
5. **重排输入截断**：送排文档按 `RERANK_MAX_DOCUMENT_CHARS` 截断，批次失败输出「Rerank 降级」告警；该护栏的排序影响见[电力规范条款边界与重排输入护栏](2026-10-19-power-spec-clause-boundary-and-rerank-guard.md)。

## 替代方案

1. **保留配置错误**：实现最简单，但一个缺失的可选配置会让整条检索不可用，与「可选能力降级」的约定冲突。
2. **保留旧的可加公式**：重排与召回分数尺度不同，需要额外调权重；人工测试显示 RRF 准确率更高。
3. **RRF 不再约束关键词路**：会让关键词路独有条目凭自身查询的相似度越过问题路，重新触发已记录的生产事故；因此保留 floor。
4. **只改后端、不改提示词和文档**：模型会被要求透传一个永不出现的句子，排障文档也会指向不存在的配置错误，不能只改一半。

## 后果

- 缺重排模型的知识库仍能返回卡片，但排序只依赖召回分数，准确率低于有重排时；`warnings` 只在工具载荷与日志中可见，前端不展示技术细节。
- RRF 改变了候选排序；旧的可加公式与 `MAX_EVIDENCE_CLAUSES` 的调参结论不再直接适用，人工测试是当前的准确率依据，尚未形成可重复的 50 题评测记录。
- 关键词路独有条目仍会进入候选，但两路分数都不高于问题路最低分，不会占据问题路排名之上。
- 旧文案与常量被删除；历史会话中的纯文本结果不受影响，仍按 `PLAIN_TOOL_MESSAGES` 中的现存值原样透传。

## 验证

| 主张 | 证据 |
| --- | --- |
| 缺重排模型仍检索并返回 warnings | `test_missing_reranker_keeps_results_and_reports_warning`：两路 `use_reranker=False`，`warnings` 含降级说明，`spec_results` 非空 |
| 关键词路不越过问题路 | `test_keyword_only_items_do_not_outrank_question_path_under_rrf`：关键词路独占条目 `match_score` 更高时仍排在问题路之后 |
| RRF 不把 0.0 当缺失 | `test_rrf_treats_zero_match_score_as_present`：`match_score=0.0` 的条目按真实相似度参与名次 |
| 文档与提示词不再要求透传旧文案 | `rg RETRIEVAL_NOT_CONFIGURED_MESSAGE` 只命中历史决策记录 |
| 改动未回归 | `pytest test/unit -m "not slow"`：2928 passed，54 skipped；`ruff check package` 通过；前端 lint/build、文档构建通过 |

人工测试：开启重排与关闭重排的对照显示，RRF 排序的检索准确率明显高于旧的可加公式；本次没有保存逐题评测记录，准确率结论以人工测试为准。

## 相关决策

- [电力规范检索的模型参与答案组织](2026-09-15-power-spec-model-organized-answer.md)：其中「缺重排模型返回专用文案」被本记录部分取代。
- [对齐 Yuxi 模型配置与新模型服务拓扑](2026-10-10-model-serving-topology-realignment.md)：正常配置下仍使用 reranker，只有缺配置时降级。
- [电力规范条款边界与重排输入护栏](2026-10-19-power-spec-clause-boundary-and-rerank-guard.md)：重排送排截断与降级告警。
