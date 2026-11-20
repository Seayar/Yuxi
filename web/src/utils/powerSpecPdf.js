const CARD_HEADING_RE = /^### 规范卡片 \d+\s*$/gm
const PDF_MARKER_RE = /\[\[PDF:([^\]\r\n]+)\]\]/g
const PDF_MARKER_SECTION_RE = /\[\[PDF:([^\]\r\n]+)\]\]/
const KNOWLEDGE_DOCUMENT_DOWNLOAD_RE =
  /^(?:\/api\/knowledge\/databases\/[^/]+\/documents\/[^/]+\/download|\/api\/knowledge\/power-spec\/pdfs\/[A-Z0-9._-]+\.pdf)$/

export const isKnowledgeDocumentDownloadUrl = (value) =>
  KNOWLEDGE_DOCUMENT_DOWNLOAD_RE.test(String(value || '').trim())

export const powerSpecPdfUrlForStandardNo = (value) => {
  const normalized = String(value || '')
    .normalize('NFKC')
    .trim()
    .toUpperCase()
    .replace(/[—–－]/g, '-')
    .replace(/／/g, '/')
  const safe = normalized
    .replace(/[^A-Z0-9._-]+/g, '_')
    .replace(/^[._-]+|[._-]+$/g, '')
    .replace(/_(?=\d)/g, '')
  return safe ? `/api/knowledge/power-spec/pdfs/${safe}.pdf` : ''
}

export const extractPowerSpecPdfUrls = (markdown) => {
  const content = String(markdown || '')
  const headings = [...content.matchAll(CARD_HEADING_RE)]

  return headings.map((heading, index) => {
    const start = heading.index + heading[0].length
    const end = headings[index + 1]?.index ?? content.length
    const match = content.slice(start, end).match(PDF_MARKER_SECTION_RE)
    const url = match?.[1]?.trim() || ''
    return isKnowledgeDocumentDownloadUrl(url) ? url : ''
  })
}

export const replacePowerSpecPdfMarkers = (markdown) =>
  String(markdown || '').replace(PDF_MARKER_RE, (_marker, value) => {
    const url = String(value || '').trim()
    return isKnowledgeDocumentDownloadUrl(url) ? `[下载 PDF 原文](${url})` : ''
  })
