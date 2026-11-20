import assert from 'node:assert/strict'
import test from 'node:test'

import { createMarkdownRenderer } from '../../src/utils/markdown_preview.js'
import { replacePowerSpecPdfMarkers } from '../../src/utils/powerSpecPdf.js'

test('无代码高亮器时 Markdown 仍保留结构化渲染', () => {
  const renderer = createMarkdownRenderer({ themeName: 'github-light', highlighter: null })
  const html = renderer.render('# Skill\n\n```python\nprint(42)\n```')
  assert.match(html, /<h1>Skill<\/h1>/)
  assert.match(html, /<pre><code/)
})

test('规范卡片正文的表格与单元格换行按 Markdown 渲染', () => {
  const renderer = createMarkdownRenderer({ themeName: 'github-light', highlighter: null })
  const html = renderer.render('| 名称 | 说明 |\n|---|---|\n| 电流速断保护 | 第一行<br>第二行 & 备注 |')

  assert.match(html, /<table>/)
  assert.match(html, /第一行<br>第二行 &amp; 备注/)
})

test('规范卡片正文的高亮 mark 标签保留为 HTML', () => {
  const renderer = createMarkdownRenderer({ themeName: 'github-light', highlighter: null })
  const html = renderer.render('<mark>电流速断保护</mark> 正文')

  assert.match(html, /<mark>电流速断保护<\/mark>/)
})

test('报告中的服务图片 Markdown 渲染为带原路径的图片', () => {
  const renderer = createMarkdownRenderer({ themeName: 'github-light', highlighter: null })
  const html = renderer.render(
    '## 二、故障预测\n\n- 退化趋势：负荷率上升\n\n![预测图](/api/data/image?objectName=prediction-charts%2Foverhead_line%2Fchart.png)'
  )

  assert.match(html, /<\/ul>\s*<p><img src="\/api\/data\/image/)
  assert.match(
    html,
    /<img src="\/api\/data\/image\?objectName=prediction-charts%2Foverhead_line%2Fchart\.png" alt="预测图">/
  )
})

test('规范卡片 PDF 标记渲染为可点击的下载链接', () => {
  const url = '/api/knowledge/databases/kb-1/documents/file-1/download'
  const renderer = createMarkdownRenderer({ themeName: 'github-light', highlighter: null })
  const html = renderer.render(replacePowerSpecPdfMarkers(`[[PDF:${url}]]`))

  assert.match(html, new RegExp(`<a href="${url}">下载 PDF 原文</a>`))
})

test('外部 PDF 标记不会生成携带登录凭据的下载链接', () => {
  const renderer = createMarkdownRenderer({ themeName: 'github-light', highlighter: null })
  const html = renderer.render(
    replacePowerSpecPdfMarkers('[[PDF:https://example.test/file.pdf]]')
  )

  assert.doesNotMatch(html, /下载 PDF 原文/)
  assert.doesNotMatch(html, /example\.test/)
})
