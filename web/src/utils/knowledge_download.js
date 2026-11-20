import { documentApi } from '@/apis/knowledge_api'
import { parseDownloadFilename } from '@/utils/file_utils'

const sanitizeFilename = (value, fallback) => {
  const filename = String(value || '')
    .replace(/[\\/:*?"<>|]/g, '_')
    .trim()
  return filename || fallback
}

export const downloadKnowledgeDocument = async (url, fallbackFilename = '规范原文.pdf') => {
  const response = await documentApi.downloadDocumentByUrl(url)
  const blob = await response.blob()
  const filename = sanitizeFilename(
    parseDownloadFilename(
      response.headers.get('Content-Disposition') || response.headers.get('content-disposition')
    ),
    fallbackFilename
  )
  const objectUrl = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = filename
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.URL.revokeObjectURL(objectUrl)
}
