# 同一条款多分块聚合成一条证据

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/power_spec/retrieval.py

## 问题

`PowerSpecSearchService.search` 把「一条证据」当作「一个检索分块」，同一条款命中多个分块时只保留第一个、其余静默丢弃，并且 `limit` 直接作用在分块上。电力规范语料里同一条款出现多个分块是常态，因此这一限制会稳定地丢掉用户需要的原文：

- 大表格条款被按字节上限切成多块（`implementations/milvus.py` 的插入切分），同一条款的其它分块从不进入证据；
- 条款级重建产出的**行级碎片语料**（一个文件一行，例如 `kb_frcfnr9ce6` 的 566 个 `RB-<标准>-T<表号>-R<行号>.md`）里，同一张表的多行分属不同文件，`limit=5` 只装得下同一条款的任意几行。

实测（`srs/power_rag_testset-v20汇总.xlsx` 50 条用例，问题文本单路召回、`recall_top_k=30`）：以「标准答案数值是否出现在最终证据里」为口径，修复前召回池 `recall@20 ≥50%` 有 13/31 条用例，但最终证据 `recall@final ≥50%` 只剩 5/31，`零召回` 16/31。即检索已经找到的答案，超过一半在选证据这一步被丢掉。

## 决策

1. **先按 `(standard_no, clause_no)` 聚合同一条款的分块，再按 `limit` 取条款。** 聚合后 `clause_body` 按分块在原文中的顺序拼接（顺序键先取 `chunk_index`，再取 `source_file`，使行级碎片按文件名里的表号/行号还原顺序），去重后合并。
2. **引用身份取原文顺序第一块。** 聚合后的 `document_id`/`clause_title`/`source_file` 来自正文开头那一段所在的碎片文档，避免引用键指向正文里排在后面的文件；`rerank_score` 取整组中重排模型的**原始输出**最大值，不带本地排序加分。
3. **可回溯性显式维护。** 聚合时把各分块的原始正文按同序写回 `metadata.raw_clause_body`，`aggregated_from` 记录参与聚合的文档，保证 `verify_evidence_projection` 仍能逐段回溯到拼接后的原文，而不是把拼接结果当成无法溯源的改写。

## 替代方案

- **只把 `limit` 从 5 调大**：组织子智能体的提示词上限受服务端 `max_model_len` 约束，把 20 个分块一起塞进去会超窗；且不改「一条证据 = 一个分块」的模型，同一条款仍会互相挤占名额。
- **保留去重、改为「同条款只留最高分分块」**：现状行为，即本记录要修的数据丢失。
- **按 `(standard_no, clause_no, clause_title)` 聚合**：行级碎片语料的标题常常是「表1 …」这类含表格标记的字符串，同一标题下可能横跨不同条款，键更不稳定；编号+标准号已足够定位条款。
- **重建语料为条款级分块，不改检索层**：粒度问题应在语料侧解决，但大表格被字节上限切分是入库机制的一部分，检索层仍需能把同一条款的多块装回一条证据；两者不冲突。

## 后果

- 同一条款的多分块从「丢弃」变为「一条证据装载全部相关分块」，卡片内容更完整；代价是单条证据正文可能明显变长，受 `MAX_EVIDENCE_BODY_CHARS` 截断保护。
- 聚合后能进入 `limit` 名额的是**条款**而不是分块，因此 `limit=5` 的含义从「5 个分块」变为「5 个条款」，噪声分块不再占用名额。
- 行级碎片语料下，一张表会合成一张卡片，`citation_key` 指向表内的某一行碎片文件——该引用在碎片语料里本来就是实现细节；标准编号、条款编号、条款标题仍是条款级事实。
- 顺序依赖 `chunk_index`/`source_file`：两者都缺失或无序时，聚合顺序退化为组内原顺序（即按分数序）。条款级分块的库有 `chunk_index`，行级碎片库有表号/行号文件名，实际语料两者都有。

## 验证

- 单元测试（`backend/test/unit/knowledge/test_power_spec_retrieval.py`，9 passed）：
  - `test_power_spec_search_merges_chunks_of_the_same_clause_in_source_order`：同条款两块按 `chunk_index` 还原顺序合并、`rerank_score` 取原始 0.91（不带本地加分）、不同条款不被合并——这个用例在旧实现下变红（旧实现只保留一块）。
  - `test_power_spec_search_merges_row_fragments_of_one_clause_across_files`：行级碎片跨文件按文件名行号合并，引用键指向正文开头那块。
  - `test_merged_clause_evidence_still_traces_back_to_source_text`：聚合结果仍通过 `verify_evidence_projection`（负向守护：若只拼接展示正文而不更新 `raw_clause_body`，该校验会报 `body_not_supported`）。
- 50 条用例检索口径回归（同一 harness、同一问题文本、`kb_frcfnr9ce6` 索引修复后）：
  - `recall@20` 不变（完整 4/31、≥50% 13/31、零召回 7/31），证明召回路径未被改动；
  - `recall@final` 由「完整 1/31、≥50% 5/31、零召回 16/31」变为「完整 4/31、≥50% 12/31、零召回 8/31」。
- 现有检索/校验/适配/渲染测试 33 passed；全套 `test/unit -m "not slow"` 通过。
