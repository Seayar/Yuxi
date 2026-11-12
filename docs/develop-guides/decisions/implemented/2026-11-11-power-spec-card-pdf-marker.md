# 电力规范卡片的 PDF 源文档标记

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/power_spec/answer.py

## 问题

前端需要从电力规范卡片的输出知道每张卡片对应哪份原始文档，才能请求并打开 PDF。卡片当前只有规范名称、标准编号、条款编号和正文，证据对象虽然有 `document_id`，对外输出却没有可请求的地址。前端与服务端约定的协议是在每张卡片末尾输出 `[[PDF:<url>]]`，URL 使用知识库文件的 MinIO 地址。

该变更由用户直接指定输出协议与地址来源，没有待裁决的实现分支，因此直接记录为 implemented。

## 决策

- `SpecClauseEvidence` 增加 `source_url`。检索层从 `knowledge_files.minio_url`（优先）或 `knowledge_files.path` 解析原始文档地址；`PowerSpecRepository.list_power_spec_extractions` 在同一次查询中返回它，向量召回与图谱、测评图谱命中共用 `build_clause_evidence` 的同一条投影。
- `render_srs_cards` 在四字段之后、每张卡片末尾追加一行 `[[PDF:<url>]]`；固定卡片与模型整理卡片走同一渲染入口。
- `items`（`spec_results`）保持四字段契约不变，标记只进入 `answer_markdown`。
- 地址为空、不是 http(s) 或同源相对地址，或含换行、`]]` 时不输出标记，避免破坏前端解析边界；跳过原因写日志。

## 替代方案

1. 在 `spec_results` 每项增加 `pdf_url` 字段。结构化消费更直接，但会改变已文档化的四字段卡片契约，且用户已经指定文本标记协议。
2. 生成 MinIO 预签名 URL。可访问私有对象，但当前客户端用内网 endpoint 生成签名（如 `minio:9000`），浏览器不可达，且链接有有效期、输出不稳定。
3. 使用后端鉴权下载接口 `/api/knowledge/databases/{kb_id}/documents/{file_id}/download`。需要鉴权头，且不是需求指定的 MinIO 地址。
4. 把地址并入 `clause_body`。会污染正文，并影响高亮与投影校验。

## 后果

- 卡片输出从四字段文本变成四字段文本加一行标记；`items` 不变。旧前端会看到原始标记文本，直到实现解析。
- URL 直接取文件记录的 MinIO 地址。若部署使用内网 host 或私有 bucket，前端能否打开取决于 MinIO 的网络可达性与 bucket 读权限；本变更不额外签名或代理。
- 没有原始文件地址的历史元数据不输出标记，不伪造链接。
- 地址不参与 `verify_evidence_projection` 和高亮，卡片正文的回溯语义不变。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 每张卡片末尾带自己的 PDF 地址 | 多卡片地址串位 | `power_spec/answer.py` | `test_power_spec_formatting.py::test_render_srs_cards_appends_pdf_marker_to_each_card`、`test_apply_organized_clauses_appends_each_card_own_pdf_marker` | 只带最后一张地址时第一个断言失败 | 通过 |
| 模型整理与图谱补卡也带标记 | 图谱补卡丢失地址 | `power_spec/answer.py` | `test_apply_organized_clauses_appends_pdf_marker_to_pinned_graph_card` | 图谱卡使用模型卡的地址时断言失败 | 通过 |
| 无地址或非法边界不输出标记 | 输出 `[[PDF:]]`、`minio://` 或提前闭合标记 | `power_spec/answer.py` | `test_render_srs_cards_omits_pdf_marker_without_source_url`、`test_render_srs_cards_omits_pdf_marker_that_breaks_marker_boundary`、`test_render_srs_cards_omits_pdf_marker_for_non_requestable_url` | 空地址或内部协议输出标记时断言失败 | 通过 |
| 证据从文件记录获得地址 | 查询未返回 `minio_url`/`path` | `repositories/power_spec_repository.py`、`power_spec/retrieval.py` | `test_power_spec_repository.py::test_repository_returns_source_url_for_card_pdf_marker`、`test_power_spec_adapters.py::test_adapt_vector_results_carries_source_url_for_pdf_marker` | `minio_url` 不再优先，或地址写回 `processing_params` 时断言失败 | 通过 |
| `spec_results` 契约不变 | 结构化载荷混入第五字段 | `power_spec/answer.py` | 既有 `test_format_power_spec_results_returns_only_four_card_fields` | 加入 `pdf_url` 字段时断言失败 | 通过 |

执行记录：Windows 临时虚拟环境运行 `test_power_spec_formatting.py`、`test_power_spec_adapters.py`、`test_power_spec_repository.py`、`test_power_spec_retrieval.py`、`test_power_spec_clause_review.py`，62 passed。未运行 Docker 内完整 unit/integration/E2E 与真实页面验证，原因是本机 Docker daemon 未启动。
