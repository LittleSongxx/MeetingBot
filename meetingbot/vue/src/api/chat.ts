/**
 * 问答接口的类型化封装：页面只调这里，不直接拼 URL 与解析响应。
 *
 * 与 `utils/request.ts`（axios 实例）的分工：普通接口走 request，它有统一的
 * 401 处理与错误提示；**流式接口走 fetch**——axios 在浏览器里拿不到响应体的
 * 增量（XHR 的 onprogress 给的是字节数，不是解析好的文本），而逐 token 渲染
 * 必须边收边画。因此这里单独实现，并**把 axios 拦截器负责的两件事补上**：
 * 带上 token、把非 2xx 翻译成带后端 msg 的错误（否则页面上只会看到 "Failed to fetch"）。
 */

import request, { type ApiResult, type PageData } from '@/utils/request'
import { parseJsonData, parseSseStream } from '@/utils/sse'

/** 与 `utils/request.ts` 同源：登录时写入的缓存里取 token。 */
function authToken(): string {
  try {
    const user = JSON.parse(localStorage.getItem('xm-user') || '{}')
    return typeof user.token === 'string' ? user.token : ''
  } catch {
    return ''
  }
}

export interface ChatSessionInfo {
  id: number
  title: string
  /** 本次对话限定的会议 id；空数组表示"当前用户可见的全部会议" */
  scope_meeting_ids: number[]
  message_count: number
  last_active_time?: string | null
  create_time?: string | null
}

export interface ChatCitation {
  ref: number
  kind: 'chunk' | 'fact' | 'segment'
  meeting_id: number
  quote: string
  meeting_title?: string
  speaker?: string | null
  start_second?: number | null
  authority?: string | null
  chunk_id?: number
  minutes_id?: number
  field?: string | null
  item_index?: number
}

export interface ChatToolCall {
  call_id: string
  name: string
  arguments: Record<string, unknown>
  summary: string
  ok: boolean | null
}

export interface ChatMessageInfo {
  id: number
  role: 'USER' | 'ASSISTANT'
  content: string
  stop_reason?: string | null
  error_message?: string | null
  token_count: number
  create_time: string
  citations: ChatCitation[]
  tools: ChatToolCall[]
  /** 非正常结束时给用户的提示（后端按 stop_reason 生成） */
  notice?: string
  /** 本轮生命周期：RUNNING / FINISHED / FAILED / INTERRUPTED */
  run_status?: string
  thread_id?: string | null
  checkpoint_id?: string | null
  trace_id?: string | null
}

export interface ChatStatus {
  model: string | null
  model_configured: boolean
  tool_calling_supported: boolean
  embedding_configured: boolean
  vector_store_reachable: boolean
}

/** 流式事件的联合类型：页面 switch 这个 type，编译器保证不遗漏。 */
export type ChatStreamEvent =
  | {
      type: 'meta'
      session_id: number
      message_id: number
      user_message_id: number
      model: string
      scope_meeting_ids: number[]
      question: string
      trace_id?: string
      thread_id?: string
      resumed?: boolean
      answer_prefix?: string
    }
  | { type: 'token'; text: string }
  | { type: 'tool_start'; name: string; call_id: string; arguments: Record<string, unknown> }
  | { type: 'tool_end'; name: string; call_id: string; summary: string }
  | { type: 'citation'; citation: ChatCitation }
  | {
      type: 'done'
      message_id: number
      stop_reason: string
      notice: string
      tokens: { input_tokens: number; output_tokens: number; total_tokens: number }
      citations: ChatCitation[]
      tool_calls: number
      unknown_refs: number[]
    }
  | { type: 'error'; message: string; message_id?: number }

export function createSession(meetingIds?: number[]): Promise<ApiResult<ChatSessionInfo>> {
  return request.post<ChatSessionInfo>('/chat/session', { meeting_ids: meetingIds })
}

export function listSessions(
  pageNum = 1,
  pageSize = 20,
): Promise<ApiResult<PageData<ChatSessionInfo>>> {
  return request.get<PageData<ChatSessionInfo>>('/chat/selectSessionPage', {
    params: { page_num: pageNum, page_size: pageSize },
  })
}

export function deleteSession(sessionId: number): Promise<ApiResult<null>> {
  return request.delete<null>('/chat/session', { params: { session_id: sessionId } })
}

export function listMessages(
  sessionId: number,
  limit = 100,
): Promise<ApiResult<{ list: ChatMessageInfo[] }>> {
  return request.get<{ list: ChatMessageInfo[] }>('/chat/messages', {
    params: { session_id: sessionId, limit },
  })
}

export function chatStatus(): Promise<ApiResult<ChatStatus>> {
  return request.get<ChatStatus>('/chat/status')
}

/** 流式事件的 JSON 形状（与后端 `chat_service.stream_answer` 的键一致）。 */
interface RawEventPayload {
  text?: string
  name?: string
  call_id?: string
  arguments?: Record<string, unknown>
  summary?: string
  message?: string
  message_id?: number
  [key: string]: unknown
}

/**
 * 发一次提问并逐事件回调。返回的 promise 在流结束时 resolve；中途失败抛 Error。
 *
 * `onEvent` 是**同步**回调：调用方只往里塞状态、不改网络行为，所以不需要 await
 * （需要等待的场景请自己收集）。
 */
export async function streamChat(options: {
  sessionId: number
  question: string
  resumeMessageId?: number
  lastEventId?: string
  signal?: AbortSignal
  onEvent: (event: ChatStreamEvent) => void
  onEventId?: (id: string) => void
}): Promise<void> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json;charset=utf-8',
    token: authToken(),
  }
  if (options.lastEventId) headers['Last-Event-ID'] = options.lastEventId
  const body: Record<string, unknown> = {
    session_id: options.sessionId,
    question: options.question,
  }
  // 正常提问不带恢复字段，保持与原协议一致的载荷（也便于抓取侧复现）
  if (options.resumeMessageId != null) body.resume_message_id = options.resumeMessageId
  const response = await fetch(`${import.meta.env.VITE_BASE_URL || ''}/chat/stream`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal: options.signal,
  })
  if (!response.ok) {
    // 后端的错误体仍是 { code, msg }，把 msg 提出来（axios 拦截器做的是同一件事）
    let message = `请求失败（${response.status}）`
    try {
      const body = (await response.json()) as { msg?: string }
      if (body?.msg) message = body.msg
    } catch {
      /* 错误体不是 JSON：保留上面的兜底文案 */
    }
    throw new Error(message)
  }
  if (!response.body) throw new Error('服务端没有返回响应体')

  let terminal = false
  for await (const raw of parseSseStream(response.body)) {
    if (raw.id) options.onEventId?.(raw.id)
    const payload = parseJsonData<RawEventPayload>(raw)
    if (!payload) continue
    switch (raw.event) {
      case 'token':
        options.onEvent({ type: 'token', text: String(payload.text ?? '') })
        break
      case 'tool_start':
        options.onEvent({
          type: 'tool_start',
          name: String(payload.name ?? ''),
          call_id: String(payload.call_id ?? ''),
          arguments: payload.arguments ?? {},
        })
        break
      case 'tool_end':
        options.onEvent({
          type: 'tool_end',
          name: String(payload.name ?? ''),
          call_id: String(payload.call_id ?? ''),
          summary: String(payload.summary ?? ''),
        })
        break
      case 'citation':
        options.onEvent({ type: 'citation', citation: payload as unknown as ChatCitation })
        break
      case 'meta':
        options.onEvent({
          type: 'meta',
          session_id: Number(payload.session_id),
          message_id: Number(payload.message_id),
          user_message_id: Number(payload.user_message_id),
          model: String(payload.model ?? ''),
          scope_meeting_ids: (payload.scope_meeting_ids as number[]) ?? [],
          question: String(payload.question ?? ''),
          trace_id: payload.trace_id ? String(payload.trace_id) : undefined,
          thread_id: payload.thread_id ? String(payload.thread_id) : undefined,
          resumed: Boolean(payload.resumed),
          answer_prefix: payload.answer_prefix ? String(payload.answer_prefix) : undefined,
        })
        break
      case 'done':
        terminal = true
        options.onEvent({ ...(payload as object), type: 'done' } as ChatStreamEvent)
        break
      case 'error':
        terminal = true
        options.onEvent({
          type: 'error',
          message: String(payload.message ?? '问答失败'),
          message_id: payload.message_id,
        })
        break
      default:
        // 未知事件名：忽略而不是报错——服务端新增事件类型时老前端不该崩
        break
    }
  }
  if (!terminal) {
    // 代理超时/后端重启可能只发半段就 EOF；不能把截断答案当正常完成。
    throw new Error('连接中断，回答可能不完整')
  }
}
