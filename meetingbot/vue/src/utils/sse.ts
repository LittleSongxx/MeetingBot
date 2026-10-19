/**
 * SSE（Server-Sent Events）帧解析。
 *
 * 为什么单独成模块：把"字节流 → 事件"这段唯一的难点（跨 chunk 的半个帧、
 * 心跳注释行、多行 data、Windows 换行）与页面渲染彻底分开，于是它可以被单测覆盖，
 * 而页面只管把事件画出来。仓库里另一处 SSE（纪要自检）此前把这段逻辑写在页面里，
 * 谁都没法单独验证它——这次不重复那个形态。
 *
 * 解析规则来自 WHATWG 的 SSE 规范，只实现本项目用得到的部分：
 * - 事件之间用空行分隔（`\n\n`，兼容 `\r\n\r\n`）
 * - 以 `:` 开头的行是注释（我们用它当心跳），必须忽略且**不**影响当前事件
 * - `event:` 给事件命名，缺省是 `message`
 * - 多行 `data:` 用换行拼接
 */

/** 一个完整事件。`data` 是原始文本，调用方自己决定怎么解析（本项目是 JSON）。 */
export interface SseEvent {
  event: string
  data: string
  /** 服务端事件 id（用于 Last-Event-ID 续传） */
  id?: string
  /** 服务端建议的重连间隔（毫秒） */
  retry?: number
}

/** 把一段块（两个空行之间）解析成事件；只有注释行时返回 null。 */
export function parseSseBlock(block: string): SseEvent | null {
  let event = 'message'
  let id: string | undefined
  let retry: number | undefined
  const dataLines: string[] = []
  let sawField = false
  for (const rawLine of block.split(/\r?\n/)) {
    if (!rawLine || rawLine.startsWith(':')) continue // 空行与心跳注释
    const separator = rawLine.indexOf(':')
    const field = separator === -1 ? rawLine : rawLine.slice(0, separator)
    // 规范规定冒号后紧跟的一个空格要去掉，其余空格保留
    let value = separator === -1 ? '' : rawLine.slice(separator + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') {
      event = value || 'message'
      sawField = true
    } else if (field === 'data') {
      dataLines.push(value)
      sawField = true
    } else if (field === 'id') {
      id = value
      sawField = true
    } else if (field === 'retry') {
      const parsed = Number(value)
      if (Number.isFinite(parsed)) retry = parsed
      sawField = true
    }
  }
  if (!sawField) return null
  return { event, data: dataLines.join('\n'), id, retry }
}

/**
 * 把响应体拆成事件流。
 *
 * 跨 chunk 的半帧由内部缓冲处理：网络分片与事件边界毫无关系，不缓冲就会
 * 把"一条 JSON 被切成两半"当成两条坏事件。
 */
export async function* parseSseStream(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<SseEvent> {
  const reader = stream.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // 事件之间是空行；把已完整的块切出来，剩下的留在缓冲区
      let index = buffer.search(/\r?\n\r?\n/)
      while (index !== -1) {
        const block = buffer.slice(0, index)
        const match = /\r?\n\r?\n/.exec(buffer.slice(index)) as RegExpExecArray
        buffer = buffer.slice(index + match[0].length)
        const parsed = parseSseBlock(block)
        if (parsed) yield parsed
        index = buffer.search(/\r?\n\r?\n/)
      }
    }
    // 收尾：服务端可能在最后一个事件后没补空行
    buffer += decoder.decode()
    const tail = parseSseBlock(buffer)
    if (tail) yield tail
  } finally {
    reader.releaseLock()
  }
}

/** 把 `data:` 里的 JSON 解析出来。坏帧返回 null 而不是抛异常——一个坏帧不该打断整条流。 */
export function parseJsonData<T>(event: SseEvent): T | null {
  try {
    return JSON.parse(event.data) as T
  } catch {
    return null
  }
}
