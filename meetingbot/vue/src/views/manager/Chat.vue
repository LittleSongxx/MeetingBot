<template>
  <div class="chat-page">
    <!-- 不可用时给一条横幅，而不是让用户问了才知道 -->
    <div v-if="data.status && !chatReady" class="card chat-banner">
      <el-alert type="warning" :closable="false" show-icon :title="bannerText" />
    </div>

    <div class="chat-body">
      <!-- 左：会话列表 + 本次对话范围 -->
      <div class="card chat-side">
        <el-button type="primary" style="width: 100%" @click="newSession">新建对话</el-button>
        <div class="scope-box">
          <div class="scope-label">对话范围</div>
          <el-radio-group v-model="scopeMode" size="small" class="scope-mode">
            <el-radio-button value="meeting">按会议</el-radio-button>
            <el-radio-button value="series">按系列</el-radio-button>
          </el-radio-group>
          <el-select
            v-if="scopeMode === 'meeting'"
            v-model="scopeMeetingIds"
            multiple
            collapse-tags
            collapse-tags-tooltip
            clearable
            filterable
            placeholder="全部可见会议"
            style="width: 100%"
          >
            <el-option
              v-for="item in data.meetings"
              :key="item.id"
              :label="`${item.meeting_no} / ${item.title}`"
              :value="item.id"
            />
          </el-select>
          <el-select
            v-else
            v-model="scopeSeriesIds"
            multiple
            collapse-tags
            collapse-tags-tooltip
            clearable
            filterable
            placeholder="全部系列"
            style="width: 100%"
          >
            <el-option
              v-for="item in data.series"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            />
          </el-select>
          <div class="scope-tip">
            留空表示在你有权查看的全部会议里检索；按系列时会展开成该系列下的会议。
          </div>
        </div>
        <el-scrollbar class="session-list">
          <div
            v-for="item in data.sessions"
            :key="item.id"
            class="session-item"
            :class="{ active: item.id === sessionId }"
            @click="openSession(item.id)"
          >
            <div class="session-title">{{ item.title }}</div>
            <div class="session-meta">
              {{ scopeSummary(item) }} · {{ item.message_count }} 条 ·
              {{ item.last_active_time || item.create_time }}
            </div>
            <el-icon class="session-del" @click.stop="removeSession(item.id)"><Delete /></el-icon>
          </div>
          <div v-if="!data.sessions.length" class="empty-tip">还没有对话</div>
        </el-scrollbar>
      </div>

      <!-- 右：消息流 + 输入 -->
      <div class="card chat-main">
        <!-- 当前会话实际生效的范围：会话建立后以服务端返回为准 -->
        <div class="scope-chips">
          <span class="scope-chip-label">当前范围</span>
          <template v-if="scopeChips.length">
            <el-tag v-for="chip in scopeChips" :key="chip.key" size="small" :type="chip.type">
              {{ chip.label }}
            </el-tag>
          </template>
          <el-tag v-else size="small" type="info">全部可见会议</el-tag>
          <span v-if="sessionId == null" class="scope-chip-tip">新范围在首次提问时生效</span>
        </div>
        <el-scrollbar ref="scrollRef" class="message-area">
          <div v-if="!messages.length" class="empty-tip">
            问点什么吧，例如「上次评审会上定的上线时间是什么」「这周有哪些待办」。 回答里的 [1]
            可以点开看是哪场会、谁说的、说了什么；决策/待办/偏好会显示成带状态的状态卡片。
          </div>
          <div
            v-for="(message, index) in messages"
            :key="message.id ?? `local-${index}`"
            class="message"
            :class="message.role === 'USER' ? 'message-user' : 'message-assistant'"
          >
            <div class="message-role">{{ message.role === 'USER' ? '我' : '助手' }}</div>
            <!-- 用户消息按纯文本渲染：它由用户自己输入，不需要 Markdown 解释 -->
            <div v-if="message.role === 'USER'" class="message-text">{{ message.content }}</div>
            <!-- eslint-disable vue/no-v-html -- 渲染前先过 renderMarkdown：
                 markdown-it(html:false) 解析 + DOMPurify 白名单消毒，见 utils/markdown.ts。
                 全项目只有这一处 v-html，且不可能绕过这个函数（它在 utils 里，页面拿不到裸 HTML） -->
            <div
              v-else
              class="message-text markdown"
              v-html="renderMarkdown(message.content)"
            ></div>
            <!-- eslint-enable vue/no-v-html -->

            <div v-if="message.role === 'ASSISTANT' && message.pending" class="typing">
              正在检索与生成…
            </div>

            <!-- 工具调用过程折叠展示：默认收起，展开能看到查了什么、拿到几条 -->
            <el-collapse v-if="message.tools?.length" class="tool-trace">
              <el-collapse-item :title="toolTitle(message.tools)">
                <div v-for="(tool, i) in message.tools" :key="i" class="tool-item">
                  <span class="tool-name">{{ chatToolText(tool.name) }}</span>
                  <span class="tool-raw">{{ tool.name }}</span>
                  <span class="tool-args">{{ JSON.stringify(tool.arguments) }}</span>
                  <span class="tool-summary" :class="{ failed: tool.ok === false }">{{
                    tool.summary
                  }}</span>
                </div>
              </el-collapse-item>
            </el-collapse>

            <!-- 引用卡片：答案里真正用到的那些（编号与 [n] 一一对应） -->
            <div v-if="usedCitations(message).length" class="citation-list">
              <template v-for="citation in usedCitations(message)" :key="citation.ref">
                <StateCitationCard v-if="citation.kind === 'state'" :citation="citation" />
                <CitationCard v-else :citation="citation" />
              </template>
            </div>

            <el-alert
              v-if="message.notice"
              class="message-notice"
              type="info"
              :closable="false"
              show-icon
              :title="message.notice"
            />
            <el-alert
              v-if="message.error_message"
              class="message-notice"
              type="error"
              :closable="false"
              show-icon
              :title="message.error_message"
            />
            <!-- 命中注入/越权等安全规则时给警示；后端未返回 security 时不会出现 -->
            <el-alert
              v-if="message.security_notice"
              class="message-notice"
              type="warning"
              :closable="false"
              show-icon
              :title="message.security_notice"
            />
            <el-button
              v-if="
                message.role === 'ASSISTANT' &&
                message.id &&
                (message.run_status === 'RUNNING' || message.run_status === 'INTERRUPTED')
              "
              class="resume-button"
              size="small"
              :loading="message.pending"
              @click="resume(message)"
            >
              继续生成
            </el-button>
          </div>
        </el-scrollbar>

        <div class="chat-input">
          <el-input
            v-model="question"
            type="textarea"
            :rows="3"
            resize="none"
            placeholder="问一个关于会议的问题（Enter 发送，Shift+Enter 换行）"
            @keydown.enter.exact.prevent="send"
          />
          <div class="chat-input-actions">
            <span class="token-tip">{{ tokenTip }}</span>
            <el-button v-if="streaming" type="danger" plain @click="stop">停止</el-button>
            <el-button v-else type="primary" :disabled="!question.trim()" @click="send"
              >发送</el-button
            >
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 知识问答页：会话列表 + 消息流 + 逐 token 渲染 + 引用卡片。
 *
 * 三件事必须由页面自己管，其余都交给下层：
 * - **逐 token**：token 事件到达即追加到"正在生成"的那条消息上（`flushTokens`
 *   按帧合并，避免高速流下每 token 触发一次渲染）；
 * - **引用**：SSE 推来的引用登记在消息上，答案里的 `[n]` 决定展示哪几张卡片；
 * - **中断**：AbortController 交给 fetch，停止后后端仍会落库（服务端对断开有收尾）。
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import request from '@/utils/request'
import { renderMarkdown, splitCitations } from '@/utils/markdown'
import CitationCard from '@/components/CitationCard.vue'
import StateCitationCard from '@/components/StateCitationCard.vue'
import { seriesOptions, type SeriesOption } from '@/api/series'
import { chatToolText } from '@/constants/label'
import {
  chatStatus,
  createSession,
  deleteSession,
  listMessages,
  listSessions,
  streamChat,
  type ChatCitation,
  type ChatMessageInfo,
  type ChatSecurity,
  type ChatSessionInfo,
  type ChatStatus,
} from '@/api/chat'

interface MeetingOption {
  id: number
  meeting_no: string
  title: string
}

/** 页面上的消息：既用于历史（来自后端），也用于正在生成的那条（本地拼）。 */
interface PageMessage {
  id?: number
  role: 'USER' | 'ASSISTANT'
  content: string
  citations: ChatCitation[]
  tools: ChatMessageInfo['tools']
  stop_reason?: string | null
  error_message?: string | null
  notice?: string
  /** 本轮 token 用量（done 事件给出；页面只做展示） */
  token_count?: number
  /** 正在生成中（用于显示"正在检索与生成…"） */
  pending?: boolean
  run_status?: string
  thread_id?: string | null
  trace_id?: string | null
  last_event_id?: string | null
  /** done/error 事件里的安全标记摘要（命中注入规则时显示警示条） */
  security_notice?: string
}

const data = reactive({
  sessions: [] as ChatSessionInfo[],
  meetings: [] as MeetingOption[],
  series: [] as SeriesOption[],
  status: null as ChatStatus | null,
})

/** 范围模式：按会议 / 按系列；一次会话只提交一种范围（后端取并集） */
const scopeMode = ref<'meeting' | 'series'>('meeting')
const scopeMeetingIds = ref<number[]>([])
const scopeSeriesIds = ref<number[]>([])
const messages = ref<PageMessage[]>([])
const sessionId = ref<number | null>(null)
const question = ref('')
const streaming = ref(false)
const tokenTip = ref('')
const scrollRef = ref<{ $el?: Element } | null>(null)

let controller: AbortController | null = null
/** 待刷新的 token 缓冲：按帧合并，避免高速流导致每 token 一次渲染 */
let tokenBuffer = ''
let flushHandle = 0

const chatReady = computed(() => {
  const status = data.status
  if (!status) return true
  return status.model_configured && status.tool_calling_supported
})

const bannerText = computed(() => {
  const status = data.status
  if (!status) return ''
  if (!status.model_configured)
    return '还没有启用 AGENT 用途的模型配置，问答不可用；请先在「AI模型配置」里启用。'
  if (!status.tool_calling_supported)
    return '当前模型供应商未声明支持工具调用，问答不可用；请在 common/providers.KNOWN_TOOL_CALLING 登记后重启后端。'
  if (!status.embedding_configured)
    return '还没有嵌入模型配置，知识库检索会失败；请先启用 EMBEDDING 用途的配置。'
  if (!status.vector_store_reachable) return 'PostgreSQL 检索索引当前不可达，检索会返回空结果。'
  return ''
})

onMounted(() => {
  loadStatus()
  loadMeetings()
  loadSeries()
  loadSessions()
})

function loadStatus(): void {
  chatStatus().then((res) => {
    data.status = res.data
  })
}

function loadMeetings(): void {
  request.get<MeetingOption[]>('/meeting/selectOptions').then((res) => {
    data.meetings = res.data || []
  })
}

function loadSeries(): void {
  seriesOptions().then((res) => {
    data.series = res.data || []
  })
}

function loadSessions(): void {
  listSessions().then((res) => {
    data.sessions = res.data.list || []
  })
}

function abortActiveStream(): void {
  // 切换/新建会话时旧流必须停掉：否则它在后台继续生成并计费，
  // 页面却已经看不到它了。abort 后由 send/resume 的 finally 收尾。
  controller?.abort()
}

/** 本次会话的范围载荷：按当前模式只提交会议或系列，两者都空表示全部。 */
function sessionScopePayload(): { meetingIds?: number[]; seriesIds?: number[] } {
  if (scopeMode.value === 'series') {
    return { seriesIds: scopeSeriesIds.value.length ? scopeSeriesIds.value : undefined }
  }
  return { meetingIds: scopeMeetingIds.value.length ? scopeMeetingIds.value : undefined }
}

function newSession(): void {
  abortActiveStream()
  createSession(sessionScopePayload()).then((res) => {
    data.sessions.unshift(res.data)
    sessionId.value = res.data.id
    messages.value = []
  })
}

function openSession(id: number): void {
  abortActiveStream()
  sessionId.value = id
  const session = data.sessions.find((item) => item.id === id)
  scopeMeetingIds.value = session?.scope_meeting_ids || []
  scopeSeriesIds.value = session?.scope_series_ids || []
  // 有系列范围时切到"按系列"，否则按会议展示
  scopeMode.value = scopeSeriesIds.value.length ? 'series' : 'meeting'
  listMessages(id).then((res) => {
    messages.value = (res.data.list || []).map((row) => ({
      id: row.id,
      role: row.role,
      content: row.content,
      citations: row.citations || [],
      tools: row.tools || [],
      stop_reason: row.stop_reason,
      error_message: row.error_message,
      notice: row.notice,
      run_status: row.run_status,
      thread_id: row.thread_id,
      trace_id: row.trace_id,
    }))
    scrollToBottom()
  })
}

function removeSession(id: number): void {
  ElMessageBox.confirm('删除后这次对话不再出现在列表里（消息记录保留在库中）。', '删除对话', {
    type: 'warning',
  })
    .then(() => deleteSession(id))
    .then(() => {
      if (sessionId.value === id) abortActiveStream()
      data.sessions = data.sessions.filter((item) => item.id !== id)
      if (sessionId.value === id) {
        sessionId.value = null
        messages.value = []
      }
    })
    .catch(() => {})
}

/**
 * 范围改了**不立刻**建会话：勾选过程中建会话会在列表里留下一串空对话。
 * 改到提问时才生效——范围变了就换一个新会话，因为同一会话里前后两次检索的
 * 范围不同，"这个结论是在哪个范围里得出的"就说不清了。
 */
function scopeChanged(): boolean {
  if (sessionId.value == null) return true
  const current = data.sessions.find((item) => item.id === sessionId.value)
  return (
    diffScope(current?.scope_meeting_ids || [], scopeMeetingIds.value) ||
    diffScope(current?.scope_series_ids || [], scopeSeriesIds.value)
  )
}

/** 两个 id 集合是否不同（顺序无关）。 */
function diffScope(before: number[], after: number[]): boolean {
  return before.length !== after.length || before.some((id) => !after.includes(id))
}

async function send(): Promise<void> {
  const text = question.value.trim()
  if (!text || streaming.value) return
  // 先置 streaming 再 await createSession：否则首次发送的等待窗口里
  // 快速双击会创建两个会话、发起两轮生成并双扣 token。
  streaming.value = true
  tokenTip.value = ''
  let target: PageMessage | undefined
  try {
    if (sessionId.value == null || scopeChanged()) {
      const created = await createSession(sessionScopePayload())
      data.sessions.unshift(created.data)
      sessionId.value = created.data.id
    }
    question.value = ''
    messages.value.push({ role: 'USER', content: text, citations: [], tools: [] })
    const assistant: PageMessage = {
      role: 'ASSISTANT',
      content: '',
      citations: [],
      tools: [],
      pending: true,
    }
    messages.value.push(assistant)
    const current = messages.value[messages.value.length - 1]
    target = current
    controller = new AbortController()
    scrollToBottom()

    await streamChat({
      sessionId: sessionId.value as number,
      question: text,
      signal: controller.signal,
      onEventId: (id) => {
        current.last_event_id = id
      },
      onEvent: (event) => {
        switch (event.type) {
          case 'meta':
            current.id = event.message_id
            current.run_status = 'RUNNING'
            current.thread_id = event.thread_id
            current.trace_id = event.trace_id
            // 恢复轮服务端会重放完整答案；先清掉本地半段，避免“前缀+完整答案”重复。
            if (event.resumed) current.content = event.answer_prefix ?? ''
            break
          case 'token':
            tokenBuffer += event.text
            scheduleFlush(current)
            break
          case 'tool_start':
            current.tools.push({
              call_id: event.call_id,
              name: event.name,
              arguments: event.arguments,
              summary: '执行中…',
              ok: null,
            })
            break
          case 'tool_end': {
            const entry = current.tools.find((item) => item.call_id === event.call_id)
            if (entry) entry.summary = event.summary
            else
              current.tools.push({
                call_id: event.call_id,
                name: event.name,
                arguments: {},
                summary: event.summary,
                ok: null,
              })
            break
          }
          case 'citation':
            if (!current.citations.some((item) => item.ref === event.citation.ref))
              current.citations.push(event.citation)
            break
          case 'done':
            current.stop_reason = event.stop_reason
            current.notice = event.notice || ''
            current.run_status = 'FINISHED'
            current.token_count = event.tokens.total_tokens
            current.citations = event.citations?.length ? event.citations : current.citations
            current.security_notice = securityNotice(event.security)
            tokenTip.value = `本轮 ${event.tokens.total_tokens} token`
            break
          case 'error':
            current.error_message = event.message
            current.run_status = 'FAILED'
            current.security_notice = securityNotice(event.security)
            break
        }
      },
    })
  } catch (error) {
    const message = error instanceof Error ? error.message : '问答失败'
    // 用户主动停止不算错误：后端会把这一轮如实记为"客户端断开"
    if (message.includes('abort')) {
      if (target) {
        target.notice = '已停止生成。'
        target.run_status = 'INTERRUPTED'
      }
    } else if (target?.id != null && !target.error_message) {
      // 已经收到 meta 后断线：服务端通常仍会落库为 INTERRUPTED，保留续传入口。
      target.notice = '连接中断，可继续生成。'
      target.run_status = 'INTERRUPTED'
    } else if (target) {
      target.error_message = message
      target.run_status = 'FAILED'
    } else {
      ElMessage.error(message)
    }
  } finally {
    if (target) {
      flushTokens(target)
      target.pending = false
    }
    streaming.value = false
    controller = null
    scrollToBottom()
  }
}

function stop(): void {
  controller?.abort()
}

/** 从已有 checkpoint 继续一轮中断的回答；服务端用 answer_prefix 对齐已输出部分。 */
async function resume(message: PageMessage): Promise<void> {
  if (streaming.value || message.id == null || sessionId.value == null) return
  streaming.value = true
  tokenBuffer = ''
  message.pending = true
  message.error_message = ''
  controller = new AbortController()
  try {
    await streamChat({
      sessionId: sessionId.value,
      question: '',
      resumeMessageId: message.id,
      lastEventId: message.last_event_id || undefined,
      signal: controller.signal,
      onEventId: (id) => {
        message.last_event_id = id
      },
      onEvent: (event) => {
        switch (event.type) {
          case 'meta':
            message.run_status = 'RUNNING'
            // 恢复轮以服务端重放的完整答案为准，先清掉本地半段
            message.content = event.resumed ? (event.answer_prefix ?? '') : message.content
            break
          case 'token':
            tokenBuffer += event.text
            scheduleFlush(message)
            break
          case 'tool_start':
            message.tools.push({
              call_id: event.call_id,
              name: event.name,
              arguments: event.arguments,
              summary: '执行中…',
              ok: null,
            })
            break
          case 'tool_end': {
            const entry = message.tools.find((item) => item.call_id === event.call_id)
            if (entry) entry.summary = event.summary
            else
              message.tools.push({
                call_id: event.call_id,
                name: event.name,
                arguments: {},
                summary: event.summary,
                ok: null,
              })
            break
          }
          case 'citation':
            if (!message.citations.some((item) => item.ref === event.citation.ref))
              message.citations.push(event.citation)
            break
          case 'done':
            message.stop_reason = event.stop_reason
            message.notice = event.notice || ''
            message.run_status = 'FINISHED'
            message.token_count = event.tokens.total_tokens
            message.citations = event.citations?.length ? event.citations : message.citations
            message.security_notice = securityNotice(event.security)
            tokenTip.value = `本轮 ${event.tokens.total_tokens} token`
            break
          case 'error':
            message.error_message = event.message
            message.run_status = 'FAILED'
            message.security_notice = securityNotice(event.security)
            break
          default:
            break
        }
      },
    })
  } catch (error) {
    const text = error instanceof Error ? error.message : '恢复失败'
    if (text.includes('abort')) message.run_status = 'INTERRUPTED'
    else if (!message.error_message) {
      // 恢复请求已经带上 message.id；网络/代理断流时保持可继续生成，
      // 不要把它误标成 FAILED 把入口藏掉。
      message.notice = '连接中断，可继续生成。'
      message.run_status = 'INTERRUPTED'
    }
  } finally {
    flushTokens(message)
    message.pending = false
    streaming.value = false
    controller = null
    scrollToBottom()
  }
}

/** 按帧合并 token：一帧里到达的所有增量一次性写入，减少渲染次数。 */
function scheduleFlush(target: PageMessage): void {
  if (flushHandle) return
  flushHandle = requestAnimationFrame(() => {
    flushHandle = 0
    flushTokens(target)
  })
}

function flushTokens(target: PageMessage): void {
  if (!tokenBuffer) return
  target.content += tokenBuffer
  tokenBuffer = ''
  scrollToBottom()
}

function scrollToBottom(): void {
  nextTick(() => {
    // el-scrollbar 把真实滚动容器藏在内部，拿它的 $el 找 .el-scrollbar__wrap
    const wrap = scrollRef.value?.$el?.querySelector('.el-scrollbar__wrap')
    if (wrap) wrap.scrollTop = wrap.scrollHeight
  })
}

/** 当前生效范围的标签：会话已建立时以服务端返回为准，否则显示待生效的选择。 */
const scopeChips = computed<{ key: string; label: string; type: 'primary' | 'success' }[]>(() => {
  const current = data.sessions.find((item) => item.id === sessionId.value)
  // 会话已建立时以服务端返回为准；未建立时只显示当前模式下待生效的选择
  const meetingIds = current
    ? current.scope_meeting_ids || []
    : scopeMode.value === 'meeting'
      ? scopeMeetingIds.value
      : []
  const seriesIds = current
    ? current.scope_series_ids || []
    : scopeMode.value === 'series'
      ? scopeSeriesIds.value
      : []
  const chips: { key: string; label: string; type: 'primary' | 'success' }[] = []
  for (const id of seriesIds) {
    const series = data.series.find((item) => item.id === id)
    chips.push({ key: `series-${id}`, label: `系列：${series?.name || id}`, type: 'success' })
  }
  for (const id of meetingIds) {
    const meeting = data.meetings.find((item) => item.id === id)
    chips.push({
      key: `meeting-${id}`,
      label: `会议：${meeting?.meeting_no || id}`,
      type: 'primary',
    })
  }
  return chips
})

/** 会话列表里的一行范围摘要。 */
function scopeSummary(session: ChatSessionInfo): string {
  const meetingCount = (session.scope_meeting_ids || []).length
  const seriesCount = (session.scope_series_ids || []).length
  if (!meetingCount && !seriesCount) return '全部会议'
  const parts: string[] = []
  if (seriesCount) parts.push(`${seriesCount} 个系列`)
  if (meetingCount) parts.push(`${meetingCount} 场会议`)
  return parts.join(' + ')
}

/** 安全标记 → 一句可读警示；没有命中规则时返回空串（不渲染警示条）。 */
function securityNotice(security: ChatSecurity | null | undefined): string {
  if (!security) return ''
  const flags = security.flags || []
  if (!flags.length && !security.blocked) return ''
  const rules = Array.from(new Set(flags.map((flag) => flag.rule).filter(Boolean)))
  const head = security.blocked
    ? '本轮回答被安全策略拦截，内容可能不可信'
    : '本轮内容命中安全规则，已按不可信内容处理'
  return rules.length ? `${head}：${rules.join('、')}` : head
}

/** 答案里真正引用到的卡片（按出现顺序）。未引用的检索结果只在工具折叠里体现。 */
function usedCitations(message: PageMessage): ChatCitation[] {
  const refs = splitCitations(message.content)
    .filter((part) => part.type === 'citation')
    .map((part) => (part as { ref: number }).ref)
  const unique = Array.from(new Set(refs))
  return unique
    .map((ref) => message.citations.find((item) => item.ref === ref))
    .filter((item): item is ChatCitation => Boolean(item))
}

function toolTitle(tools: ChatMessageInfo['tools']): string {
  const names = tools.map((item) => chatToolText(item.name)).join('、')
  return `查询过程（${tools.length} 次：${names}）`
}
</script>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: calc(100vh - 140px);
}

.chat-banner {
  margin-bottom: 0;
}

.chat-body {
  display: flex;
  gap: 8px;
  flex: 1;
  min-height: 0;
}

.chat-side {
  width: 280px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
}

.scope-box {
  border-top: 1px solid #f0f2f5;
  padding-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.scope-mode {
  margin-bottom: 2px;
}

.scope-chips {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding-bottom: 8px;
  margin-bottom: 4px;
  border-bottom: 1px solid #f0f2f5;
}

.scope-chip-label {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.5);
}

.scope-chip-tip {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.4);
  margin-left: 4px;
}

.scope-label {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.75);
  margin-bottom: 6px;
}

.scope-tip {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.45);
  margin-top: 4px;
}

.session-list {
  flex: 1;
  min-height: 0;
}

.session-item {
  position: relative;
  padding: 8px 26px 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 4px;
}

.session-item:hover {
  background: #f5f7fa;
}

.session-item.active {
  background: #ecf5ff;
}

.session-title {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.9);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-meta {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.45);
  margin-top: 2px;
}

.session-del {
  position: absolute;
  right: 8px;
  top: 10px;
  color: rgba(17, 24, 39, 0.35);
}

.session-del:hover {
  color: #f56c6c;
}

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.message-area {
  flex: 1;
  min-height: 0;
  padding-right: 6px;
}

.empty-tip {
  color: rgba(17, 24, 39, 0.45);
  font-size: 13px;
  padding: 12px;
  line-height: 1.8;
}

.message {
  margin-bottom: 14px;
}

.message-role {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.5);
  margin-bottom: 4px;
}

.message-text {
  font-size: 14px;
  line-height: 1.75;
  color: rgba(17, 24, 39, 0.9);
  white-space: pre-wrap;
  word-break: break-word;
}

.message-user .message-text {
  background: #f5f7fa;
  border-radius: 6px;
  padding: 8px 10px;
}

.typing {
  font-size: 12px;
  color: #409eff;
  margin-top: 4px;
}

.tool-trace {
  margin-top: 6px;
}

.tool-item {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.7);
  display: flex;
  gap: 8px;
  padding: 2px 0;
}

.tool-name {
  font-weight: 600;
}

.tool-raw {
  font-size: 11px;
  color: rgba(17, 24, 39, 0.4);
  font-family: monospace;
}

.tool-args {
  color: rgba(17, 24, 39, 0.45);
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-summary.failed {
  color: #f56c6c;
}

.citation-list {
  display: flex;
  flex-wrap: wrap;
}

.message-notice {
  margin-top: 6px;
}

.chat-input {
  border-top: 1px solid #f0f2f5;
  padding-top: 8px;
}

.chat-input-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 8px;
}

.token-tip {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.45);
}

.markdown :deep(p) {
  margin: 0 0 8px;
}

.markdown :deep(ul),
.markdown :deep(ol) {
  padding-left: 20px;
}
</style>
