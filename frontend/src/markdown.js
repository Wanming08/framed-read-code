import { marked } from 'marked'

const ALLOWED_TAGS = new Set([
  'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'P', 'BR', 'HR', 'BLOCKQUOTE',
  'UL', 'OL', 'LI', 'STRONG', 'EM', 'DEL', 'CODE', 'PRE', 'A',
  'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD'
])

export function renderMarkdown(markdown) {
  if (!markdown) return ''

  let cleanText = markdown.replace(/<think>[\s\S]*?<\/think>/gi, '')
  if (cleanText.includes('</think>')) cleanText = cleanText.split('</think>').pop()
  if (!cleanText.trim()) cleanText = markdown
  cleanText = linkVideoTimestamps(cleanText)

  const template = document.createElement('template')
  template.innerHTML = marked.parse(cleanText)
  template.content.querySelectorAll('*').forEach(sanitizeNode)
  return template.innerHTML
}

export function extractReportReferences(renderedHtml) {
  if (!renderedHtml) return []

  const template = document.createElement('template')
  template.innerHTML = renderedHtml
  const references = new Map()

  for (const link of template.content.querySelectorAll('a[href^="#video-t="]')) {
    const value = link.getAttribute('href').slice('#video-t='.length).trim()
    if (!/^\d+(?:\.\d+)?$/.test(value)) continue
    const timestampMs = Number(value) * 1000
    if (!Number.isFinite(timestampMs) || timestampMs < 0) continue

    const snippet = reportReferenceSnippet(link)
    const existing = references.get(timestampMs)
    // 同一位置可能同时出现在引用列表和正文中，保留上下文更完整的一条。
    if (!existing || meaningfulTextLength(snippet) > meaningfulTextLength(existing.snippet)) {
      references.set(timestampMs, { timestampMs, snippet })
    }
  }

  return [...references.values()].sort((left, right) => left.timestampMs - right.timestampMs)
}

function reportReferenceSnippet(link) {
  // 表格引用保留整行；其他引用优先取最近的段落或列表项。
  const contexts = [link.closest('tr'), link.closest('li, p'), link.closest('li'), link.parentElement]
  for (const context of new Set(contexts)) {
    if (!context) continue
    const copy = context.cloneNode(true)
    copy.querySelectorAll('a[href^="#video-t="]').forEach(reference => {
      reference.replaceWith(document.createTextNode(' '))
    })
    // textContent 不保留单元格和段落边界，补空格以免相邻文字粘连。
    copy.querySelectorAll('br').forEach(lineBreak => {
      lineBreak.replaceWith(document.createTextNode(' '))
    })
    copy.querySelectorAll('p, li, th, td').forEach(block => {
      block.appendChild(document.createTextNode(' '))
    })
    const snippet = (copy.textContent || '').replace(/\s+/g, ' ').trim()
    if (meaningfulTextLength(snippet)) return snippet
  }
  return ''
}

function meaningfulTextLength(text) {
  return (text.match(/[\p{L}\p{N}]/gu) || []).length
}

function sanitizeNode(node) {
  if (!ALLOWED_TAGS.has(node.tagName)) {
    node.replaceWith(document.createTextNode(node.textContent || ''))
    return
  }

  for (const attribute of [...node.attributes]) {
    const allowed = node.tagName === 'A'
      && (attribute.name === 'href' || attribute.name === 'title')
    if (!allowed) node.removeAttribute(attribute.name)
  }
  if (node.tagName !== 'A') return

  const href = node.getAttribute('href') || ''
  if (!/^(https?:|mailto:|\/|#)/i.test(href)) node.removeAttribute('href')
  node.setAttribute('rel', 'noopener noreferrer')
  if (!href.startsWith('#video-t=')) node.setAttribute('target', '_blank')
}

function linkVideoTimestamps(markdown) {
  return markdown.replace(/\[((?:\d+:)?\d+:\d{2})\](?!\()/g, (match, timestamp) => {
    const parts = timestamp.split(':').map(Number)
    const seconds = parts.length === 3
      ? parts[0] * 3600 + parts[1] * 60 + parts[2]
      : parts[0] * 60 + parts[1]
    return `[${timestamp}](#video-t=${seconds})`
  })
}
