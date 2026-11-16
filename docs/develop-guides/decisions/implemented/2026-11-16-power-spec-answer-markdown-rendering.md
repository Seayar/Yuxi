# 电力规范答案按 Markdown 渲染并停止 HTML 转义

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/power_spec/answer.py

## 问题

电力规范卡片正文来自 Markdown（表格、单元格 `<br>`、LaTeX）或解析出的 HTML 表格。`answer.py` 的 `_highlight()` 对正文调用 `html.escape(..., quote=False)`，把 `<br>`、`<table>` 等结构写成 `&lt;br&gt;`、`&lt;table&gt;` 文本；如果源正文本身已含实体，还会二次转义为 `&amp;lt;br&amp;gt;`。

前端卡片用 `v-html` 渲染 `clause_body`，助手消息用 Markdown 渲染器渲染 `answer_markdown`。两者都不会把 `&lt;br&gt;` 还原成换行：卡片只显示原始竖线与 `<br>` 文本，助手消息的表格单元格显示 `<br>` 字面量；HTML 表格则整体退化为文本。

## 决策

1. `answer.py::_highlight()` 不再做 HTML 转义，只按 `<[^>]*>` 切分正文，在文本段插入 `<mark>`；词元命中标签名或属性时不改写标签。
2. `clause_body` 与 `answer_markdown` 保留原文 Markdown/HTML（表格、`<br>`、LaTeX），后端只注入 `<mark>`。
3. 前端 `SearchPowerSpecTool` 使用公共 `MarkdownPreview` 渲染 `clause_body`，与助手消息共用 markdown-it、KaTeX 与 DOMPurify；移除 `v-html` 和 `white-space: pre-wrap`。
4. 非 Web 消费者按原始 Markdown 文本读取同一字段，不再依赖 HTML 实体。

## 替代方案

- **只在前端 `html.unescape` 后再渲染**：网络协议里仍是被转义数据，非 Web 消费者拿到同样内容；二次解码扩大信任边界，不采用。
- **保留转义，后端把正文转成 HTML**：需要新增 Markdown→HTML 渲染与净化管线，和前端渲染器形成两套事实，不采用。
- **在 `_highlight` 里维护 HTML 白名单**：白名单要覆盖表格、`<br>`、LaTeX 相关标签与 OCR 噪声，仍是自建 sanitizer；前端已有 DOMPurify，不重复建设。

## 后果

- 卡片与助手消息能渲染 Markdown 表格、单元格 `<br>` 换行和 KaTeX，`<mark>` 高亮保留。
- XSS 防线从前端 `v-html` 的转义文本移到 `MarkdownPreview` 的 DOMPurify；`clause_body` 不再保证是无标签文本，任何直接拼 HTML 的新消费者都必须走 sanitizer。
- 调试面板、复制文本和 CLI 等纯文本消费者看到原始 `<br>` 标签而不是 `&lt;br&gt;` 实体；两者都不是可读换行，行为没有实质回退。
- `verify_evidence_projection` 的正文比较继续通过 `normalize_evidence_text` 做 `html.unescape` 与去除 `<mark>`，原始正文与展示正文的匹配规则不变。

## 验证

| 主张 | 证据 |
| --- | --- |
| `<br>`、`&` 和 Markdown 表格不再被转义 | `test_card_body_keeps_markdown_html_and_latex_for_frontend_rendering` |
| 高亮词元不改写 HTML 标签名或属性 | `test_highlight_does_not_insert_mark_inside_html_tags`（负向：`<table class="spec">` 原样保留） |
| 整理模型正文同样保留原文结构 | `test_apply_organized_clauses_keeps_model_body_html` |
| 卡片正文按 Markdown 渲染表格与单元格换行 | `web/test/unit/markdown_preview_fallback.test.js` 的“规范卡片正文的表格与单元格换行按 Markdown 渲染” |
| `<mark>` 高亮在 Markdown 渲染后仍然生效 | 同一测试文件的“规范卡片正文的高亮 mark 标签保留为 HTML” |
| 发布门仍只发布工具给出的 `answer_markdown` | `test/unit/services/test_chat_service_power_spec_scope.py` 7 passed |

已执行：`docker compose exec -T api uv run --group test pytest test/unit/knowledge -q -k power_spec` 为 232 passed、4 failed；失败项全部是 `[[PDF:...]]` 标记断言，来自工作区已存在的 PDF 标记临时移除，与本次修改无关。`test/unit/services/test_chat_service_power_spec_scope.py` 7 passed。`pnpm run test:unit` 363 passed。`pnpm run lint:check` 通过。`cd docs && pnpm run build` 通过。`py -3 scripts/verify_engineering_contracts.py` 通过（148 decisions / 201 docs）。`py -3 -m unittest scripts.test_verify_engineering_contracts` 62 项中 61 通过，1 项因 Windows 无符号链接权限失败，属于既有环境限制。`ruff check --ignore E501` 通过；完整 ruff 的 E501 来自工作区已存在的 PDF 标记 TODO 注释，与本次修改无关。

未执行：真实页面渲染与截图（没有可登录页面的视觉验证入口）；`pytest test/unit -m "not slow"` 全量回归。
