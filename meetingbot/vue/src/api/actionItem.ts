/**
 * 会议待办接口的类型化封装。
 *
 * 待办从已确认纪要生成，也可以手工新建；状态流转写 action_item_event 留痕
 * （HITL：谁在什么时候把哪条从什么状态改成什么状态、附了什么备注）。
 */

import request, { type ApiResult, type PageData } from '@/utils/request'
import type { MemoryEvidence } from '@/api/memory'

/** 待办列表的一行（GET /action-item/selectPage）。 */
export interface ActionItem {
  id: number
  series_id: number | null
  series_name: string | null
  meeting_id: number
  meeting_title: string | null
  minutes_id: number | null
  content: string
  owner_user_id: number | null
  owner_name: string | null
  /** 解析出的截止时间；未解析出时间时为空 */
  deadline: string | null
  /** 纪要里的原文期限（例如「下周五前」） */
  deadline_text: string | null
  /** OPEN / DONE / BLOCKED / CANCELLED */
  status: string
  evidence_json: MemoryEvidence[] | null
  /** 最后在哪个会议被提及（跨会议结转时更新） */
  last_seen_meeting_id: number | null
  update_time: string
}

/** 待办事件（GET /action-item/events/{id}）：状态/负责人/期限/结转各一行。 */
export interface ActionItemEvent {
  id: number
  action_item_id: number
  /** CREATE / STATUS / OWNER / DEADLINE / CARRY_OVER */
  event_type: string
  from_value: string | null
  to_value: string | null
  note: string | null
  actor_id: number
  actor_name: string | null
  create_time: string
}

export interface ActionItemQuery {
  series_id?: number
  owner_user_id?: number
  status?: string
  /** 只看逾期未完成 */
  overdue?: boolean
  pageNum?: number
  pageSize?: number
}

/** 新建载荷：来源会议必填，截止时间只收纪要里的原文（deadline_text），由后端解析。 */
export interface ActionItemAddPayload {
  meeting_id: number
  content: string
  series_id?: number | null
  owner_user_id?: number | null
  owner_name?: string | null
  deadline_text?: string | null
}

/** 编辑载荷：后端只允许改内容/负责人/截止时间，来源会议与系列不可改。 */
export interface ActionItemUpdatePayload {
  id: number
  content?: string
  owner_user_id?: number | null
  deadline?: string | null
  deadline_text?: string | null
}

/** 状态更新载荷：note 会写进 action_item_event.note。 */
export interface ActionItemStatusPayload {
  id: number
  status: string
  note?: string
}

export function selectActionItemPage(
  query: ActionItemQuery = {},
): Promise<ApiResult<PageData<ActionItem>>> {
  return request.get<PageData<ActionItem>>('/action-item/selectPage', {
    params: {
      series_id: query.series_id ?? undefined,
      owner_user_id: query.owner_user_id ?? undefined,
      status: query.status || undefined,
      // 后端按真值判断；false 时不传，交给 undefined 让 Axios 省略参数
      overdue: query.overdue ? true : undefined,
      page_num: query.pageNum ?? 1,
      page_size: query.pageSize ?? 10,
    },
  })
}

export function addActionItem(payload: ActionItemAddPayload): Promise<ApiResult<ActionItem>> {
  return request.post<ActionItem>('/action-item/add', payload)
}

export function updateActionItem(payload: ActionItemUpdatePayload): Promise<ApiResult<ActionItem>> {
  return request.put<ActionItem>('/action-item/update', payload)
}

export function updateActionItemStatus(
  payload: ActionItemStatusPayload,
): Promise<ApiResult<ActionItem>> {
  return request.put<ActionItem>('/action-item/updateStatus', payload)
}

/** 查看某条待办的变更历史（详情抽屉里渲染成时间线）。 */
export function listActionItemEvents(id: number): Promise<ApiResult<ActionItemEvent[]>> {
  return request.get<ActionItemEvent[]>('/action-item/events/' + id)
}
