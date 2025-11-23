/**
 * 模型路由接口封装：调用类型/用途 → 模型配置。
 *
 * 路由为空或目标配置不可用时后端回退到原用途（例如 CHAT → AGENT），
 * 所以前端只负责展示与增删改，不做任何路由计算。
 * 模型配置下拉复用 /aiModel/selectPage（一次取 200 条足够覆盖全部配置）。
 */

import request, { type ApiResult, type PageData } from '@/utils/request'

/** 一条模型路由（GET /aiModel/routes）。model_name 由后端联表带出。 */
export interface AiModelRoute {
  id: number
  /** MINUTES / AGENT / SPEAKER / CHAT / CHAT_SUMMARY / KB_QUERY_PLAN / KB_CONTEXTUALIZE */
  route_key: string
  model_config_id: number
  model_name: string | null
  enabled: boolean
  note: string | null
}

/** 新增/编辑载荷：id 有值走编辑，此时 route_key 不可改。 */
export interface AiModelRoutePayload {
  id?: number
  route_key: string
  model_config_id: number
  enabled: boolean
  note?: string | null
}

/** 模型配置下拉项（/aiModel/selectPage 的行里只用到这四个字段）。 */
export interface AiModelOption {
  id: number
  name: string
  model_type: string
  model_name: string
  enabled: boolean
}

export function listModelRoutes(): Promise<ApiResult<AiModelRoute[]>> {
  return request.get<AiModelRoute[]>('/aiModel/routes')
}

export function addModelRoute(payload: AiModelRoutePayload): Promise<ApiResult<AiModelRoute>> {
  return request.post<AiModelRoute>('/aiModel/routes', payload)
}

export function updateModelRoute(
  id: number,
  payload: AiModelRoutePayload,
): Promise<ApiResult<AiModelRoute>> {
  // 后端 PUT 的 id 在路径上；route_key 唯一且不允许改，载荷里留着也无害
  return request.put<AiModelRoute>('/aiModel/routes/' + id, payload)
}

export function deleteModelRoute(id: number): Promise<ApiResult<null>> {
  return request.delete<null>('/aiModel/routes/' + id)
}

/** 路由表单的模型配置下拉数据源。 */
export function listAiModelOptions(): Promise<ApiResult<PageData<AiModelOption>>> {
  return request.get<PageData<AiModelOption>>('/aiModel/selectPage', {
    params: { page_num: 1, page_size: 200 },
  })
}
