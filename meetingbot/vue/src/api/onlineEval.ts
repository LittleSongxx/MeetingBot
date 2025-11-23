/**
 * 在线评测接口封装（管理员手动触发，单次样本上限由后端把关）。
 *
 * 评测是异步的：POST /run 立即返回 RUNNING 的运行记录，页面轮询
 * GET /runs/{id} 等它变成 DONE / FAILED。这里只负责请求与类型，
 * 轮询节流与卸载清理放在 OnlineEvalPanel 组件里。
 */

import request, { type ApiResult, type PageData } from '@/utils/request'

/** 单次运行的聚合指标：分数与通过率都是 0~1；无样本时分数为 null。 */
export interface OnlineEvalSummary {
  avg_faithfulness: number | null
  avg_relevance: number | null
  pass_rate: number | null
  case_count: number
  failed_count: number
  /** 判定通过阈值（0~1），有样本时后端会带上 */
  threshold?: number
  /** 无样本等情况下的一句话说明 */
  reading?: string
}

/** 评测运行列表的一行（GET /observability/online-eval/runs）。 */
export interface OnlineEvalRun {
  id: number
  status: 'RUNNING' | 'DONE' | 'FAILED'
  sample_size: number
  model_config_id: number
  triggered_by: number
  started_at: string
  finished_at: string | null
  summary_json: OnlineEvalSummary | null
  error_message: string | null
}

/** 单条评测结果：失败样例抽屉读它。 */
export interface OnlineEvalCase {
  id: number
  run_id?: number
  chat_message_id?: number
  question: string
  answer: string
  citations_json: unknown[] | null
  tools_json: unknown[] | null
  /** 逐项分数与判定理由，键由后端定（faithfulness / relevance / pass / reason 等） */
  scores_json: Record<string, unknown> | null
  judge_model: string | null
  error_message: string | null
  create_time: string
}

/** 运行详情：运行记录 + 全部单条结果。 */
export interface OnlineEvalRunDetail extends OnlineEvalRun {
  cases: OnlineEvalCase[]
}

/** 触发一次评测：后端立即返回运行 id，真正的判定在后台任务里跑。 */
export interface OnlineEvalRunCreated {
  run_id: number
  status: OnlineEvalRun['status']
}

export function runOnlineEval(sampleSize: number): Promise<ApiResult<OnlineEvalRunCreated>> {
  return request.post<OnlineEvalRunCreated>('/observability/online-eval/run', {
    sample_size: sampleSize,
  })
}

export function listOnlineEvalRuns(
  pageNum = 1,
  pageSize = 5,
): Promise<ApiResult<PageData<OnlineEvalRun>>> {
  return request.get<PageData<OnlineEvalRun>>('/observability/online-eval/runs', {
    params: { page_num: pageNum, page_size: pageSize },
  })
}

export function getOnlineEvalRun(id: number): Promise<ApiResult<OnlineEvalRunDetail>> {
  return request.get<OnlineEvalRunDetail>('/observability/online-eval/runs/' + id)
}
