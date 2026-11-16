import assert from 'node:assert/strict'
import test from 'node:test'

import { createMarkdownRenderer } from '../../src/utils/markdown_preview.js'

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
