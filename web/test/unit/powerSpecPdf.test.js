import assert from 'node:assert/strict'
import test from 'node:test'

import {
  extractPowerSpecPdfUrls,
  isKnowledgeDocumentDownloadUrl,
  powerSpecPdfUrlForStandardNo,
  replacePowerSpecPdfMarkers
} from '../../src/utils/powerSpecPdf.js'

test('规范卡片按顺序解析各自的知识库下载地址', () => {
  const markdown = `### 规范卡片 1

- **规范名称**：电力变压器运行规程

[[PDF:/api/knowledge/databases/kb-1/documents/file-1/download]]

### 规范卡片 2

- **规范名称**：电力设备预防性试验规程

[[PDF:/api/knowledge/databases/kb-1/documents/file-2/download]]`

  assert.deepEqual(extractPowerSpecPdfUrls(markdown), [
    '/api/knowledge/databases/kb-1/documents/file-1/download',
    '/api/knowledge/databases/kb-1/documents/file-2/download'
  ])
})

test('规范卡片缺少标记时保持空位，不把后一张地址串到前一张', () => {
  const markdown = `### 规范卡片 1

无原文档

### 规范卡片 2

[[PDF:/api/knowledge/databases/kb-1/documents/file-2/download]]`

  assert.deepEqual(extractPowerSpecPdfUrls(markdown), [
    '',
    '/api/knowledge/databases/kb-1/documents/file-2/download'
  ])
})

test('外部或 MinIO 内网地址不成为可下载地址', () => {
  for (const url of [
    'https://example.test/file.pdf',
    'http://minio:9000/knowledgebases/kb-1/upload/file.pdf',
    'minio://knowledgebases/kb-1/upload/file.pdf'
  ]) {
    assert.equal(isKnowledgeDocumentDownloadUrl(url), false)
    assert.equal(replacePowerSpecPdfMarkers(`[[PDF:${url}]]`), '')
  }
})

test('最终 Markdown 的 PDF 标记转换为同源下载链接', () => {
  const url = '/api/knowledge/databases/kb-1/documents/file-1/download'

  assert.equal(replacePowerSpecPdfMarkers(`[[PDF:${url}]]`), `[下载 PDF 原文](${url})`)
})

test('powerspec bucket 的标准号 PDF 地址可解析', () => {
  const url = '/api/knowledge/power-spec/pdfs/GB_T6451-2023.pdf'

  assert.equal(isKnowledgeDocumentDownloadUrl(url), true)
  assert.equal(replacePowerSpecPdfMarkers(`[[PDF:${url}]]`), `[下载 PDF 原文](${url})`)
})

test('旧卡片结果可由标准号补生成 powerspec 下载地址', () => {
  assert.equal(
    powerSpecPdfUrlForStandardNo('DL/T 408-2023'),
    '/api/knowledge/power-spec/pdfs/DL_T408-2023.pdf'
  )
  assert.equal(
    powerSpecPdfUrlForStandardNo('GB／T 11022—2020'),
    '/api/knowledge/power-spec/pdfs/GB_T11022-2020.pdf'
  )
  assert.equal(powerSpecPdfUrlForStandardNo(''), '')
})
