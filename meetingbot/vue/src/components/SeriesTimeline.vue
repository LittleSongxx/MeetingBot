<template>
  <div class="series-timeline">
    <div v-if="loading" class="timeline-empty">加载中…</div>
    <div v-else-if="!meetings.length" class="timeline-empty">这个系列还没有会议</div>
    <div v-for="(meeting, index) in meetings" v-else :key="meeting.id" class="timeline-item">
      <div class="timeline-axis">
        <span class="timeline-dot" :class="`tone-${statusTag(meeting.status)}`"></span>
        <span v-if="index < meetings.length - 1" class="timeline-line"></span>
      </div>
      <div class="timeline-body">
        <div class="timeline-head">
          <span class="timeline-time">{{ formatMinute(meeting.start_time) }}</span>
          <span class="timeline-status" :class="`tone-${statusTag(meeting.status)}`">
            {{ statusText(meeting.status) }}
          </span>
          <button type="button" class="timeline-link" @click="emit('open', meeting)">
            查看会议
          </button>
        </div>
        <div class="timeline-title">{{ meeting.title }}</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 系列详情里的会议时间线：按后端返回顺序展示（后端已按开始时间倒序）。
 * 点击"查看会议"跳转会议管理页；状态颜色取自会议状态机，与其他页面一致。
 */
import type { SeriesMeetingItem } from '@/api/series'
import { MEETING_STATUS } from '@/constants/status'
import { formatMinute } from '@/utils/format'

withDefaults(defineProps<{ meetings: SeriesMeetingItem[]; loading?: boolean }>(), {
  loading: false,
})

const emit = defineEmits<{ (event: 'open', meeting: SeriesMeetingItem): void }>()

const statusText = MEETING_STATUS.text

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'
// 常量模块的 tag 返回 string；会议状态只有四种，颜色一定取自 el-tag 的五档
const statusTag = MEETING_STATUS.tag as (status?: string) => TagType
</script>

<style scoped>
.series-timeline {
  padding: 4px 0;
}

.timeline-item {
  display: flex;
  gap: 10px;
}

.timeline-axis {
  position: relative;
  width: 12px;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-top: 4px;
}

.timeline-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
  background: rgba(17, 24, 39, 0.25);
}

.timeline-line {
  flex: 1;
  width: 1px;
  background: #e4e7ed;
  margin-top: 4px;
}

.timeline-body {
  flex: 1;
  min-width: 0;
  padding-bottom: 16px;
}

.timeline-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: rgba(17, 24, 39, 0.55);
}

.timeline-time {
  font-variant-numeric: tabular-nums;
}

.timeline-status {
  border-radius: 3px;
  padding: 0 5px;
}

.timeline-link {
  margin-left: auto;
  border: none;
  background: none;
  padding: 0;
  color: #409eff;
  font-size: 12px;
  cursor: pointer;
}

.timeline-link:hover {
  text-decoration: underline;
}

.timeline-title {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.9);
  margin-top: 4px;
  word-break: break-word;
}

.timeline-empty {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.45);
  padding: 14px 0;
  text-align: center;
}
</style>
