import { describe, expect, it } from 'vitest'

import { renderMarkdown, splitCitations } from '@/utils/markdown'

describe('renderMarkdown', () => {
  it('渲染常见 Markdown 结构', () => {
    const html = renderMarkdown('**结论**：十月上线\n\n- 第一条\n- 第二条')
    expect(html).toContain('<strong>结论</strong>')
    expect(html).toContain('<li>第一条</li>')
  })

  it('原始 HTML 不被解释（html: false）', () => {
    const html = renderMarkdown('会议里有人念了 <img src=x onerror="alert(1)"> 这段')
    // markdown-it 直接把它当文本转义，DOMPurify 再兜一层
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
  })

  it('消毒掉 javascript: 链接与 script 标签', () => {
    const html = renderMarkdown('<script>alert(1)</script>\n\n[点我](javascript:alert(1))')
    expect(html).not.toContain('<script')
    // markdown-it 自己就不认 javascript: 链接（按普通文本处理），
    // 这里断言的是"没有产出危险的 href"，而不是"文本里没有这几个字"
    expect(html).not.toContain('href="javascript:')
  })

  it('保留引用标记本身（它是页面挂点击行为的锚点）', () => {
    expect(renderMarkdown('上线时间定在十月[1]。')).toContain('[1]')
  })

  it('空输入不炸', () => {
    expect(renderMarkdown('')).toBe('')
  })
})

describe('splitCitations', () => {
  it('把答案切成文本与引用片段', () => {
    expect(splitCitations('结论是十月[1]，另外[12]存疑。')).toEqual([
      { type: 'text', text: '结论是十月' },
      { type: 'citation', ref: 1 },
      { type: 'text', text: '，另外' },
      { type: 'citation', ref: 12 },
      { type: 'text', text: '存疑。' },
    ])
  })

  it('没有引用时只剩一段文本', () => {
    expect(splitCitations('没有引用')).toEqual([{ type: 'text', text: '没有引用' }])
  })

  it('方括号里不是数字的不算引用', () => {
    expect(splitCitations('见[附录]与[abc]')).toEqual([{ type: 'text', text: '见[附录]与[abc]' }])
  })

  it('连续引用各自成段', () => {
    expect(splitCitations('[1][2]')).toEqual([
      { type: 'citation', ref: 1 },
      { type: 'citation', ref: 2 },
    ])
  })
})
