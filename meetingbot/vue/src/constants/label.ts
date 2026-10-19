// 跨页面复用的小型格式化函数（此前逐字复制在多个页面里）。

// 会议下拉框显示“编号 / 主题”（Transcription / MeetingMaterial 共用）
export const meetingLabel = (item) => `${item.meeting_no} / ${item.title}`

// Agent 步骤类型中文名（Agent / Observability 共用）
export const stepTypeText = (type) =>
  ({ REVIEW: '审查', REFINE: '重写', FINAL: '汇总' })[type] || type

// ---- AI模型配置页：用途下拉与中文名（原先在 AiModel.vue 里抄了三处，加 CHEAP 后统一到常量）----
export const MODEL_TYPE_OPTIONS: { label: string; value: string }[] = [
  { label: '音视频转写', value: 'TRANSCRIPTION' },
  { label: '会议纪要', value: 'MINUTES' },
  { label: '纪要自检', value: 'AGENT' },
  { label: '说话人匹配', value: 'SPEAKER' },
  { label: '向量嵌入', value: 'EMBEDDING' },
  { label: '跨族裁判', value: 'JUDGE' },
  // 低成本模型：模型路由（model_route）的回退目标，例如会话摘要 / 检索改写走它
  { label: '低成本模型', value: 'CHEAP' },
]

export const modelTypeText = (type?: string) =>
  MODEL_TYPE_OPTIONS.find((item) => item.value === type)?.label || type || ''

// ---- 模型路由：调用类型 → 中文名（AiModel 路由表 / Observability 共用）----
export const MODEL_ROUTE_OPTIONS: { label: string; value: string }[] = [
  { label: '纪要生成', value: 'MINUTES' },
  { label: '纪要自检', value: 'AGENT' },
  { label: '说话人匹配', value: 'SPEAKER' },
  { label: '知识问答', value: 'CHAT' },
  { label: '会话摘要', value: 'CHAT_SUMMARY' },
  { label: '检索查询规划', value: 'KB_QUERY_PLAN' },
  { label: '检索上下文改写', value: 'KB_CONTEXTUALIZE' },
  // 在线评测的裁判模型：可用更强/更便宜的模型单独指定
  { label: '在线评测裁判', value: 'ONLINE_EVAL_JUDGE' },
]

export const modelRouteText = (key?: string) =>
  MODEL_ROUTE_OPTIONS.find((item) => item.value === key)?.label || key || ''

// ---- Chat 工具轨迹：工具名 → 中文名（未知工具直接显示原名，后端加工具时前端不用改）----
const CHAT_TOOL_TEXT: Record<string, string> = {
  search_knowledge: '知识检索',
  list_facts: '事实清单',
  get_minutes: '读取纪要',
  get_transcript_excerpt: '转写片段',
  query_meeting_state: '会议状态查询',
}

export const chatToolText = (name?: string) => (name && CHAT_TOOL_TEXT[name]) || name || ''
