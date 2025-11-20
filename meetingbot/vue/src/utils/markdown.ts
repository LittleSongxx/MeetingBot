/**
 * Markdown 渲染：markdown-it 解析 + DOMPurify 消毒。
 *
 * 为什么必须消毒：答案正文里混着【模型输出】与【检索到的会议原文】——后者是用户
 * 上传的会议内容，本身就是不可信输入。直接 `v-html` 渲染等于把"会议里有人念了
 * 一段 `<img onerror=...>`"变成产品里的 XSS。渲染链只此一处，页面不要自己拼 HTML。
 */

import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({
  html: false, // 连 HTML 都不解释：答案只需要 Markdown 子集
  linkify: true,
  breaks: true, // 会议原文与模型输出都常按单换行分段，按字面换行更符合阅读习惯
})

/** 引用标记 `[1]` 单独包一层，页面据此挂点击行为（Markdown 已经把方括号当普通字符）。 */
const CITATION_RE = /\[(\d{1,3})\]/g

export function renderMarkdown(text: string): string {
  const html = md.render(text || '')
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      'p',
      'br',
      'strong',
      'em',
      'del',
      'code',
      'pre',
      'blockquote',
      'ul',
      'ol',
      'li',
      'h1',
      'h2',
      'h3',
      'h4',
      'h5',
      'h6',
      'table',
      'thead',
      'tbody',
      'tr',
      'th',
      'td',
      'a',
      'hr',
      'span',
    ],
    ALLOWED_ATTR: ['href', 'title', 'target', 'rel', 'class'],
  })
}

/**
 * 把答案按引用标记切成片段，供页面渲染成可点击的角标。
 *
 * 返回的每段要么是文本、要么是引用编号——页面不需要自己写正则，
 * 也就不会与 `renderMarkdown` 的白名单漂移。
 */
export type AnswerPart = { type: 'text'; text: string } | { type: 'citation'; ref: number }

export function splitCitations(text: string): AnswerPart[] {
  const parts: AnswerPart[] = []
  let cursor = 0
  for (const match of (text || '').matchAll(CITATION_RE)) {
    const index = match.index ?? 0
    if (index > cursor) parts.push({ type: 'text', text: text.slice(cursor, index) })
    parts.push({ type: 'citation', ref: Number(match[1]) })
    cursor = index + match[0].length
  }
  if (cursor < (text || '').length) parts.push({ type: 'text', text: text.slice(cursor) })
  return parts
}
