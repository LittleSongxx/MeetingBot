<template>
  <div class="citation-card">
    <div class="citation-head">
      <span class="citation-ref">[{{ citation.ref }}]</span>
      <span class="citation-where">{{ where }}</span>
      <span v-if="authorityText" class="citation-authority">{{ authorityText }}</span>
    </div>
    <div class="citation-quote">{{ citation.quote }}</div>
    <div class="citation-foot">
      <!-- 点回会议：带 meetingId 跳到转写页（那边本来就支持这个 query 参数） -->
      <button type="button" class="citation-link" @click="openMeeting">查看会议原话</button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 引用卡片：把一条 `[n]` 引用渲染成"哪场会 · 谁 · 什么时候"加原句。
 *
 * 刻意不用 Element Plus 组件：这张卡片要被单测直接挂载（不带整套 UI 库），
 * 用原生元素 + scoped 样式就够了，也让"点击跳转"的断言只依赖 router。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import type { ChatCitation } from '@/api/chat'

const props = defineProps<{ citation: ChatCitation }>()

const router = useRouter()

/** 出处的一行摘要：会议标题 / 说话人 / 秒数（缺什么就少显示什么，不显示拼接痕迹）。 */
const where = computed(() => {
  const parts: string[] = []
  parts.push(props.citation.meeting_title || `会议 ${props.citation.meeting_id}`)
  if (props.citation.speaker) parts.push(props.citation.speaker)
  if (typeof props.citation.start_second === 'number' && props.citation.start_second >= 0) {
    const minutes = Math.floor(props.citation.start_second / 60)
    const seconds = props.citation.start_second % 60
    parts.push(`${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`)
  }
  return parts.join(' · ')
})

/** 权威级只在值得提醒时显示：草稿与转写原话都要让用户看得见"这不是已确认的结论"。 */
const authorityText = computed(() => {
  switch (props.citation.authority) {
    case 'CONFIRMED':
      return '已确认纪要'
    case 'DRAFT':
      return '草稿纪要'
    case 'RAW':
      return '转写原话'
    default:
      return ''
  }
})

function openMeeting(): void {
  router.push({
    path: '/manager/transcription',
    query: { meetingId: String(props.citation.meeting_id) },
  })
}
</script>

<style scoped>
.citation-card {
  border: 1px solid #e4e7ed;
  border-left: 3px solid #409eff;
  border-radius: 6px;
  padding: 8px 10px;
  background: #fafcff;
  margin: 6px 8px 6px 0;
  min-width: 260px;
  max-width: 420px;
}

.citation-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: rgba(17, 24, 39, 0.6);
  margin-bottom: 4px;
}

.citation-ref {
  color: #409eff;
  font-weight: 600;
}

.citation-authority {
  border: 1px solid #d9ecff;
  border-radius: 4px;
  padding: 0 4px;
  color: #409eff;
  background: #ecf5ff;
}

.citation-quote {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.9);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.citation-foot {
  margin-top: 6px;
}

.citation-link {
  border: none;
  background: none;
  padding: 0;
  color: #409eff;
  font-size: 12px;
  cursor: pointer;
}

.citation-link:hover {
  text-decoration: underline;
}
</style>
