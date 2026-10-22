# 电力规范条款边界与重排输入护栏

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/power_spec/ingestion.py

## 问题

电力规范知识库的条款正文最长到 66,621 字符，重排（`local:Qwen3-Reranker-4B`）单路召回在 30 秒客户端超时下被打满，超时后整批按 0.5 兜底。三处缺陷叠加：

1. **附录被并入上一条正文。** 解析只把数字标题（`## 6.1 …`）当条款边界，`## 附录 A` 与附录子条款（`A.1`、`B.2.1`）不算边界，整段附录续在前一条正文之后。实测 `GB/T 14285-2023` 的 9.8 条款因此达到 24,827 字符，旧库 `GB/T 11022-2020` 的 12.3 达到 66,621 字符。
2. **目录条目本身是 Markdown 标题时拦不住。** 块级目录跳过规则要求目录标题与条目不同级；新清洗文档的条目与目录标题同级（`### 14.2 有限空间作业 26`、`## 附录A（规范性）标示牌样式 27`），目录文字因此并进正文；页码写在下一行的跨行条目（`## 附录 D（资料性） …` + `（IVPD）…说明 ……  78`）连单行判据也识别不到。
3. **重排送排的是条款全文。** 同一批 30 条文档、最长 24,871 字符时，单路召回实测 51–52 秒；截断到 600 字符后同一批次 1.7–2.5 秒。超时后 `acompute_score` 把整批写成 0.5 并继续，排序与相关度一起失真且无告警。

另外，标准号模式不接受分部号：`GB/T 1094.2—2013` 匹配失败，解析回退到正文引用的 `GB/T 13499—2002`，卡片会写错编号。

## 决策

1. **附录是条款边界。** `附录 X` 标题截断上一条正文；附录子条款（`A.1`、`B.2.1`）作为条款产出；附录块自身只在没有子条款时成条，作为容器时不重复成条。
2. **目录条目按内容识别。** 带引导点的 Markdown 标题，或结尾页码且相邻行也是目录条目的标题，不产出条款，但仍作为边界截断上一条正文；页码落在下一行的跨行条目按同样的判据识别。仅凭结尾数字不判定目录：`15.2.2 … 见表 49` 这类正常条款标题必须保留。
3. **重排输入按字符截断。** `RERANK_MAX_DOCUMENT_CHARS`（默认 600）截断每条送排文档；整批失败时输出一条「Rerank 降级」告警，兜底分不再静默发布。
4. **标准号统一口径。** 保留原文是否带 `/T`，统一编号前空格和年份前的 ASCII 连字符，并保留分部号（`DL/T 409-2023`、`GB/T 1094.2-2013`、`GB 50054-2011`）；历史卡片保持原值，新旧格式可以并存。
5. **用新库文档重建知识库。** 以 `kb_rf26q1t7bt`（M1-C004-ISOLATION-TEST-20261013）的 9 份清洗后 markdown 重建 `kb_9jttazquid`，验证通过后删除旧文件。

## 替代方案

1. **只换数据不改解析**：新库同样把附录并入上一条正文（最长 24,827），换库后仍会打满重排超时。
2. **只加边界不产出附录条款**：`GB/T 11022-2020` 附录 F 这类没有子条款的巨型表格（32,609 字符）会被整块丢弃，附录表失去可检索性。
3. **调大重排超时**：不改变单条文档过大造成的耗时，只把超时点后移；超时后仍是 0.5 兜底。
4. **在分块层下调统一 chunk 字符上限**：影响所有分块预设的检索粒度，且必须全量重建才能验证；本次先在检索层设护栏，分块粒度留待单独评估。

## 后果

- 条款数 3,163 → 3,304（附录内容变为可检索），最长条款 24,871 → 14,477，`GB/T 14285-2023` 的 24,827 巨型条款消失（该文件最长 2,891）。
- 重排只看每条文档的前 600 字符：超长表格条款的尾部不参与排序，卡片正文仍取条款元数据全文，因此排序质量在超长表格题上可能变差，换来的是不再有 30 秒白等与 0.5 兜底。
- 截断在共享的 `BaseReranker` 层生效，所有知识库的 rerank 输入都受同一上限影响；其他知识库若依赖长 chunk 排序，需要调大 `RERANK_MAX_DOCUMENT_CHARS`，或后续把上限改为按调用方传入。
- 标准号写法变化影响此后所有新入库的电力规范卡片；历史卡片保持原值。
- 条款号出现非数字标识（`附录A`、`A.1`）；引用键仍是 `file_id#clause_no`，与既有卡片一致。
- 重建后该知识库的条款元数据、chunk 与 Milvus 实体全部来自同一次解析，解析规则再次变化时仍需整体重建。

## 验证

| 主张 | 证据 |
| --- | --- |
| 附录不再并入上一条正文 | 单测 `test_parse_power_spec_text_cuts_body_at_appendix_and_emits_appendix_clause`；真实文件 `GB/T 14285-2023` 最长条款 24,827 → 2,891 |
| 附录子条款成条且容器不重复 | 单测 `test_parse_power_spec_text_emits_appendix_subsections_without_container_clause`；9 份文件重复条款号 0 |
| 跨行目录条目不成条 | 单测 `test_parse_power_spec_text_ignores_multiline_toc_entry_with_appendix_prefix`；`GBT6451-2023` 的 6,552 字符伪条款消失，条款 484 → 483 |
| 同行目录标题不成条 | 单测 `test_parse_power_spec_text_ignores_toc_headings_with_page_numbers` |
| 正文首行以数字结尾不被误判 | 单测 `test_parse_power_spec_text_keeps_heading_whose_body_line_ends_with_number` |
| 正常条款标题以数字结尾不被误判 | 单测 `test_parse_power_spec_text_keeps_clause_title_ending_with_number`、`test_parse_power_spec_text_keeps_title_with_table_reference_number` |
| 无 `/T` 的强制标准不被补成 `/T` | 单测 `test_parse_power_spec_text_keeps_mandatory_standard_without_recommended_mark` |
| 重排送排输入被截断 | 单测 `test_overlong_documents_are_truncated_before_provider_request`，断言提供者收到的文档长度为上限 |
| 重排降级可观察 | 单测 `test_degraded_batch_is_reported_as_warning` |
| 分部号不被替换 | 单测 `test_parse_power_spec_text_keeps_part_number_instead_of_referenced_standard`；真实文件解析为 `GB/T 1094.2-2013` |
| 改动未回归 | `pytest test/unit/knowledge -k power_spec`：170 passed；`pytest test/unit -m "not slow"`：2928 passed，54 skipped；`ruff check package` 通过；改动文件 `ruff format --check` 通过 |
| 检索时延 | 同一问题在重建后知识库：问题路 2.4s、关键词路 1.8s、工具整体 4.6s；护栏前同库为 51–52s，旧库 12.8s/6.5s |
| 真实答案链路 | 条款字段校对 38.9s（3 条标题修复被接受，含新文件条款）、整理 → 审查 → 合并 213.3s，`answer_source=model`，12 张卡片 |
| 重建结果 | `kb_9jttazquid` 9 个文件、3,304 chunk、最长 chunk 14,524、9 份文件条款元数据全部 `ready=True` |

未验证：重建后尚未通过前端完成一次真实提问（本次用进程内工具与模型链路验证）；`ruff format package --check` 仍有 4 个与本改动无关的历史文件待格式化。
