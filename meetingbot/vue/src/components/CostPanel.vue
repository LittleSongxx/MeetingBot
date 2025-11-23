<template>
  <div class="card cost-panel">
    <div class="panel-head">
      <div class="chart-title">成本与缓存</div>
      <span class="panel-tip">按会议统计已定价调用的成本；未定价调用单独标注，不按 0 元计入</span>
    </div>
    <div class="cost-grid" v-loading="data.loading">
      <div class="cost-card">
        <div class="cost-label">单轮问答成本</div>
        <div class="cost-value">
          {{
            result.by_turn?.avg_cost_micro == null
              ? '-'
              : formatMicro(result.by_turn.avg_cost_micro)
          }}<span>元</span>
        </div>
        <div class="cost-note">
          共 {{ formatNumber(result.by_turn?.turn_count) }} 轮 /
          {{ formatNumber(result.by_turn?.call_count) }} 次调用
        </div>
      </div>
      <div class="cost-card">
        <div class="cost-label">单轮平均调用</div>
        <div class="cost-value">
          {{ result.by_turn?.avg_calls == null ? '-' : result.by_turn.avg_calls }}<span>次</span>
        </div>
        <div class="cost-note">按助手消息聚合成一轮</div>
      </div>
      <div class="cost-card">
        <div class="cost-label">缓存命中率</div>
        <div class="cost-value">{{ formatPercent(result.cache?.rate) }}</div>
        <div class="cost-note">
          <template v-if="result.cache?.reported">
            命中 {{ formatNumber(result.cache?.hit_tokens) }} / 输入
            {{ formatNumber(result.cache?.input_tokens) }} token
          </template>
          <template v-else>供应商未上报缓存命中 token，无法计算</template>
        </div>
      </div>
      <div class="cost-card">
        <div class="cost-label">未定价调用</div>
        <div class="cost-value">{{ formatNumber(result.unpriced_count) }}<span>次</span></div>
        <div class="cost-note">这些调用没有单价，成本未知而不是免费</div>
      </div>
    </div>
    <el-table
      :data="result.by_meeting || []"
      stripe
      v-loading="data.loading"
      empty-text="该时间段没有成本记录"
      max-height="320"
    >
      <el-table-column prop="meeting_title" label="会议" min-width="260" show-overflow-tooltip>
        <template #default="scope">
          {{ scope.row.meeting_title || `会议 ${scope.row.meeting_id}` }}
        </template>
      </el-table-column>
      <el-table-column prop="call_count" label="调用量" width="100" />
      <el-table-column label="成本(元)" width="140">
        <template #default="scope">{{ formatMicro(scope.row.cost_micro) }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
/**
 * 成本与缓存卡片：数据来自 GET /observability/cost?date_from&date_to。
 *
 * 页面传"已提交查询"的日期范围（而不是日期控件的实时值），避免用户只拨动
 * 日期控件就触发一串请求；查询按钮点击后父组件更新 range，这里再拉数据。
 */
import { reactive, watch } from 'vue'

import { getCost, type CostResult } from '@/api/cost'
import { formatMicro, formatNumber, formatPercent } from '@/utils/format'

const props = defineProps<{ dateFrom: string; dateTo: string }>()

const data = reactive({ loading: false })

const emptyResult = (): CostResult => ({
  by_meeting: [],
  by_turn: { turn_count: 0, call_count: 0, avg_cost_micro: null, avg_calls: null },
  cache: { hit_tokens: 0, input_tokens: 0, rate: null, reported: false },
  unpriced_count: 0,
})

const result = reactive<CostResult>(emptyResult())

function load(): void {
  if (!props.dateFrom || !props.dateTo) return
  data.loading = true
  getCost(props.dateFrom, props.dateTo)
    .then((res) => {
      // 返回体缺字段时用空结构兜底，免得模板里到处判空
      Object.assign(result, emptyResult(), res.data || {})
    })
    .catch(() => {
      // 错误提示由 request 拦截器统一弹出；这里只保证不把旧数据当新数据
      Object.assign(result, emptyResult())
    })
    .finally(() => (data.loading = false))
}

// 只在父组件提交的日期范围变化时重查；immediate 覆盖首次进入页面
watch(() => [props.dateFrom, props.dateTo], load, { immediate: true })
</script>

<style scoped>
.cost-panel {
  margin-top: 10px;
}

.panel-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}

.panel-tip {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.45);
}

.cost-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.cost-card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fafbfc;
}

.cost-label {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.55);
}

.cost-value {
  font-size: 24px;
  font-weight: 600;
  color: rgba(17, 24, 39, 0.9);
  margin-top: 4px;
}

.cost-value span {
  font-size: 12px;
  font-weight: 400;
  color: rgba(17, 24, 39, 0.45);
  margin-left: 4px;
}

.cost-note {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.45);
  margin-top: 4px;
}

@media (max-width: 1200px) {
  .cost-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
