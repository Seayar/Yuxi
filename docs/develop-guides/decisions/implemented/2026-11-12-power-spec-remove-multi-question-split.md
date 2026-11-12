# 移除电力规范检索的多问题拆分与逐题回答

状态：implemented
类型：simplification
Owner：backend/package/yuxi/knowledge/power_spec/intent.py

## 问题

为提升测评集多跳题（`MULTI_*`）的答案准确率，意图识别曾改成先把一次输入拆成至多 6 个子问题：`PowerSpecIntentResult.sub_questions`、`PowerSpecQuery.sub_queries`、逐题 `answers`、`### 问题回答` 摘要，以及图谱的 `#问题N` 子问题变体与 `分组` 轮转。这套能力只服务多跳题型；普通用户输入只有一个问题，代价却落在每次检索上：意图提示词要求模型先判断问题数量再逐题识别字段，确认卡片换成分条计划；整理与合并提示词要求逐题输出 `answers`；图谱 schema 多一个 `分组` 属性，运行时要按分组重排 pinned 证据。

## 决策

1. 意图识别只输出一个问题：删除 `sub_questions` 提示词段与字段、`PowerSpecIntentQuestion`、`_build_sub_queries`、`_split_explicit_sub_questions`；`PowerSpecQuery` 不再携带 `sub_queries`。
2. 确认卡片恢复单计划文案，确认计划不再编码子问题；恢复路径只重建顶层字段。
3. 主召回路线只剩改写问题、原始问题与宽问题角度；删除 `_subquestion_routes` 与模型子问题路线。
4. 整理、审查、合并只处理条款：删除 `QuestionAnswer`、`OrganizedDraft.answers`、逐题提示段落与 `render_question_answers`，`apply_organized_clauses` 不再拼接 `### 问题回答` 摘要。
5. 测评图谱只保存完整问法与问法变体：删除 `#问题N` 子问题变体、`分组` 边属性、`group`/`matched_group` 字段，以及 `_balance_pinned_evidence`、`_citation_sort_key`；pinned 证据按 `排序` 排列。
6. 保留多中断同源恢复绑定、审查忠实性收敛、宽问题角度扩展与 `route_hits` 加权。后三项由多跳驱动但服务通用路径，前者保护所有同源并行中断。

## 替代方案

- **只改提示词、保留解析代码**：意图模型仍可能输出 `sub_questions`，旧字段继续出现在确认计划与日志中，形成两套真相。不采用。
- **保留配置开关**：当前没有第二个 consumer 需要这套能力，开关只会让已删除的分支长期留在运行时路径上。
- **保留 `answers` 但不再渲染**：整理与合并仍要为逐题结论付出结构化输出与 token，且没有发布出口。

## 后果

- 多问题输入只得到一条改写问题与一份主召回路线；第二个子问题的独立召回、独立结论和分组优先不再存在，`MULTI_*` 类题型的答案级表现预计回落到拆分前水平。多问题是否被全部覆盖取决于模型改写，代码不再校验。
- 意图、整理、合并三步的提示词更短，确认卡片只展示一个计划，checkpoint 中的确认计划更小。
- 存量 checkpoint 里带 `sub_questions` 的确认计划仍可解码：`query_from_confirmed_plan` 只读顶层字段，多余键被忽略。
- 已写入 `power_spec_qa` 的问题变体可能仍含 `#问题N` 条目与 `分组` 属性。运行时完全不读取 `分组`，旧数据里的该键被忽略；但 `#问题N` 仍是一条普通问法，会继续参与命中。要让子问题问法彻底不再命中，需用修改后的种子脚本以 `--replace` 重灌图谱。

## 验证

旧能力不存在：`PowerSpecIntentResult.sub_questions`、`PowerSpecQuery.sub_queries`、`PowerSpecSubQuery`、`SUBQUESTION_SPLIT_PATTERN`、`OrganizedDraft.answers`、`### 问题回答` 渲染、图谱 `分组`/`matched_group` 与 `_balance_pinned_evidence` 均已删除，源码与机制文档不再引用。

重新引入条件：出现真实用户输入需要逐题独立召回、独立结论与逐题引用的业务需求，且已确认一条改写问题无法覆盖这些要求；重新引入时一并评估意图识别、确认卡片与图谱 schema 的耦合成本。

| 主张 | 证据 |
| --- | --- |
| 意图只输出一个问题，确认计划按单计划回放 | `test_multi_question_input_collapses_into_single_plan`（负向：显式“问题1./问题2.”输入仍只有一个计划且计划无 `sub_questions`）、`test_confirmation_plan_round_trips_single_question` |
| 主召回路线只剩改写问题与原始问题 | `test_retrieval_routes_keep_rewrite_and_original_question`（负向：显式编号输入不额外成路） |
| 宽问题角度扩展仍然生效 | `test_broad_question_expands_to_device_angles`、`test_angle_routes_are_capped_by_configuration` |
| 图谱 pinned 证据按 `排序` 置前 | `test_graph_hit_is_pinned_before_higher_scored_vector_evidence`、`test_order_graph_evidence_first_orders_graph_evidence_by_rank`、`test_apply_organized_clauses_orders_two_graph_cards_by_rank`、`test_find_hit_orders_citations_by_rank` |
| 整理契约没有逐题 `answers`，答案前没有摘要 | `test_organized_draft_contract_drops_per_question_answers`（负向：`OrganizedDraft` 无 `answers` 字段且多写的键被忽略）、`test_apply_organized_clauses_publishes_cards_without_summary_prefix` |
| 图谱不再写入或读取分组字段 | `test_graph_hit_has_no_subquestion_group_fields`（负向：数据类无 `matched_group`/`group`）、`test_question_props_contain_all_matching_keys`（命中键只含 `key`/`text`/`type`）、`test_seed_graph_writes_schema_nodes_and_edges`（Schema 与边写入不含 `分组`/`grp`）、`test_legacy_group_key_does_not_block_matching`（旧数据多余键被忽略） |
| 依据条款的“1./2.”编号仍被剥离 | `test_section_citations_strip_leading_item_numbering` |

已执行：`py -3 scripts/verify_engineering_contracts.py` 通过（141 decisions / 5 workflows / 4 agents files / 194 docs / 27 routers / 252 web sources）。未执行：`pytest test/unit/knowledge -k power_spec` 与真实检索链路，本机没有项目 Python 依赖且 Docker 不可达；`py -3 -m unittest scripts.test_verify_engineering_contracts` 的 62 项里仅 `test_decision_owner_symlink_cannot_escape_repository` 因 Windows 无符号链接权限报错，其余通过；`cd docs && pnpm run build` 未运行。

## 相关决策

- [电力规范多问题意图拆分与逐题回答（已归档）](../archived/2026-11-02-power-spec-multi-question-intent.md)：本记录取代该能力。
- [电力规范测评图谱问答](2026-10-30-power-spec-qa-graph.md)：图谱主关系保留，`分组` 与子问题变体的部分被本记录取代。
- [电力规范检索的多路召回与候选宽度](2026-10-29-power-spec-multi-route-retrieval.md)：宽问题角度扩展与 `route_hits` 加权保留，显式子问题路线被本记录取代。
