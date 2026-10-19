<template>
  <div class="memory-review">
    <div v-if="loading" class="memory-loading">加载中…</div>
    <div v-else-if="!items.length" class="memory-empty">{{ emptyText }}</div>
    <div v-for="item in items" v-else :key="item.id" class="memory-card">
      <div class="memory-head">
        <span class="memory-kind">{{ kindText(item.kind) }}</span>
        <span class="memory-scope">{{ scopeText(item.scope_type) }}</span>
        <span class="memory-status" :class="`status-${item.status.toLowerCase()}`">
          {{ statusText(item.status) }}
        </span>
        <span class="memory-time">{{ item.create_time }}</span>
      </div>
      <div class="memory-content">{{ item.content }}</div>
      <div class="memory-source">
        来源：{{ sourceText(item) }}
        <button
          v-if="item.source_meeting_id"
          type="button"
          class="memory-link"
          @click="emit('open-meeting', item)"
        >
          查看会议
        </button>
      </div>
      <EvidenceList :evidence="item.evidence_json" />
      <div v-if="item.supersedes_id" class="memory-supersede">
        确认后将替代记忆 #{{ item.supersedes_id }}
      </div>
      <div v-if="!readonly" class="memory-actions">
        <template v-if="item.status === 'PROPOSED'">
          <button type="button" class="memory-btn primary" @click="emit('confirm', item)">
            确认
          </button>
          <button type="button" class="memory-btn" @click="emit('reject', item)">拒绝</button>
        </template>
        <button
          v-if="item.status === 'CONFIRMED'"
          type="button"
          class="memory-btn"
          @click="emit('expire', item)"
        >
          标记过期
        </button>
        <button type="button" class="memory-btn" @click="emit('edit', item)">编辑</button>
      </div>
      <div v-else class="memory-readonly">只读</div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 记忆审核列表：待确认候选与已确认记忆共用。
 *
 * 确认/拒绝/编辑/过期都只 emit，具体弹窗与接口调用由页面负责——审核动作需要
 * 二次确认与备注，这些交互放进组件会让组件同时承担"展示 + 表单 + 请求"三件事。
 */
import EvidenceList from '@/components/EvidenceList.vue'
import type { MeetingMemoryItem } from '@/api/memory'
import { MEMORY_KIND, MEMORY_SCOPE, MEMORY_STATUS, MEMORY_SOURCE_TEXT } from '@/constants/status'

withDefaults(
  defineProps<{
    items: MeetingMemoryItem[]
    loading?: boolean
    /** 员工只读：只展示内容，不出现审核按钮 */
    readonly?: boolean
    emptyText?: string
  }>(),
  { loading: false, readonly: false, emptyText: '暂无记忆' },
)

const emit = defineEmits<{
  (event: 'confirm', item: MeetingMemoryItem): void
  (event: 'reject', item: MeetingMemoryItem): void
  (event: 'edit', item: MeetingMemoryItem): void
  (event: 'expire', item: MeetingMemoryItem): void
  (event: 'open-meeting', item: MeetingMemoryItem): void
}>()

const kindText = MEMORY_KIND.text
const scopeText = MEMORY_SCOPE.text
const statusText = MEMORY_STATUS.text

/** 来源一行：优先显示来源会议标题，没关联会议时回落到来源类型。 */
function sourceText(item: MeetingMemoryItem): string {
  if (item.source_meeting_title) return item.source_meeting_title
  if (item.source_meeting_id) return `会议 ${item.source_meeting_id}`
  return MEMORY_SOURCE_TEXT(item.source_type)
}
</script>

<style scoped>
.memory-review {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.memory-card {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fff;
}

.memory-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: rgba(17, 24, 39, 0.55);
}

.memory-kind {
  color: #409eff;
  background: #ecf5ff;
  border-radius: 3px;
  padding: 0 5px;
}

.memory-scope {
  color: rgba(17, 24, 39, 0.55);
  background: #f4f4f5;
  border-radius: 3px;
  padding: 0 5px;
}

.memory-status {
  border-radius: 3px;
  padding: 0 5px;
}

.status-proposed {
  color: #e6a23c;
  background: #fdf6ec;
}

.status-confirmed {
  color: #529b2e;
  background: #f0f9eb;
}

.status-rejected,
.status-expired,
.status-superseded {
  color: rgba(17, 24, 39, 0.55);
  background: #f4f4f5;
}

.memory-time {
  margin-left: auto;
}

.memory-content {
  font-size: 13px;
  line-height: 1.65;
  color: rgba(17, 24, 39, 0.9);
  margin-top: 6px;
  white-space: pre-wrap;
  word-break: break-word;
}

.memory-source {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.5);
  margin-top: 6px;
}

.memory-link {
  border: none;
  background: none;
  padding: 0 0 0 6px;
  color: #409eff;
  font-size: 12px;
  cursor: pointer;
}

.memory-link:hover {
  text-decoration: underline;
}

.memory-supersede {
  font-size: 12px;
  color: #e6a23c;
  margin-top: 6px;
}

.memory-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.memory-btn {
  border: 1px solid #dcdfe6;
  background: #fff;
  color: rgba(17, 24, 39, 0.75);
  border-radius: 4px;
  font-size: 12px;
  padding: 3px 10px;
  cursor: pointer;
}

.memory-btn:hover {
  border-color: #a0cfff;
  color: #409eff;
}

.memory-btn.primary {
  border-color: #409eff;
  background: #409eff;
  color: #fff;
}

.memory-btn.primary:hover {
  background: #66b1ff;
}

.memory-readonly,
.memory-loading,
.memory-empty {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.45);
  padding: 16px 0;
  text-align: center;
}
</style>
