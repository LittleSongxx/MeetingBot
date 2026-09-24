// 全部业务状态机的中文名 / 标签颜色 / 下拉选项，唯一真源。
// 来历：此前五个页面各自复制一份字典，Home 与 Meeting 的同键文案颜色已经漂移
//（SCHEDULED 一边'未开始'/info、一边'待开始'/primary）。本模块按状态机分组导出，
// 各页解构导入同名符号（statusOptions/statusText/statusTag），模板调用点零改动。
// 注意：不同状态机的 SUCCEEDED/FAILED 语义不同（转写/纪要/自检），禁止按键名合并。

const make = (entries) => ({
  options: Object.entries(entries).map(([value, [label, tag, short]]) => ({
    label, value
  })),
  text: (status) => entries[status]?.[0] ?? status,
  tag: (status) => entries[status]?.[1] ?? '',
  // 图表/图例用的短文案（观测页饼图空间有限），缺省回落到标准文案
  shortText: (status) => entries[status]?.[2] ?? entries[status]?.[0] ?? status
})

// 状态机 A：转写任务（Transcription / Home）
export const TRANSCRIPTION_STATUS = make({
  PENDING: ['等待执行', 'info'],
  PROCESSING: ['转写中', 'warning'],
  SUCCEEDED: ['已完成', 'success'],
  FAILED: ['转写失败', 'danger', '失败']
})

// 状态机 B：会议纪要（MeetingMinutes / Home）
export const MINUTES_STATUS = make({
  GENERATING: ['生成中', 'warning'],
  DRAFT: ['待确认', 'info'],
  CONFIRMED: ['已确认', 'success'],
  FAILED: ['生成失败', 'danger', '失败']
})

// 状态机 C：Agent 自检运行（Agent / Observability）
export const AGENT_RUN_STATUS = make({
  RUNNING: ['自检中', 'warning', '运行中'],
  WAITING_CONFIRMATION: ['等待人工确认', 'warning', '等待确认'],
  SUCCEEDED: ['已完成', 'success'],
  REJECTED: ['已放弃修订', 'info'],
  FAILED: ['自检失败', 'danger', '失败'],
  INTERRUPTED: ['已中断', 'info']
})

// 状态机 D：会议（Meeting / Home）。冲突以管理页（Meeting.vue）版本为准。
export const MEETING_STATUS = make({
  SCHEDULED: ['待开始', 'primary'],
  IN_PROGRESS: ['进行中', 'warning'],
  FINISHED: ['已结束', 'success'],
  CANCELLED: ['已取消', 'info']
})

// 状态机 E：参会邀请确认（Meeting 详情）
export const INVITATION_STATUS_TEXT = (status) =>
  ({ PENDING: '待确认', ACCEPTED: '已接受', DECLINED: '已拒绝' }[status] || status)

// 参会角色（Meeting 详情）
export const PARTICIPANT_ROLE_TEXT = (role) =>
  ({ CREATOR: '创建人', HOST: '主持人', PARTICIPANT: '参会人' }[role] || role)
