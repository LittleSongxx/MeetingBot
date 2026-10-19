<template>
  <div>
    <div class="card filter-card">
      <!-- 日期范围写入 data.dateRange，两端分别作为 date_from 和 date_to 发给后端 -->
      <el-date-picker
        v-model="data.dateRange"
        type="daterange"
        value-format="YYYY-MM-DD"
        range-separator="至"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
        :clearable="false"
      />
      <!-- 模型下拉框选项来自 data.modelOptions，选中的配置主键写入 data.modelConfigId -->
      <el-select
        v-model="data.modelConfigId"
        clearable
        filterable
        style="width: 260px"
        placeholder="全部模型"
      >
        <el-option
          v-for="item in data.modelOptions"
          :key="item.model_config_id"
          :label="`${item.model_name} / ${item.model_config_id}`"
          :value="item.model_config_id"
        />
      </el-select>
      <!-- 查询期间按钮显示加载状态；重置恢复最近三十天并清空模型后重新查询 -->
      <el-button type="primary" :loading="data.loading" @click="load">查询</el-button>
      <el-button type="warning" plain @click="reset">重置</el-button>
      <!-- 后端按角色返回“企业全局观测”或“个人AI与Agent观测” -->
      <el-tag type="info">{{ data.result.scope || '观测范围' }}</el-tag>
    </div>

    <!-- 八张卡片由 summaryCards 计算属性生成，数据来自 data.result.summary -->
    <div class="summary-grid">
      <div v-for="item in summaryCards" :key="item.key" class="card summary-card">
        <div class="summary-label">{{ item.label }}</div>
        <div class="summary-value">
          {{ item.value }}<span>{{ item.unit }}</span>
        </div>
        <div v-if="item.note" class="summary-note">{{ item.note }}</div>
      </div>
    </div>

    <!-- 在线评测：抽样最近的问答由裁判模型评分；仅管理员可触发 -->
    <OnlineEvalPanel v-if="isAdmin" />

    <!-- 四个带 ref 的 div 是 ECharts 容器，由 renderCharts 绘制 -->
    <div class="chart-grid">
      <!-- 趋势图容器占两列宽 -->
      <div class="card chart-card wide">
        <div class="chart-title">AI调用与Token趋势</div>
        <div ref="trendRef" class="chart"></div>
      </div>
      <!-- 调用类型分布和运行状态两张环形图各占一列 -->
      <div class="card chart-card">
        <div class="chart-title">调用类型分布</div>
        <div ref="callTypeRef" class="chart"></div>
      </div>
      <div class="card chart-card">
        <div class="chart-title">纪要自检运行状态</div>
        <div ref="agentStatusRef" class="chart"></div>
      </div>
      <!-- 步骤执行结果图容器用 tall 样式，高度比其他图多一些 -->
      <div class="card chart-card wide">
        <div class="chart-title">自检步骤执行结果</div>
        <div ref="stepRef" class="chart tall"></div>
      </div>
      <!-- 模型调用排行不是图表，直接用表格展示 -->
      <div class="card chart-card wide">
        <div class="chart-title">模型调用排行</div>
        <!-- 模型排行直接绑定 model_ranking 数组 -->
        <el-table stripe :data="data.result.model_ranking || []" height="350">
          <el-table-column prop="model_name" label="模型" min-width="170" show-overflow-tooltip />
          <el-table-column prop="call_count" label="调用量" width="85" />
          <el-table-column prop="success_rate" label="成功率" width="90">
            <template #default="scope">{{ scope.row.success_rate }}%</template>
          </el-table-column>
          <!-- 成功率加百分号，Token 加千分位 -->
          <el-table-column prop="average_elapsed_ms" label="平均耗时ms" width="110" />
          <el-table-column prop="total_tokens" label="Token" width="110">
            <template #default="scope">{{ formatNumber(scope.row.total_tokens) }}</template>
          </el-table-column>
          <!-- 成本按微元换算成元显示，0 表示这套配置未定价（不是免费） -->
          <el-table-column prop="cost_micro" label="成本元" width="110">
            <template #default="scope">{{ formatMicro(scope.row.cost_micro) }}</template>
          </el-table-column>
        </el-table>
      </div>
    </div>

    <!-- 成本与缓存：接口仅管理员可读，跟随查询按钮提交的日期范围 -->
    <CostPanel v-if="isAdmin" :date-from="data.appliedRange[0]" :date-to="data.appliedRange[1]" />

    <div class="card failure-card">
      <div class="chart-title">最近失败记录</div>
      <!-- 最近失败记录绑定 recent_failures，source 区分 AI 调用失败和自检运行失败 -->
      <el-table stripe :data="data.result.recent_failures || []">
        <el-table-column prop="source" label="来源" width="110">
          <template #default="scope">{{
            scope.row.source === 'AI_CALL' ? 'AI调用' : 'Agent运行'
          }}</template>
        </el-table-column>
        <!-- AI 调用显示调用类型，自检运行显示运行编号 -->
        <el-table-column prop="type" label="类型/编号" min-width="180" />
        <el-table-column prop="model_name" label="模型" min-width="150" />
        <el-table-column
          prop="error_message"
          label="失败原因"
          min-width="300"
          show-overflow-tooltip
        />
        <el-table-column prop="create_time" label="发生时间" width="175" />
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { ElMessage } from 'element-plus'
import request from '@/utils/request'
import CostPanel from '@/components/CostPanel.vue'
import OnlineEvalPanel from '@/components/OnlineEvalPanel.vue'
import { AGENT_RUN_STATUS } from '@/constants/status'
import { stepTypeText as STEP_TYPE_TEXT_FN } from '@/constants/label'

// 按需注册用到的三种图表、三个组件和 Canvas 渲染器
echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
])

// 登录缓存里的角色：在线评测会真实调用裁判模型，只给管理员开放触发入口
const user = JSON.parse(localStorage.getItem('xm-user') || '{}')
const isAdmin = user.role === 'ADMIN'

// 四个图表容器的模板引用
const trendRef = ref()
const callTypeRef = ref()
const agentStatusRef = ref()
const stepRef = ref()
// 已创建的图表实例，窗口缩放时逐个 resize，离开页面时逐个 dispose
// 用 ReturnType<typeof echarts.init> 表达实例类型：echarts/core 的 init 返回 EChartsType，
// 这样 charts 不再是 never[]，push/includes/resize/dispose 都能按实例类型检查
const charts: ReturnType<typeof echarts.init>[] = []

// 默认日期范围：今天和往前 29 天，格式化成 YYYY-MM-DD，页面打开和点重置时使用
const defaultRange = () => {
  const end = new Date()
  const start = new Date()
  start.setDate(end.getDate() - 29)
  const format = (value) =>
    `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
  return [format(start), format(end)]
}

// ---- /observability/overview 的返回结构（字段名与后端 observability_service.build_observability 的返回字典一致）----

// 八张卡片读取的汇总指标。两者都声明为可选：初次查询前 result 是空对象，
// 且页面本来就用 `|| 0` 兜底，可选让"还没有数据"这件事在类型上看得见。
interface ObservabilitySummary {
  call_count?: number
  failed_call_count?: number
  call_success_rate?: number
  total_tokens?: number
  input_tokens?: number
  output_tokens?: number
  average_elapsed_ms?: number
  agent_run_count?: number
  average_agent_elapsed_ms?: number
  agent_success_rate?: number
  step_count?: number
  step_failure_rate?: number
  revision_count?: number
  rejection_count?: number
  average_rounds?: number
  /** 已定价调用的成本合计（微元，1e-6 元）与它的可读形式 */
  cost_micro?: number
  cost_text?: string
  /** 未定价的调用次数：成本为 0 与"不知道多少钱"是两件事 */
  unpriced_call_count?: number
}

// 趋势图的横轴日期和三条系列（调用量、失败量、Token）
interface DailyTrendItem {
  date: string
  call_count: number
  failed_count: number
  total_tokens: number
  cost_micro: number
  unpriced_count: number
}

// 调用类型分布（环形图）的扇区
interface CallTypeItem {
  call_type: string
  call_count: number
}

// 模型调用排行表格的一行；同一次响应里 model_ranking 也是模型下拉框的选项来源
interface ModelRankingItem {
  model_config_id: number
  model_name: string
  call_count: number
  success_rate: number
  average_elapsed_ms: number
  total_tokens: number
  cost_micro: number
}

// 自检运行状态分布（环形图）的扇区，后端固定返回六种状态
interface AgentStatusItem {
  status: string
  count: number
}

// 自检步骤执行结果（横向堆叠柱形）的两个系列
interface StepRankingItem {
  step_type: string
  succeeded_count: number
  failed_count: number
}

// 最近失败记录表格的一行，source 区分 AI 调用失败和自检运行失败
interface RecentFailureItem {
  source: string
  type: string
  // 自检运行失败时后端给 null
  model_name: string | null
  error_message: string | null
  create_time: string
}

// 接口返回的整块统计结果，即 data.result 的类型
interface ObservabilityResult {
  // 顶部观测范围标签；初始空结果里还没有这个字段，故可选
  scope?: string
  summary: ObservabilitySummary
  daily_trend: DailyTrendItem[]
  call_types: CallTypeItem[]
  model_ranking: ModelRankingItem[]
  agent_status: AgentStatusItem[]
  step_ranking: StepRankingItem[]
  recent_failures: RecentFailureItem[]
}

// 页面状态：筛选条件 + 接口返回的统计结果
interface ObservabilityPageState {
  // 日期范围控件绑定的两个日期字符串
  dateRange: string[]
  // 已提交查询的日期范围：成本面板只跟随它，避免拨动日期就发请求
  appliedRange: string[]
  // 模型下拉框选中的配置主键，null 表示全部模型
  modelConfigId: number | null
  // 模型下拉框的选项，内容就是有调用记录的模型排行（只用到主键和名称两列）
  modelOptions: ModelRankingItem[]
  // 查询按钮的加载状态
  loading: boolean
  // 接口返回的全部统计数据，模板和图表都从这里读
  result: ObservabilityResult
}

const data = reactive<ObservabilityPageState>({
  // 日期范围控件绑定的两个日期字符串
  dateRange: defaultRange(),
  // 已提交查询的日期范围（成本面板的入参）
  appliedRange: defaultRange(),
  // 模型下拉框选中的配置主键，null 表示全部模型
  modelConfigId: null,
  // 模型下拉框的选项
  modelOptions: [],
  // 查询按钮的加载状态
  loading: false,
  // 接口返回的全部统计数据，模板和图表都从这里读
  result: {
    summary: {},
    daily_trend: [],
    call_types: [],
    model_ranking: [],
    agent_status: [],
    step_ranking: [],
    recent_failures: [],
  },
})

// Token 数加千分位
const formatNumber = (value) => Number(value || 0).toLocaleString('zh-CN')
// 成本存的是微元整数（避免浮点累加误差），展示时才换算成元
const formatMicro = (value) => {
  const micro = Number(value || 0)
  if (!micro) return '0'
  const text = (micro / 1_000_000).toFixed(6)
  return text.replace(/0+$/, '').replace(/\.$/, '')
}
// 把 summary 里的字段组装成八张卡片的标题、数值、单位和说明
const summaryCards = computed(() => {
  const summary = data.result.summary || {}
  return [
    // 调用次数，说明文字显示其中失败的次数
    {
      key: 'calls',
      label: 'AI调用量',
      value: summary.call_count || 0,
      unit: '次',
      note: `失败 ${summary.failed_call_count || 0} 次`,
    },
    // 成功调用占全部调用的百分比
    { key: 'success', label: 'AI调用成功率', value: summary.call_success_rate || 0, unit: '%' },
    // Token 总量，说明文字拆成输入和输出
    {
      key: 'tokens',
      label: 'Token总量',
      value: formatNumber(summary.total_tokens),
      unit: '',
      note: `输入 ${formatNumber(summary.input_tokens)} / 输出 ${formatNumber(summary.output_tokens)}`,
    },
    // 成本合计（微元换算成元）。未定价调用单独说明：那些调用的花销不在这个数里
    {
      key: 'cost',
      label: 'AI调用成本',
      value: summary.cost_text || '0',
      unit: '元',
      note: summary.unpriced_call_count
        ? `另有 ${summary.unpriced_call_count} 次未定价调用（未计入）`
        : '全部调用均已定价',
    },
    // 全部调用的平均耗时
    { key: 'elapsed', label: 'AI平均耗时', value: summary.average_elapsed_ms || 0, unit: 'ms' },
    // 自检运行次数，说明文字是已结束运行的平均耗时
    {
      key: 'runs',
      label: 'Agent运行',
      value: summary.agent_run_count || 0,
      unit: '次',
      note: `平均耗时 ${summary.average_agent_elapsed_ms || 0}ms`,
    },
    // 状态为 SUCCEEDED 的运行占全部运行的百分比
    {
      key: 'agentSuccess',
      label: 'Agent成功率',
      value: summary.agent_success_rate || 0,
      unit: '%',
    },
    // 自检步骤条数，说明文字是失败步骤的占比
    {
      key: 'steps',
      label: '自检步骤',
      value: summary.step_count || 0,
      unit: '步',
      note: `失败率 ${summary.step_failure_rate || 0}%`,
    },
    // 产生修订稿的运行数，说明文字是人工放弃次数和平均轮次
    {
      key: 'revisions',
      label: '产生修订稿',
      value: summary.revision_count || 0,
      unit: '次',
      note: `人工放弃 ${summary.rejection_count || 0} 次，平均 ${summary.average_rounds || 0} 轮`,
    },
  ]
})

// 同一个容器已经有实例就复用，没有才初始化，新实例记进 charts
const chart = (element) => {
  const instance = echarts.getInstanceByDom(element) || echarts.init(element)
  if (!charts.includes(instance)) charts.push(instance)
  return instance
}

const renderCharts = () => {
  // 趋势图：调用量和失败量画柱形用左轴，Token 画折线用右轴
  const trend = data.result.daily_trend || []
  chart(trendRef.value).setOption(
    {
      // 鼠标悬停时显示同一天的三组数据
      tooltip: { trigger: 'axis' },
      legend: { data: ['调用量', '失败量', 'Token'] },
      grid: { left: 50, right: 65, bottom: 35, top: 45 },
      // 横轴是日期，日期太密时隐藏重叠的标签
      xAxis: {
        type: 'category',
        data: trend.map((item) => item.date),
        axisLabel: { hideOverlap: true },
      },
      // 两条纵轴：左边是次数，右边是 Token
      yAxis: [
        { type: 'value', name: '次' },
        { type: 'value', name: 'Token' },
        // 成本单独一根轴：它的量级与 token 差着数量级，共用一根轴会被压成一条直线
        { type: 'value', name: '成本(元)', offset: 60 },
      ],
      series: [
        // 调用量和失败量画柱形，默认使用左轴
        {
          name: '调用量',
          type: 'bar',
          data: trend.map((item) => item.call_count),
          itemStyle: { color: '#2563eb' },
        },
        {
          name: '失败量',
          type: 'bar',
          data: trend.map((item) => item.failed_count),
          itemStyle: { color: '#ef4444' },
        },
        // Token 画平滑折线，yAxisIndex: 1 使用右轴
        {
          name: 'Token',
          type: 'line',
          yAxisIndex: 1,
          smooth: true,
          data: trend.map((item) => item.total_tokens),
          itemStyle: { color: '#f59e0b' },
        },
        // 成本趋势用第三根轴（右侧再偏移 60px），单位换算成元便于阅读
        {
          name: '成本(元)',
          type: 'line',
          yAxisIndex: 2,
          smooth: true,
          data: trend.map((item) => Number((item.cost_micro || 0) / 1_000_000)),
          itemStyle: { color: '#10b981' },
        },
      ],
    },
    true,
  )

  // 调用类型分布：环形图，调用类型编码翻成中文
  const typeText = {
    TRANSCRIPTION: '音视频转写',
    // 分段/合并是单遍改造前的调用类型：新数据不再产生，历史记录仍要显示得出来
    MINUTES_CHUNK: '纪要分段（旧）',
    MINUTES_MERGE: '纪要合并（旧）',
    MINUTES: '纪要生成',
    AGENT_REVIEW: '自检审查',
    AGENT_REFINE: '自检重写',
    AGENT_REVIEW_SUPPORT: '自检复核',
    SPEAKER_MATCH: '说话人匹配',
    CHAT: '知识问答',
    CHAT_SUMMARY: '会话摘要',
    KB_EMBEDDING: '知识库嵌入',
    KB_QUERY_EMBED: '检索查询嵌入',
    KB_RERANK: '检索重排',
    KB_QUERY_PLAN: '查询规划',
  }
  const callTypes = data.result.call_types || []
  chart(callTypeRef.value).setOption(
    {
      tooltip: { trigger: 'item' },
      // 图例放在底部，调用类型多时可以滚动
      legend: { bottom: 0, type: 'scroll' },
      series: [
        {
          type: 'pie',
          radius: ['36%', '68%'],
          center: ['50%', '43%'],
          // 扇区名称是中文调用类型，数值是调用次数
          data: callTypes.map((item) => ({
            name: typeText[item.call_type] || item.call_type,
            value: item.call_count,
          })),
          label: { formatter: '{b}: {c}' },
        },
      ],
    },
    true,
  )

  // 自检运行状态：环形图，六种状态翻成中文（短文案，共享 @/constants）
  const agentText = AGENT_RUN_STATUS.shortText
  const statuses = data.result.agent_status || []
  chart(agentStatusRef.value).setOption(
    {
      tooltip: { trigger: 'item' },
      legend: { bottom: 0 },
      // 扇区颜色按 agent_status 的返回顺序依次取用
      color: ['#2563eb', '#f59e0b', '#10b981', '#ef4444', 'rgba(17, 24, 39, .55)'],
      series: [
        {
          type: 'pie',
          radius: ['36%', '68%'],
          center: ['50%', '43%'],
          // 后端固定返回六种状态，数量为 0 的状态也在图例里
          data: statuses.map((item) => ({ name: agentText(item.status), value: item.count })),
          label: { formatter: '{b}: {c}' },
        },
      ],
    },
    true,
  )

  // 自检步骤执行结果：横向堆叠柱形，成功和失败叠在一起
  const stepTypeText = Object.fromEntries(
    ['REVIEW', 'REFINE', 'FINAL'].map((k) => [k, STEP_TYPE_TEXT_FN(k)]),
  )
  const steps = data.result.step_ranking || []
  chart(stepRef.value).setOption(
    {
      // 悬停时整行加阴影，同时显示成功和失败条数
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { data: ['成功', '失败'] },
      grid: { left: 100, right: 30, bottom: 35, top: 45 },
      // 横轴是步骤条数，刻度只显示整数
      xAxis: { type: 'value', minInterval: 1 },
      // 纵轴是中文步骤类型
      yAxis: {
        type: 'category',
        data: steps.map((item) => stepTypeText[item.step_type] || item.step_type),
      },
      series: [
        // 成功和失败两组柱形的 stack 相同，叠成一根
        {
          name: '成功',
          type: 'bar',
          stack: 'step',
          data: steps.map((item) => item.succeeded_count),
          itemStyle: { color: '#10b981' },
        },
        {
          name: '失败',
          type: 'bar',
          stack: 'step',
          data: steps.map((item) => item.failed_count),
          itemStyle: { color: '#ef4444' },
        },
      ],
    },
    true,
  )
}

const load = () => {
  if (!data.dateRange?.length) return ElMessage.warning('请选择观测日期范围')
  // 成本面板只跟随已提交的范围：拨动日期控件时不发请求，点查询才更新
  data.appliedRange = [...data.dateRange]
  data.loading = true
  request
    .get<ObservabilityResult>('/observability/overview', {
      params: {
        date_from: data.dateRange[0],
        date_to: data.dateRange[1],
        // 没选模型时传 undefined，Axios 不拼这个参数，后端统计全部模型
        model_config_id: data.modelConfigId || undefined,
      },
    })
    .then((res) => {
      if (res.code === '200') {
        // 整体替换统计结果，卡片和两张表格随之重新渲染
        data.result = res.data
        // 不限定模型时，模型排行里就是有调用记录的全部模型，用它填下拉选项
        if (!data.modelConfigId) data.modelOptions = res.data.model_ranking || []
        // 等 DOM 更新完再绘制四张图
        nextTick(renderCharts)
      } else {
        ElMessage.error(res.msg)
      }
    })
    // 请求成功或失败都关闭加载状态
    .finally(() => (data.loading = false))
}

const reset = () => {
  // 日期恢复最近三十天，模型恢复全部，再查一次
  data.dateRange = defaultRange()
  data.modelConfigId = null
  load()
}

const resize = () => charts.forEach((item) => item.resize())
onMounted(() => {
  // 容器挂载后才能初始化图表，所以在 onMounted 里查询
  window.addEventListener('resize', resize)
  load()
})
onUnmounted(() => {
  // 离开页面时移除监听并释放图表实例
  window.removeEventListener('resize', resize)
  charts.forEach((item) => item.dispose())
})
</script>

<style scoped>
.filter-card {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 10px;
}
.summary-card {
  padding: 17px;
}
.summary-label {
  color: rgba(17, 24, 39, 0.55);
}
.summary-value {
  font-size: 26px;
  font-weight: bold;
  margin-top: 7px;
}
.summary-value span {
  font-size: 13px;
  font-weight: normal;
  color: rgba(17, 24, 39, 0.55);
  margin-left: 4px;
}
.summary-note {
  color: rgba(17, 24, 39, 0.55);
  font-size: 12px;
  margin-top: 5px;
}
.chart-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}
.chart-card {
  min-width: 0;
}
.chart-card.wide {
  grid-column: span 2;
}
.chart-title {
  font-weight: bold;
  font-size: 16px;
  padding-bottom: 10px;
  border-bottom: 1px solid rgba(17, 24, 39, 0.1);
}
.chart {
  height: 315px;
}
.chart.tall {
  height: 350px;
}
.failure-card {
  margin-top: 10px;
}
@media (max-width: 1200px) {
  .chart-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
