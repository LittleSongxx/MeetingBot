<template>
  <div class="state-card">
    <div class="state-head">
      <span class="state-ref">[{{ citation.ref }}]</span>
      <span class="state-kind">{{ typeText }}</span>
      <span v-if="statusText" class="state-status" :class="statusClass">{{ statusText }}</span>
      <span v-if="citation.series_name" class="state-series">{{ citation.series_name }}</span>
    </div>
    <div class="state-content">{{ content }}</div>
    <div v-if="citation.owner_name || citation.deadline_text" class="state-fields">
      <span v-if="citation.owner_name" class="state-field">负责人：{{ citation.owner_name }}</span>
      <span v-if="citation.deadline_text" class="state-field"
        >截止：{{ citation.deadline_text }}</span
      >
    </div>
    <div v-if="meetingText" class="state-meeting">来源：{{ meetingText }}</div>
    <div class="state-foot">
      <!-- 待办跳待办页、记忆跳记忆页，与会话里的状态引用一一对应 -->
      <button v-if="canOpenState" type="button" class="state-link" @click="openState">
        查看{{ targetName }}
      </button>
      <button v-if="citation.meeting_id" type="button" class="state-link" @click="openMeeting">
        会议原话
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 状态引用卡片：`kind="state"` 的引用不是原文切片，而是决策/待办/偏好等
 * 人工确认过的状态（来自 query_meeting_state）。要展示的是**当前状态**：
 * 类型、状态、负责人、截止时间、来源会议与证据原句，并点回对应的管理页面。
 *
 * 与 CitationCard 一样刻意不用 Element Plus：这张卡片要被单测直接挂载，
 * 用原生元素 + scoped 样式即可。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import type { ChatCitation } from '@/api/chat'
import { ACTION_ITEM_STATUS, MEMORY_KIND, MEMORY_STATUS } from '@/constants/status'

const props = defineProps<{ citation: ChatCitation }>()

const router = useRouter()

/** 后端 state_service 给待办的 kind 是小写 action_item，记忆是大写 DECISION 等，统一收口。 */
const stateType = computed(() => (props.citation.state_type || '').toUpperCase())

/** 待办与记忆是两套状态机，文案/颜色不能混用。 */
const isActionItem = computed(() => stateType.value === 'ACTION_ITEM')

const typeText = computed(() => {
  if (isActionItem.value) return '待办'
  return MEMORY_KIND.text(stateType.value) || '状态'
})

const statusText = computed(() => {
  if (!props.citation.status) return ''
  const status = props.citation.status ?? undefined
  return isActionItem.value ? ACTION_ITEM_STATUS.text(status) : MEMORY_STATUS.text(status)
})

/** 状态颜色按状态机取，未知状态回落成灰色。 */
const statusClass = computed(() => {
  const status = props.citation.status ?? undefined
  const tag = isActionItem.value ? ACTION_ITEM_STATUS.tag(status) : MEMORY_STATUS.tag(status)
  return tag ? `tone-${tag}` : 'tone-info'
})

/** 正文优先用完整条目正文（source_text），没有才退回引用原句。 */
const content = computed(() => props.citation.source_text || props.citation.quote || '-')

const meetingText = computed(() => {
  if (props.citation.meeting_title) return props.citation.meeting_title
  return props.citation.meeting_id ? `会议 ${props.citation.meeting_id}` : ''
})

const canOpenState = computed(
  () => props.citation.state_id != null && Boolean(props.citation.state_id),
)

const targetName = computed(() => (isActionItem.value ? '待办' : '记忆'))

function openState(): void {
  const id = props.citation.state_id
  if (id == null) return
  if (isActionItem.value) {
    router.push({ path: '/manager/actionItems', query: { actionItemId: String(id) } })
  } else {
    router.push({ path: '/manager/memory', query: { memoryId: String(id) } })
  }
}

function openMeeting(): void {
  router.push({
    path: '/manager/transcription',
    query: { meetingId: String(props.citation.meeting_id) },
  })
}
</script>

<style scoped>
.state-card {
  border: 1px solid #e4e7ed;
  border-left: 3px solid #67c23a;
  border-radius: 6px;
  padding: 8px 10px;
  background: #fbfefb;
  margin: 6px 8px 6px 0;
  min-width: 260px;
  max-width: 460px;
}

.state-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
  color: rgba(17, 24, 39, 0.6);
  margin-bottom: 4px;
}

.state-ref {
  color: #67c23a;
  font-weight: 600;
}

.state-kind {
  border-radius: 4px;
  padding: 0 4px;
  color: #409eff;
  background: #ecf5ff;
}

.state-status {
  border-radius: 4px;
  padding: 0 4px;
}

.tone-primary {
  color: #409eff;
  background: #ecf5ff;
}

.tone-success {
  color: #67c23a;
  background: #f0f9eb;
}

.tone-warning {
  color: #e6a23c;
  background: #fdf6ec;
}

.tone-danger {
  color: #f56c6c;
  background: #fef0f0;
}

.tone-info {
  color: rgba(17, 24, 39, 0.6);
  background: #f4f4f5;
}

.state-series {
  color: rgba(17, 24, 39, 0.45);
}

.state-content {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.9);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.state-fields {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 6px;
}

.state-field {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.7);
}

.state-meeting {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.45);
  margin-top: 4px;
}

.state-foot {
  display: flex;
  gap: 14px;
  margin-top: 6px;
}

.state-link {
  border: none;
  background: none;
  padding: 0;
  color: #409eff;
  font-size: 12px;
  cursor: pointer;
}

.state-link:hover {
  text-decoration: underline;
}
</style>
