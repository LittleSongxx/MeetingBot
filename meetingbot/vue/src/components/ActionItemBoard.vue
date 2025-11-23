<template>
  <div class="action-board">
    <div v-for="column in COLUMNS" :key="column.key" class="board-column">
      <div class="board-column-head" :class="`tone-${column.tone}`">
        <span>{{ column.label }}</span>
        <span class="board-count">{{ grouped[column.key].length }}</span>
      </div>
      <div class="board-column-body">
        <div v-if="loading" class="board-loading">加载中…</div>
        <template v-else-if="grouped[column.key].length">
          <div
            v-for="item in grouped[column.key]"
            :key="item.id"
            class="board-card"
            @click="emit('select', item)"
          >
            <div class="board-content">{{ item.content }}</div>
            <div class="board-meta">
              <span v-if="item.series_name" class="board-series">{{ item.series_name }}</span>
              <span v-if="item.owner_name" class="board-owner">{{ item.owner_name }}</span>
              <span v-if="isOverdue(item.deadline, item.status)" class="board-overdue">已逾期</span>
              <span v-else-if="item.deadline_text" class="board-deadline">
                {{ item.deadline_text }}
              </span>
              <span v-else-if="item.deadline" class="board-deadline">
                {{ formatMinute(item.deadline) }}
              </span>
            </div>
            <div
              v-if="item.last_seen_meeting_id && item.last_seen_meeting_id !== item.meeting_id"
              class="board-carried"
            >
              结转自会议 {{ item.last_seen_meeting_id }}
            </div>
          </div>
        </template>
        <div v-else class="board-empty">暂无</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 待办看板：按状态分四列（待处理 / 受阻 / 已完成 / 已取消）。
 *
 * 只做展示与点击选中，状态更新交给页面弹窗——看板上直接改状态会让"谁改的、
 * 为什么改"这些留痕信息无处填写（HITL 要求状态变更必须带备注）。
 * 刻意不用 Element Plus：看板要被单测直接挂载，原生元素足够。
 */
import { computed } from 'vue'

import type { ActionItem } from '@/api/actionItem'
import { isOverdue, formatMinute } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    items: ActionItem[]
    /** 首次加载中：显示"加载中…"而不是"暂无" */
    loading?: boolean
  }>(),
  { loading: false },
)

const emit = defineEmits<{ (event: 'select', item: ActionItem): void }>()

/** 四列的顺序固定：待处理 → 受阻 → 已完成 → 已取消。 */
const COLUMNS: { key: string; label: string; tone: string }[] = [
  { key: 'OPEN', label: '待处理', tone: 'warning' },
  { key: 'BLOCKED', label: '受阻', tone: 'danger' },
  { key: 'DONE', label: '已完成', tone: 'success' },
  { key: 'CANCELLED', label: '已取消', tone: 'info' },
]

const grouped = computed<Record<string, ActionItem[]>>(() => {
  const result: Record<string, ActionItem[]> = {}
  for (const column of COLUMNS) result[column.key] = []
  for (const item of props.items) {
    // 未知状态不丢数据：挂到"待处理"列（后端新增状态时看板仍能显示）
    const key = result[item.status] ? item.status : 'OPEN'
    result[key].push(item)
  }
  return result
})
</script>

<style scoped>
.action-board {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.board-column {
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: #f7f8fa;
  border-radius: 8px;
  max-height: 560px;
}

.board-column-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
  padding: 9px 12px;
  border-radius: 8px 8px 0 0;
}

.tone-warning {
  color: #b88230;
  background: #fdf6ec;
}

.tone-danger {
  color: #c45656;
  background: #fef0f0;
}

.tone-success {
  color: #529b2e;
  background: #f0f9eb;
}

.tone-info {
  color: rgba(17, 24, 39, 0.6);
  background: #f4f4f5;
}

.board-count {
  font-weight: 400;
  font-size: 12px;
  opacity: 0.8;
}

.board-column-body {
  flex: 1;
  min-height: 80px;
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.board-card {
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 9px 10px;
  cursor: pointer;
  transition:
    box-shadow 0.15s,
    border-color 0.15s;
}

.board-card:hover {
  border-color: #a0cfff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.12);
}

.board-content {
  font-size: 13px;
  line-height: 1.55;
  color: rgba(17, 24, 39, 0.9);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}

.board-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  font-size: 12px;
  color: rgba(17, 24, 39, 0.55);
}

.board-series {
  color: #409eff;
  background: #ecf5ff;
  border-radius: 3px;
  padding: 0 4px;
}

.board-overdue {
  color: #f56c6c;
  font-weight: 600;
}

.board-carried {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.4);
  margin-top: 4px;
}

.board-loading,
.board-empty {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.4);
  text-align: center;
  padding: 18px 0;
}

@media (max-width: 1200px) {
  .action-board {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
