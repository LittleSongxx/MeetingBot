/**
 * 页面共用的数值/时间格式化。
 *
 * 成本在库里是整数微元（1e-6 元），换算只在展示层做；此前 AiModel 与 Observability
 * 各有一份一模一样的换算，新增成本面板后会变成三份——抽到这里唯一真源。
 * 「没有数据」与「数值是 0」必须分开：传 null/undefined 时比例类返回 "-"。
 */

/** 微元（1e-6 元）→ 元，去掉多余的 0；0 原样返回 "0"（未定价由调用方单独标注）。 */
export function formatMicro(value?: number | null): string {
  const micro = Number(value || 0)
  if (!micro) return '0'
  return (micro / 1_000_000).toFixed(6).replace(/0+$/, '').replace(/\.$/, '')
}

/** 整数加千分位。 */
export function formatNumber(value?: number | null): string {
  return Number(value || 0).toLocaleString('zh-CN')
}

/** 0~1 的比例 → 百分数；null/undefined 表示没有数据，显示 "-" 而不是 0%。 */
export function formatPercent(value?: number | null, digits = 1): string {
  if (value == null) return '-'
  return `${(Number(value) * 100).toFixed(digits)}%`
}

/** 日期时间截断到分钟；兼容后端的「YYYY-MM-DD HH:mm:ss」与 ISO 的 T 分隔。 */
export function formatMinute(value?: string | null): string {
  if (!value) return '-'
  return value.replace('T', ' ').slice(0, 16)
}

/** 秒数 → mm:ss；负数/非数字返回空串（调用方据此决定是否显示这一小段）。 */
export function formatSeconds(seconds?: number | null): string {
  if (typeof seconds !== 'number' || Number.isNaN(seconds) || seconds < 0) return ''
  const minutes = Math.floor(seconds / 60)
  const rest = Math.floor(seconds % 60)
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
}

/**
 * 截止时间是否已过期（本地时区，按 "YYYY-MM-DD HH:mm:ss" 解析）。
 * 已完成/已取消的待办不算逾期；没有截止时间也不算。
 */
export function isOverdue(deadline?: string | null, status?: string): boolean {
  if (!deadline) return false
  if (status === 'DONE' || status === 'CANCELLED') return false
  const time = new Date(deadline.replace(' ', 'T')).getTime()
  if (Number.isNaN(time)) return false
  return time < Date.now()
}
