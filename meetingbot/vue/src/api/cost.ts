/**
 * 成本与缓存统计接口封装（GET /observability/cost）。
 *
 * 成本在库里一律是整数微元（1e-6 元），避免浮点累加误差；页面展示时才换算。
 * cache 只统计供应商真实上报过的输入 token（input_tokens / reported）：
 * 没上报的调用不进分母，否则缓存命中率会被算成 0 而不是"不知道"。
 */

import request, { type ApiResult } from '@/utils/request'

/** 按会议聚合的成本行。 */
export interface CostByMeeting {
  meeting_id: number
  meeting_title: string | null
  cost_micro: number
  call_count: number
}

/** 问答轮维度的统计：成本按助手消息 id 聚合成"轮"。 */
export interface CostByTurn {
  turn_count: number
  call_count: number
  /** 平均每轮成本（微元）；没有问答轮时为 null */
  avg_cost_micro: number | null
  /** 平均每轮模型调用次数 */
  avg_calls: number | null
}

/** 缓存命中统计：只统计供应商真实上报过的输入 token。 */
export interface CostCache {
  hit_tokens: number
  input_tokens: number
  /** 命中率 0~1；没有可统计的上报 token 时为 null（"不知道"不等于 0%） */
  rate: number | null
  /** 供应商是否上报了缓存命中 token（false 时命中率不可用） */
  reported: boolean
}

export interface CostResult {
  by_meeting: CostByMeeting[]
  by_turn: CostByTurn
  cache: CostCache
  /** 未定价调用次数：成本 0 与"不知道多少钱"是两件事，单独展示 */
  unpriced_count: number
}

export function getCost(dateFrom: string, dateTo: string): Promise<ApiResult<CostResult>> {
  return request.get<CostResult>('/observability/cost', {
    params: { date_from: dateFrom, date_to: dateTo },
  })
}
