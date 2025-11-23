<template>
  <div class="card online-eval-panel">
    <div class="panel-head">
      <div class="chart-title">在线评测</div>
      <span class="panel-tip">
        抽样最近的问答，用裁判模型评忠实度与相关性；单次最多 50 条，结果只落库不阻塞问答
      </span>
      <div class="panel-actions">
        <el-select v-model="data.sampleSize" style="width: 120px" :disabled="data.running">
          <el-option v-for="size in SAMPLE_SIZES" :key="size" :label="`${size} 条`" :value="size" />
        </el-select>
        <el-button type="primary" :loading="data.running" @click="run">开始评测</el-button>
      </div>
    </div>

    <el-alert
      v-if="data.running"
      class="panel-alert"
      type="warning"
      :closable="false"
      show-icon
      :title="runningTitle"
    />

    <!-- 最新一次运行的均分：没有运行记录时整块不出现，不留四张空卡片 -->
    <div v-if="latestSummary" class="eval-grid">
      <div class="eval-card">
        <div class="eval-label">平均忠实度</div>
        <div class="eval-value">{{ formatPercent(latestSummary.avg_faithfulness) }}</div>
      </div>
      <div class="eval-card">
        <div class="eval-label">平均相关性</div>
        <div class="eval-value">{{ formatPercent(latestSummary.avg_relevance) }}</div>
      </div>
      <div class="eval-card">
        <div class="eval-label">通过率</div>
        <div class="eval-value">{{ formatPercent(latestSummary.pass_rate) }}</div>
      </div>
      <div class="eval-card">
        <div class="eval-label">失败样例</div>
        <div class="eval-value">
          {{ latestSummary.failed_count }}<span>/ {{ latestSummary.case_count }} 条</span>
        </div>
        <button
          v-if="latestSummary.failed_count > 0 && latestRun"
          type="button"
          class="eval-link"
          @click="openDetail(latestRun.id, true)"
        >
          查看失败样例
        </button>
      </div>
    </div>

    <el-table :data="data.runs" stripe v-loading="data.loading" empty-text="还没有评测记录">
      <el-table-column prop="id" label="#" width="70" />
      <el-table-column label="状态" width="110">
        <template #default="scope">
          <el-tag :type="statusTag(scope.row.status)">{{ statusText(scope.row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="sample_size" label="样本" width="80" />
      <el-table-column label="忠实度" width="100">
        <template #default="scope">
          {{ formatPercent(scope.row.summary_json?.avg_faithfulness) }}
        </template>
      </el-table-column>
      <el-table-column label="相关性" width="100">
        <template #default="scope">
          {{ formatPercent(scope.row.summary_json?.avg_relevance) }}
        </template>
      </el-table-column>
      <el-table-column label="通过率" width="100">
        <template #default="scope">
          {{ formatPercent(scope.row.summary_json?.pass_rate) }}
        </template>
      </el-table-column>
      <el-table-column label="失败" width="80">
        <template #default="scope">{{ scope.row.summary_json?.failed_count ?? '-' }}</template>
      </el-table-column>
      <el-table-column label="开始时间" width="170">
        <template #default="scope">{{ formatMinute(scope.row.started_at) }}</template>
      </el-table-column>
      <el-table-column label="结束时间" width="170">
        <template #default="scope">{{ formatMinute(scope.row.finished_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="scope">
          <el-button link type="primary" @click="openDetail(scope.row.id, false)">明细</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer v-model="data.detailVisible" :title="drawerTitle" size="70%" destroy-on-close>
      <div v-if="data.detail" class="detail-body" v-loading="data.detailLoading">
        <el-alert
          v-if="data.detail.error_message"
          type="error"
          :closable="false"
          show-icon
          :title="data.detail.error_message"
        />
        <div class="detail-summary">
          <span>状态：{{ statusText(data.detail.status) }}</span>
          <span>样本：{{ data.detail.sample_size }}</span>
          <span>忠实度：{{ formatPercent(data.detail.summary_json?.avg_faithfulness) }}</span>
          <span>相关性：{{ formatPercent(data.detail.summary_json?.avg_relevance) }}</span>
          <span>通过率：{{ formatPercent(data.detail.summary_json?.pass_rate) }}</span>
          <span>失败：{{ data.detail.summary_json?.failed_count ?? '-' }}</span>
        </div>
        <div class="detail-toolbar">
          <el-radio-group v-model="data.onlyFailed" size="small">
            <el-radio-button :value="false">全部 {{ data.detail.cases.length }}</el-radio-button>
            <el-radio-button :value="true">仅失败 {{ failedCases.length }}</el-radio-button>
          </el-radio-group>
        </div>
        <el-table :data="visibleCases" stripe empty-text="没有符合条件的样例" max-height="520">
          <el-table-column prop="question" label="问题" min-width="200" show-overflow-tooltip />
          <el-table-column prop="answer" label="回答" min-width="260" show-overflow-tooltip />
          <el-table-column label="评分" min-width="220" show-overflow-tooltip>
            <template #default="scope">{{ scoreText(scope.row.scores_json) }}</template>
          </el-table-column>
          <el-table-column label="判定证据" min-width="220" show-overflow-tooltip>
            <template #default="scope">{{ evidenceText(scope.row.scores_json) }}</template>
          </el-table-column>
          <el-table-column prop="judge_model" label="裁判模型" width="150" />
          <el-table-column
            prop="error_message"
            label="错误"
            min-width="180"
            show-overflow-tooltip
          />
        </el-table>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
/**
 * 在线评测卡片：触发一次抽样评测 + 轮询运行状态 + 均分/失败样例展示。
 *
 * 评测由后端异步执行：POST /run 立即返回 RUNNING 记录，页面每 2.5 秒查一次
 * GET /runs/{id}，状态离开 RUNNING 就停表并刷新历史。离开页面必须清掉定时器，
 * 否则切走后仍在后台打接口。
 */
import { computed, onMounted, onUnmounted, reactive } from 'vue'

import {
  getOnlineEvalRun,
  listOnlineEvalRuns,
  runOnlineEval,
  type OnlineEvalCase,
  type OnlineEvalRun,
  type OnlineEvalRunDetail,
  type OnlineEvalSummary,
} from '@/api/onlineEval'
import { ONLINE_EVAL_STATUS } from '@/constants/status'
import { formatMinute, formatPercent } from '@/utils/format'

const SAMPLE_SIZES = [10, 20, 30, 50]
const POLL_INTERVAL_MS = 2500

interface OnlineEvalState {
  runs: OnlineEvalRun[]
  loading: boolean
  running: boolean
  sampleSize: number
  /** 本次触发/续查的运行 id（POST 只回 run_id，轮询按它查） */
  activeRunId: number | null
  /** 轮询到的最近一次运行详情（无 cases 时也可当运行记录用） */
  activeRun: OnlineEvalRun | null
  detail: OnlineEvalRunDetail | null
  detailVisible: boolean
  detailLoading: boolean
  onlyFailed: boolean
}

const data = reactive<OnlineEvalState>({
  runs: [],
  loading: false,
  running: false,
  sampleSize: 20,
  activeRunId: null,
  activeRun: null,
  detail: null,
  detailVisible: false,
  detailLoading: false,
  onlyFailed: false,
})

const statusText = ONLINE_EVAL_STATUS.text

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'
const statusTag = ONLINE_EVAL_STATUS.tag as (status?: string) => TagType

let pollTimer: ReturnType<typeof setInterval> | null = null
let polling = false

/** 历史里最新一次带汇总的运行：首页四张卡读它的 summary_json。 */
const latestRun = computed<OnlineEvalRun | null>(() => {
  return data.runs.find((run) => run.summary_json) || data.activeRun || null
})

const latestSummary = computed<OnlineEvalSummary | null>(
  () => latestRun.value?.summary_json || null,
)

const runningTitle = computed(() => {
  const run = data.activeRun
  if (!run) return '正在提交评测…'
  return `评测运行 #${run.id} 进行中（样本 ${run.sample_size} 条），完成后自动刷新`
})

/** 判定通过阈值：后端在 summary 里带，旧运行没有时用默认 0.6。 */
const passThreshold = computed(() => data.detail?.summary_json?.threshold ?? 0.6)

const failedCases = computed<OnlineEvalCase[]>(() =>
  (data.detail?.cases || []).filter((item) => isFailedCase(item, passThreshold.value)),
)

const visibleCases = computed<OnlineEvalCase[]>(() =>
  data.onlyFailed ? failedCases.value : data.detail?.cases || [],
)

const drawerTitle = computed(() => {
  const run = data.detail
  return run ? `评测运行 #${run.id} 明细` : '评测明细'
})

/** 失败样例：判定调用报错，或忠实度/相关性任一低于阈值。 */
function isFailedCase(item: OnlineEvalCase, threshold: number): boolean {
  if (item.error_message) return true
  const scores = item.scores_json
  if (!scores) return false
  const faithfulness = scores.faithfulness
  const relevance = scores.answer_relevancy
  if (typeof faithfulness === 'number' && faithfulness < threshold) return true
  if (typeof relevance === 'number' && relevance < threshold) return true
  return false
}

/** 评分压成一行键值对：数字保留两位小数，非数字字段（reason 等）不混进来。 */
function scoreText(scores: Record<string, unknown> | null): string {
  if (!scores) return '-'
  const parts = Object.entries(scores)
    .filter(([, value]) => typeof value === 'number')
    .map(([key, value]) => `${key}=${(value as number).toFixed(2)}`)
  return parts.length ? parts.join(' ') : '-'
}

/** 判定证据/理由：后端主字段是 evidence，兼容 reason/rationale 等旧键。 */
function evidenceText(scores: Record<string, unknown> | null): string {
  if (!scores) return '-'
  for (const key of ['evidence', 'reason', 'rationale', 'explanation']) {
    const value = scores[key]
    if (typeof value === 'string' && value.trim()) return value
  }
  return '-'
}

function loadRuns(): void {
  data.loading = true
  listOnlineEvalRuns(1, 5)
    .then((res) => {
      data.runs = res.data?.list || []
      // 切走页面/刷新后回来：历史里还有 RUNNING 的运行就自动续查
      const running = data.runs.find((item) => item.status === 'RUNNING')
      if (running && !data.running) {
        data.activeRunId = running.id
        data.activeRun = running
        data.running = true
        startPolling()
      }
    })
    .catch(() => {
      // 错误提示由 request 拦截器统一弹出
    })
    .finally(() => (data.loading = false))
}

/** 轮询一次运行；离开 RUNNING 后停表并刷新历史。 */
function pollActiveRun(): void {
  const runId = data.activeRunId
  if (runId == null || polling) return
  polling = true
  getOnlineEvalRun(runId)
    .then((res) => {
      data.activeRun = res.data
      if (res.data.status !== 'RUNNING') {
        stopPolling()
        data.running = false
        loadRuns()
      }
    })
    .catch(() => {
      // 单次轮询失败不视为评测失败：下一轮继续查
    })
    .finally(() => (polling = false))
}

function startPolling(): void {
  stopPolling()
  pollTimer = setInterval(pollActiveRun, POLL_INTERVAL_MS)
}

function stopPolling(): void {
  if (pollTimer != null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function run(): void {
  if (data.running) return
  data.running = true
  data.activeRun = null
  runOnlineEval(data.sampleSize)
    .then((res) => {
      // POST 立即返回 run_id，真正的判定在后台任务里跑
      data.activeRunId = res.data.run_id
      loadRuns()
      startPolling()
    })
    .catch(() => {
      // 没有运行记录可轮询：恢复按钮，错误提示已由拦截器弹出
      data.running = false
    })
}

function openDetail(runId: number, onlyFailed: boolean): void {
  data.detail = null
  data.onlyFailed = onlyFailed
  data.detailVisible = true
  data.detailLoading = true
  getOnlineEvalRun(runId)
    .then((res) => {
      data.detail = res.data
    })
    .catch(() => {
      // 拦截器已提示；抽屉保持空态
    })
    .finally(() => (data.detailLoading = false))
}

onMounted(() => {
  loadRuns()
})

onUnmounted(() => {
  stopPolling()
})
</script>

<style scoped>
.online-eval-panel {
  margin-top: 10px;
}

.panel-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 10px;
}

.panel-tip {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.45);
  flex: 1;
  min-width: 240px;
}

.panel-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-alert {
  margin-bottom: 10px;
}

.eval-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.eval-card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fafbfc;
}

.eval-label {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.55);
}

.eval-value {
  font-size: 24px;
  font-weight: 600;
  color: rgba(17, 24, 39, 0.9);
  margin-top: 4px;
}

.eval-value span {
  font-size: 12px;
  font-weight: 400;
  color: rgba(17, 24, 39, 0.45);
  margin-left: 4px;
}

.eval-link {
  border: none;
  background: none;
  padding: 0;
  margin-top: 4px;
  color: #409eff;
  font-size: 12px;
  cursor: pointer;
}

.eval-link:hover {
  text-decoration: underline;
}

.detail-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  font-size: 13px;
  color: rgba(17, 24, 39, 0.7);
  margin: 10px 0;
}

.detail-toolbar {
  margin-bottom: 10px;
}

@media (max-width: 1200px) {
  .eval-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
