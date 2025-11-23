/**
 * 长期记忆接口的类型化封装。
 *
 * 记忆分两级作用域：SERIES（系列级决策/结论）与 USER（个人偏好）。
 * PROPOSED 只是候选，必须人工确认后才进检索；确认时可以指定 supersedes_id
 * 把被替代的旧记忆标成 SUPERSEDED——这是"同一件事有新说法"的唯一入口。
 */

import request, { type ApiResult, type PageData } from '@/utils/request'

/** 证据片段：来自纪要条目的原话与定位信息。字段按后端 evidence_json 的写法，全部可选。 */
export interface MemoryEvidence {
  quote?: string
  source_text?: string
  speaker?: string | null
  start_ms?: number | null
  end_ms?: number | null
  start_second?: number | null
  meeting_id?: number | null
  minutes_id?: number | null
  field?: string | null
  item_index?: number | null
}

/** 记忆列表的一行（GET /memory/selectPage）。 */
export interface MeetingMemoryItem {
  id: number
  /** SERIES / USER */
  scope_type: string
  /** scope_type=SERIES 时是 series_id，USER 时是 user_id */
  scope_id: number
  /** DECISION / CONCLUSION / PREFERENCE */
  kind: string
  content: string
  /** PROPOSED / CONFIRMED / REJECTED / EXPIRED / SUPERSEDED */
  status: string
  /** MINUTES / MANUAL */
  source_type: string
  source_id: number | null
  source_meeting_id: number | null
  source_meeting_title: string | null
  evidence_json: MemoryEvidence[] | null
  /** 本条确认后替代的旧记忆 id */
  supersedes_id: number | null
  valid_from: string | null
  valid_to: string | null
  confirmed_time: string | null
  create_time: string
}

/** 列表查询条件；分页沿用后端的 page_num/page_size。 */
export interface MemoryQuery {
  status?: string
  scope_type?: string
  scope_id?: number
  kind?: string
  pageNum?: number
  pageSize?: number
}

export function selectMemoryPage(
  query: MemoryQuery = {},
): Promise<ApiResult<PageData<MeetingMemoryItem>>> {
  return request.get<PageData<MeetingMemoryItem>>('/memory/selectPage', {
    params: {
      status: query.status || undefined,
      scope_type: query.scope_type || undefined,
      scope_id: query.scope_id ?? undefined,
      kind: query.kind || undefined,
      page_num: query.pageNum ?? 1,
      page_size: query.pageSize ?? 10,
    },
  })
}

/** 确认结果：存在相似活跃记忆时 confirmed=false，并带回冲突列表。 */
export interface MemoryConfirmResult {
  id: number
  status: string
  confirmed: boolean
  /** 本条确认后替代的旧记忆 id */
  superseded_id: number | null
  /** 冲突的旧记忆（confirmed=false 时非空），由用户在确认弹窗里选择替代目标 */
  conflicts: MeetingMemoryItem[]
}

/**
 * 确认候选。不传 supersede_id 时只做冲突检测：
 * 没有冲突就直接确认；有冲突时返回 {confirmed:false, conflicts}，
 * 用户选择被替代的旧记忆后带 supersede_id 再调一次。
 */
export function confirmMemory(
  id: number,
  supersedeId?: number,
): Promise<ApiResult<MemoryConfirmResult>> {
  return request.post<MemoryConfirmResult>('/memory/confirm', { id, supersede_id: supersedeId })
}

export function rejectMemory(id: number): Promise<ApiResult<null>> {
  return request.post<null>('/memory/reject', { id })
}

/** 把已确认记忆置为过期（不再参与检索，但保留审计痕迹）。 */
export function expireMemory(id: number): Promise<ApiResult<null>> {
  return request.post<null>('/memory/expire', { id })
}

/** 编辑记忆正文；只允许改内容，作用域与类型保持不变。 */
export function updateMemory(id: number, content: string): Promise<ApiResult<null>> {
  return request.put<null>('/memory/update', { id, content })
}

/** 新增个人偏好：写入当前用户的 USER 级记忆。 */
export function addPreference(content: string): Promise<ApiResult<MeetingMemoryItem>> {
  return request.post<MeetingMemoryItem>('/memory/preference', { content })
}
