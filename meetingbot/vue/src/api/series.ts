/**
 * 会议系列接口的类型化封装。
 *
 * 页面只调这里：URL、查询参数名（下划线）和返回字段形状都收在本文件里，
 * 后端契约调整时编译器会把受影响的调用点全部指出来。写操作只有管理员用得上，
 * 但接口本身不区分角色——权限由后端兜底，页面只决定按钮是否出现。
 */

import request, { type ApiResult, type PageData } from '@/utils/request'

/** 系列列表的一行（GET /meeting/series/selectPage）。 */
export interface MeetingSeriesItem {
  id: number
  name: string
  description: string | null
  /** 周期：WEEKLY / BIWEEKLY / MONTHLY / IRREGULAR */
  cadence: string
  owner_id: number
  owner_name: string | null
  /** ACTIVE / ARCHIVED */
  status: string
  meeting_count: number
  open_action_count: number
  memory_count: number
  create_time: string
}

/** 系列详情里的一条会议（时间线用）。 */
export interface SeriesMeetingItem {
  id: number
  title: string
  start_time: string
  status: string
}

/** 系列详情（GET /meeting/series/selectById/{id}）。 */
export interface MeetingSeriesDetail extends MeetingSeriesItem {
  meetings: SeriesMeetingItem[]
  pending_memory_count: number
}

/** 下拉框选项（GET /meeting/series/options）。 */
export interface SeriesOption {
  id: number
  name: string
  status?: string
}

/** 新建/编辑载荷：编辑时带 id；owner_id 留空时后端取当前用户。 */
export interface SeriesPayload {
  id?: number
  name: string
  description?: string | null
  cadence: string
  owner_id?: number
  status?: string
}

/** 「建议系列」候选：score 是 0~1 的相似度，reasons 是给人看的理由。 */
export interface SeriesSuggestionCandidate {
  series_id: number
  name: string
  score: number
  reasons: string[]
}

export interface SeriesSuggestionResult {
  candidates: SeriesSuggestionCandidate[]
}

/** 列表查询条件；分页参数沿用后端的 page_num/page_size。 */
export interface SeriesQuery {
  name?: string
  status?: string
  cadence?: string
  pageNum?: number
  pageSize?: number
}

export function selectSeriesPage(
  query: SeriesQuery = {},
): Promise<ApiResult<PageData<MeetingSeriesItem>>> {
  return request.get<PageData<MeetingSeriesItem>>('/meeting/series/selectPage', {
    params: {
      // 空串转 undefined，Axios 就不会拼出 name= 这样的空条件
      name: query.name || undefined,
      status: query.status || undefined,
      cadence: query.cadence || undefined,
      page_num: query.pageNum ?? 1,
      page_size: query.pageSize ?? 10,
    },
  })
}

/** 系列详情：会议时间线 + 待办/记忆计数都在这一次返回里。 */
export function getSeriesDetail(id: number): Promise<ApiResult<MeetingSeriesDetail>> {
  return request.get<MeetingSeriesDetail>('/meeting/series/selectById/' + id)
}

/** 会议表单/筛选用的系列下拉选项（只返回当前用户可见的系列）。 */
export function seriesOptions(): Promise<ApiResult<SeriesOption[]>> {
  return request.get<SeriesOption[]>('/meeting/series/options')
}

export function addSeries(payload: SeriesPayload): Promise<ApiResult<MeetingSeriesItem>> {
  return request.post<MeetingSeriesItem>('/meeting/series/add', payload)
}

export function updateSeries(payload: SeriesPayload): Promise<ApiResult<MeetingSeriesItem>> {
  return request.put<MeetingSeriesItem>('/meeting/series/update', payload)
}

/** 归档不删数据：归档后系列不再参与建议与默认检索范围。 */
export function archiveSeries(id: number): Promise<ApiResult<null>> {
  return request.post<null>('/meeting/series/archive', { id })
}

/** 给一场会议推荐系列：返回候选与可解释的理由。 */
export function suggestSeries(meetingId: number): Promise<ApiResult<SeriesSuggestionResult>> {
  return request.post<SeriesSuggestionResult>('/meeting/series/suggest', {
    meeting_id: meetingId,
  })
}

/**
 * 把一场会议归入某个系列（或传 null 移出）。
 *
 * 归类是会后整理动作，不受「只有 SCHEDULED 会议可编辑」限制；权限由后端按
 * 会议写权限 + 目标系列可见性判定。
 */
export function assignMeetingToSeries(
  meetingId: number,
  seriesId: number | null,
): Promise<
  ApiResult<{ meeting_id: number; series_id: number | null; series_name: string | null }>
> {
  return request.post('/meeting/series/assign', {
    meeting_id: meetingId,
    series_id: seriesId,
  })
}
