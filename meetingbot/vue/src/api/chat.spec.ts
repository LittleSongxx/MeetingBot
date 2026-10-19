import { afterEach, describe, expect, it, vi } from 'vitest'

import { createSession, streamChat, type ChatStreamEvent } from '@/api/chat'

// createSession 走 axios 实例（@/utils/request），这里换成纯 mock；
// streamChat 走 fetch，不受影响（下面用 stubGlobal 单独控制）。
const requestMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('@/utils/request', () => ({ default: requestMocks }))

/** 构造一个"像 Response"的对象：只需要 ok/status/body/json 四个用到的东西。 */
function responseOf(chunks: string[], init: Partial<Response> = {}): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return { ok: true, status: 200, body, json: async () => ({}), ...init } as unknown as Response
}

const DONE =
  'event: done\ndata: {"message_id":9,"stop_reason":"finished","notice":"","tokens":{"total_tokens":1},"citations":[],"tool_calls":0,"unknown_refs":[]}\n\n'

describe('streamChat', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('把 SSE 帧翻译成类型化事件', async () => {
    const frames = [
      'event: meta\ndata: {"session_id":1,"message_id":9,"model":"m","scope_meeting_ids":[],"question":"q"}\n\n',
      'event: tool_start\ndata: {"name":"search_knowledge","call_id":"c1","arguments":{"query":"上线"}}\n\n',
      'event: tool_end\ndata: {"name":"search_knowledge","call_id":"c1","summary":"3 条切片"}\n\n',
      'event: citation\ndata: {"ref":1,"kind":"chunk","meeting_id":3,"quote":"十月上线"}\n\n',
      'event: token\ndata: {"text":"上线时间"}\n\n',
      'event: done\ndata: {"message_id":9,"stop_reason":"finished","notice":"","tokens":{"total_tokens":30},"citations":[],"tool_calls":1,"unknown_refs":[]}\n\n',
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseOf(frames)))

    const events: ChatStreamEvent[] = []
    await streamChat({ sessionId: 1, question: '上线时间', onEvent: (event) => events.push(event) })

    expect(events.map((event) => event.type)).toEqual([
      'meta',
      'tool_start',
      'tool_end',
      'citation',
      'token',
      'done',
    ])
    expect(events[1]).toEqual({
      type: 'tool_start',
      name: 'search_knowledge',
      call_id: 'c1',
      arguments: { query: '上线' },
    })
    expect(events[3]).toMatchObject({ type: 'citation', citation: { ref: 1, meeting_id: 3 } })
    expect(events[4]).toEqual({ type: 'token', text: '上线时间' })
    expect(events[5]).toMatchObject({ type: 'done', stop_reason: 'finished' })
  })

  it('请求带上登录 token 与 JSON 载荷', async () => {
    localStorage.setItem('xm-user', JSON.stringify({ id: 1, token: 'tok-123' }))
    const fetchMock = vi.fn().mockResolvedValue(responseOf([DONE]))
    vi.stubGlobal('fetch', fetchMock)

    await streamChat({ sessionId: 7, question: '问', onEvent: () => {} })

    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/chat/stream')
    expect(init.method).toBe('POST')
    expect(init.headers.token).toBe('tok-123')
    expect(JSON.parse(init.body)).toEqual({ session_id: 7, question: '问' })
  })

  it('非 2xx 把后端的 msg 提出来（否则页面只会看到 Failed to fetch）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        responseOf([], {
          ok: false,
          status: 400,
          json: async () => ({ code: '400', msg: '会话不存在或无权访问' }),
        }),
      ),
    )
    await expect(streamChat({ sessionId: 1, question: '问', onEvent: () => {} })).rejects.toThrow(
      '会话不存在或无权访问',
    )
  })

  it('未知事件名被忽略（服务端加事件不该让老前端崩）', async () => {
    const frames = [
      'event: heartbeat_v2\ndata: {"x":1}\n\n',
      'event: token\ndata: {"text":"ok"}\n\n',
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseOf(frames)))
    const events: ChatStreamEvent[] = []
    await streamChat({ sessionId: 1, question: '问', onEvent: (event) => events.push(event) })
    expect(events).toEqual([
      { type: 'token', text: 'ok' },
      expect.objectContaining({ type: 'done' }),
    ])
  })

  it('坏帧被跳过，好帧照常送达', async () => {
    const frames = ['event: token\ndata: {坏}\n\n', 'event: token\ndata: {"text":"好"}\n\n', DONE]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseOf(frames)))
    const events: ChatStreamEvent[] = []
    await streamChat({ sessionId: 1, question: '问', onEvent: (event) => events.push(event) })
    expect(events).toEqual([
      { type: 'token', text: '好' },
      expect.objectContaining({ type: 'done' }),
    ])
  })

  it('没有 done/error 的 EOF 视为连接中断', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(responseOf(['event: token\ndata: {"text":"半"}'])),
    )
    await expect(streamChat({ sessionId: 1, question: '问', onEvent: () => {} })).rejects.toThrow(
      '连接中断',
    )
  })

  it('meta 的系列范围与 done 的安全标记原样透传', async () => {
    const frames = [
      'event: meta\ndata: {"session_id":1,"message_id":9,"model":"m","scope_meeting_ids":[],"scope_series_ids":[3],"question":"q"}\n\n',
      'event: done\ndata: {"message_id":9,"stop_reason":"finished","notice":"","tokens":{"total_tokens":3},"citations":[],"tool_calls":0,"unknown_refs":[],"security":{"flags":[{"rule":"injection.ignore_instructions","severity":"high","source":"user","match":"忽略"}],"blocked":false}}\n\n',
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseOf(frames)))
    const events: ChatStreamEvent[] = []
    await streamChat({ sessionId: 1, question: '问', onEvent: (event) => events.push(event) })
    expect(events[0]).toMatchObject({ type: 'meta', scope_series_ids: [3] })
    expect(events[1]).toMatchObject({
      type: 'done',
      security: { blocked: false, flags: [{ rule: 'injection.ignore_instructions' }] },
    })
  })
})

describe('createSession', () => {
  it('会议与系列范围一起提交（后端取并集）', async () => {
    requestMocks.post.mockResolvedValue({ code: '200', msg: 'ok', data: { id: 1 } })
    await createSession({ meetingIds: [5], seriesIds: [3] })
    expect(requestMocks.post).toHaveBeenCalledWith('/chat/session', {
      meeting_ids: [5],
      series_ids: [3],
    })
  })

  it('不选范围时两个字段都是 undefined（全部可见会议）', async () => {
    requestMocks.post.mockResolvedValue({ code: '200', msg: 'ok', data: { id: 2 } })
    await createSession()
    expect(requestMocks.post).toHaveBeenCalledWith('/chat/session', {
      meeting_ids: undefined,
      series_ids: undefined,
    })
  })
})
