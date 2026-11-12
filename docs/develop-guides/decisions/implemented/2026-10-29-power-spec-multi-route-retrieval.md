# 电力规范检索的多路召回与候选宽度

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/power_spec/tool.py

## 问题

电力规范检索只按模型改写后的一个问题加一路关键词召回，`recall_top_k=30`、`bm25_top_k=30`、`final_top_k=20`，检索层最多取 12 条证据。用户问题包含两个显式子问题（问题1./问题2.）时，长问题在稠密检索里会被其中一个子话题主导，另一个子问题的正确条款完全进不了候选池；实测「500kV 变压器注油后静置时间 / 局放超标结合 DGA 判断」一题，第一个子问题的静置时间条款没有出现在前 24 条候选中。

同一时期的内置「智能助手」通过 `query_kb` 连续发起多轮不同角度的检索，再用模型综合，因此用户观察到的检索范围更大、耗时更长。2026-10-29 的真实对话里，「110kV 电力变压器注意事项」在电力检索中只召回 5 条候选、输出 5 张卡片，其中 2 张是章节标题/图示负向内容；同一问题在智能助手中用十余次不同角度查询得到 3539/5747 字的实用回答。电力规范检索工具需要在不改变“一次确认、一次工具调用”产品流程的前提下，补上单次调用内的检索广度和多问题覆盖。

## 决策

1. **多路主召回**：一次工具调用内按“改写问题、原始问题、显式子问题、宽问题角度”分别执行 `executor.aquery`，最多 7 路。显式子问题只识别 `问题1.`、`1.` 这类标记，不把 `1.5Um`、`10kV、35kV` 当成问题编号。原始问题路线保留用户原话，避免模型改写漂移直接丢掉正确条款。
2. **宽问题角度扩展**：问题包含“注意事项、有哪些、如何检查/处理、要求、内容、措施”等宽问法、且关注维度不超过 1 个时，按设备与电压拆出安装与交接试验、运行巡视与维护、检修与预防性试验、保护与监测、冷却/绝缘油/接地等角度，每个角度单独召回；角度数量由 `POWER_SPEC_ANGLE_ROUTES` 控制，默认 5。没有设备类型时不扩展，避免凭空拼查询。
3. **召回宽度回到知识库能力**：`recall_top_k=50`、`bm25_top_k=50`、`final_top_k=30`，与知识库自身的 hybrid 配置一致；工具不再把候选池压得比 `query_kb` 更窄。
4. **证据上限放宽到 20 条**：`MAX_EVIDENCE_CLAUSES` 从 12 改为 20，覆盖排序在 13–20 名、但确实回答问题的条款；条款正文很短，prompt 成本可控。
5. **路线命中加权**：同一 chunk 被多路召回时保留最高重排分，并在 RRF 名次融合里按命中路线数加 0.001–0.002 的小权重；单路命中仍然可以进入候选，不因少一路而被丢弃。
6. **关键词路保持 floor 约束**：关键词路独有条目仍被压到问题路最低分以下，原有防“关键词路劫持排序”的约束不变。
7. **降级可观察**：部分主路线失败时保留其余路线结果，并把“N 路召回失败”写入工具结果的 `warnings`；全部主路线失败才抛错，不静默返回空结果。

## 替代方案

1. **只调大 top_k**：候选池变大但长问题仍被一个子话题主导，多问题覆盖不会改善；实测只把 30/30/20 放宽到 50/50/50，56 个金标条款的命中从 35 升到 37，收益有限。
2. **开启图检索**：当前 `use_graph_retrieval` 路径对电力规范是死路径，`graph_search` 只从同一路结果里筛 `graph_score`；开启它不会增加候选，只会增加配置误导。
3. **改成多轮 Agent 循环**：最接近「智能助手」的行为，但会重复弹出意图确认、改变发布门与运行归属，属于独立的产品决策；本次先在一次工具调用内完成多路召回。
4. **完全去掉问题维度硬过滤**：会让完全无关的条款进入卡片，损害精度；本次保留硬过滤，实测召回池与最终证据的命中数相同，硬过滤不是当前瓶颈。
5. **只放宽证据上限到 20、不改召回路线**：对单跳题有效，但多问题题仍缺第一子问题的候选。

## 后果

- 单次检索最多执行 7 路主召回加 1 路关键词召回；宽问题的角度扩展会增加重排调用与检索耗时，但相对条款字段校对与整理/审查阶段（数十秒到百秒级）仍是小头。
- 进入条款校对和整理阶段的证据最多 20 条，模型调用成本与延迟上升；整理/合并阶段仍以整理者返回的条款为准，审查不再按相关性裁剪。单条正文仍按 `MAX_EVIDENCE_BODY_CHARS` 截断，20 条同时是超长表格时可能触及子智能体上下文，需要按实际语料观察降级日志。
- 多问题与比对类问题的候选覆盖提升；宽问题的角度扩展会带入更多相邻条款，排序仍由重排分与 RRF 决定，角度关键词需要按真实问题继续调优。
- 表格行问题仍依赖条款元数据把表格并入所属条款；如果表格被切到独立 chunk 且无法映射到条款，检索层不会凭空补出该条款。
- 未改变意图确认、结果缓存、发布门和“一次真实检索”的对外行为；同一轮重复调用仍返回缓存结果。

## 验证

| 主张 | 证据 |
| --- | --- |
| 显式子问题被拆成独立路线 | `test_subquestion_routes_split_explicit_multi_question_markers` |
| 小数与电压列表不被误拆 | `test_subquestion_routes_ignore_decimal_and_voltage_lists` |
| 路线顺序与上限正确 | `test_retrieval_routes_keep_rewrite_original_and_subquestions` |
| 宽问题按设备与电压拆出角度 | `test_broad_question_expands_to_device_angles`、`test_broad_question_routes_include_angles` |
| 具体数值问题不扩展角度 | `test_specific_numeric_question_does_not_expand_angles` |
| 无设备时不凭空拼角度 | `test_broad_question_without_device_does_not_expand_angles` |
| 角度数量受配置控制 | `test_angle_routes_are_capped_by_configuration` |
| 多路同 chunk 取最高分并计命中数 | `test_merge_primary_results_keeps_max_score_and_counts_route_hits` |
| route_hits 穿过适配层参与融合加权 | `test_adapt_vector_results_carries_route_hits_for_fusion_weighting`（适配层丢键即红）+ `test_multi_route_hits_boost_fusion_rank`（名次相邻时三路命中压过单路） |
| 关键词路仍不越过问题路 | `test_keyword_only_items_never_outrank_the_question_path` |
| 关键词路失败仍保留问题路结果 | `test_keyword_recall_failure_is_visible_in_warnings` |
| 相关单元回归 | `python -m pytest test/unit/knowledge/test_power_spec_result_merge.py test/unit/knowledge/test_power_spec_retrieval.py test/unit/knowledge/test_power_spec_answer_organization.py test/unit/knowledge/test_power_spec_tool_intent.py -q`：65 passed |
| 宽问题真实检索冒烟 | 「110kV 电力变压器注意事项」：`routes=6`、`angles=5`、`vector_count=16`（改动前同问题为 5 条候选） |

检索层人工评测使用仓库内 50 题测试集（`srs/power_rag_testset-v20汇总.xlsx`）中的 40 道可回答题、56 个金标条款。严格字面匹配下，当前单路流程命中 35/56，放宽候选池后 37/56；表格行引用按数值签名复核后，MULTI_02、MULTI_08、COMP_01 的表格内容实际已在证据中。子问题路线把 MULTI_03、MULTI_15 从 0/2 提升到 1/2。该评测是检索层脚本，不是端到端答案准确率；最终卡片数还受整理/审查阶段裁剪影响，未在本记录中声称端到端提升。

## 相关决策

- [电力规范检索重排可选与 RRF 排序](2026-10-21-power-spec-rerank-optional-and-rrf.md)：保留关键词路 floor 与 RRF 名次融合。
- [电力规范条款分块聚合](2026-10-15-power-spec-clause-chunk-aggregation.md)：同条款多分块合并规则不变。
