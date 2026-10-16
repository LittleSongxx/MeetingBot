import { describe, expect, it } from 'vitest'

import { parseJsonData, parseSseBlock, parseSseStream, type SseEvent } from '@/utils/sse'

/** 把若干字符串片段包成响应体：片段边界模拟"网络分片与事件边界无关"。 */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
}

async function collect(chunks: string[]): Promise<SseEvent[]> {
  const events: SseEvent[] = []
  for await (const event of parseSseStream(streamOf(chunks))) events.push(event)
  return events
}

describe('parseSseBlock', () => {
  it('读取事件名与数据', () => {
    expect(parseSseBlock('event: token\ndata: {"text":"上线"}')).toEqual({
      event: 'token',
      data: '{"text":"上线"}',
    })
  })

  it('缺省事件名是 message', () => {
    expect(parseSseBlock('data: hi')).toEqual({ event: 'message', data: 'hi' })
  })

  it('多行 data 用换行拼接', () => {
    expect(parseSseBlock('data: a\ndata: b')).toEqual({ event: 'message', data: 'a\nb' })
  })

  it('忽略心跳注释行', () => {
    expect(parseSseBlock(': ping')).toBeNull()
    expect(parseSseBlock(': ping\ndata: x')).toEqual({ event: 'message', data: 'x' })
  })

  it('冒号后只吃掉一个空格，其余保留', () => {
    expect(parseSseBlock('data:  两个空格')).toEqual({ event: 'message', data: ' 两个空格' })
  })
})

describe('parseSseStream', () => {
  it('逐事件解析', async () => {
    const events = await collect([
      'event: meta\ndata: {"a":1}\n\n',
      'event: token\ndata: {"b":2}\n\n',
    ])
    expect(events).toEqual([
      { event: 'meta', data: '{"a":1}' },
      { event: 'token', data: '{"b":2}' },
    ])
  })

  it('跨分片的半个事件不会被当成坏事件', async () => {
    // 这正是缓冲存在的理由：网络分片与事件边界没有关系
    const events = await collect(['event: tok', 'en\ndata: {"text":"上', '线时间"}\n\n'])
    expect(events).toEqual([{ event: 'token', data: '{"text":"上线时间"}' }])
  })

  it('一个分片里塞多个事件', async () => {
    const events = await collect(['data: 1\n\ndata: 2\n\n'])
    expect(events.map((event) => event.data)).toEqual(['1', '2'])
  })

  it('心跳注释出现在中间时不影响前后事件', async () => {
    const events = await collect(['data: 1\n\n: ping\n\ndata: 2\n\n'])
    expect(events.map((event) => event.data)).toEqual(['1', '2'])
  })

  it('兼容 CRLF 换行', async () => {
    const events = await collect(['data: 1\r\n\r\ndata: 2\r\n\r\n'])
    expect(events.map((event) => event.data)).toEqual(['1', '2'])
  })

  it('最后一个事件没有补空行时也要吐出来', async () => {
    const events = await collect(['data: 收尾'])
    expect(events).toEqual([{ event: 'message', data: '收尾' }])
  })

  it('中文跨字节边界不会乱码', async () => {
    const encoder = new TextEncoder()
    const bytes = encoder.encode('data: 说话人\n\n')
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        // 按字节切开，故意切断一个汉字的 UTF-8 序列
        controller.enqueue(bytes.slice(0, 7))
        controller.enqueue(bytes.slice(7))
        controller.close()
      },
    })
    const events: SseEvent[] = []
    for await (const event of parseSseStream(stream)) events.push(event)
    expect(events[0].data).toBe('说话人')
  })
})

describe('parseJsonData', () => {
  it('解析 JSON', () => {
    expect(parseJsonData<{ a: number }>({ event: 'x', data: '{"a":1}' })).toEqual({ a: 1 })
  })

  it('坏帧返回 null 而不是抛异常', () => {
    expect(parseJsonData({ event: 'x', data: '{坏' })).toBeNull()
  })
})
