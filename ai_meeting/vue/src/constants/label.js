// 跨页面复用的小型格式化函数（此前逐字复制在多个页面里）。

// 会议下拉框显示“编号 / 主题”（Transcription / MeetingMaterial 共用）
export const meetingLabel = (item) => `${item.meeting_no} / ${item.title}`

// Agent 步骤类型中文名（Agent / Observability 共用）
export const stepTypeText = (type) =>
  ({ REVIEW: '审查', REFINE: '重写', FINAL: '汇总' }[type] || type)
