<template>
  <div v-if="items.length" class="evidence-list">
    <div v-for="(item, index) in items" :key="index" class="evidence-item">
      <div class="evidence-quote">{{ quoteOf(item) }}</div>
      <div v-if="metaText(item)" class="evidence-meta">{{ metaText(item) }}</div>
    </div>
  </div>
  <span v-else class="evidence-empty">暂无证据</span>
</template>

<script setup lang="ts">
/**
 * 证据片段列表：记忆、待办、状态引用卡片共用。
 *
 * evidence_json 的形状不统一（有的是一条对象，有的是数组），这里统一收成数组渲染；
 * 缺字段就少显示一段，不拼出 "undefined" 这类痕迹。
 */
import { computed } from 'vue'

import type { MemoryEvidence } from '@/api/memory'
import { formatSeconds } from '@/utils/format'

const props = defineProps<{ evidence: MemoryEvidence[] | MemoryEvidence | null | undefined }>()

/** evidence_json 既可能是数组也可能是一条对象，向外统一成数组。 */
const items = computed<MemoryEvidence[]>(() => {
  const value = props.evidence
  if (!value) return []
  return Array.isArray(value) ? value.filter((item) => Boolean(item)) : [value]
})

/** 纪要字段名 → 中文，未登记的字段直接显示原名。 */
const FIELD_TEXT: Record<string, string> = {
  decisions: '决策',
  conclusions: '结论',
  pending_items: '待办',
  action_items: '待办',
  risks: '风险',
  topics: '议题',
  viewpoints: '观点',
  summary: '摘要',
}

function quoteOf(item: MemoryEvidence): string {
  return item.quote || item.source_text || '-'
}

/** 证据的一行出处：说话人 · 时间 · 纪要字段。 */
function metaText(item: MemoryEvidence): string {
  const parts: string[] = []
  if (item.speaker) parts.push(item.speaker)
  // 优先用秒，其次用毫秒换算；两个都没有就不显示时间
  const seconds =
    typeof item.start_second === 'number'
      ? item.start_second
      : typeof item.start_ms === 'number'
        ? item.start_ms / 1000
        : null
  const clock = formatSeconds(seconds)
  if (clock) parts.push(clock)
  if (item.field) parts.push(FIELD_TEXT[item.field] || item.field)
  return parts.join(' · ')
}
</script>

<style scoped>
.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}

.evidence-item {
  border-left: 2px solid #c6e2ff;
  background: #f8fbff;
  border-radius: 4px;
  padding: 6px 8px;
}

.evidence-quote {
  font-size: 13px;
  line-height: 1.6;
  color: rgba(17, 24, 39, 0.85);
  word-break: break-word;
}

.evidence-meta {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.45);
  margin-top: 2px;
}

.evidence-empty {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.4);
}
</style>
