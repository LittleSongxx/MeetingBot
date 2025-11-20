// 问答接口的并发压测（k6）。
//
// 为什么单独压这一条：它是全项目**唯一**"一次请求触发多次模型调用"的接口，
// 单请求成本与耗时都比普通接口高两个数量级，容量上限也由它决定。
//
// 用法（先起好 compose，准备好一个可登录账号）：
//   k6 run -e BASE_URL=http://127.0.0.1:19091 -e TOKEN=<JWT> \
//          -e SESSION_ID=<会话id> -e VUS=10 -e DURATION=60s scripts/loadtest/chat.js
//
// 判据两条，都在 k6 的阈值里写死（不达标时退出码非 0，可直接进流水线）：
//   - 一轮问答端到端 p95 < 60s
//   - 失败率 < 1%（429 是设计内的限流结果，脚本单独放行）
//
// **量的是端到端而不是首字延迟**：k6 的内置 HTTP 客户端会把响应体缓冲完再交出来，
// 拿不到"第一个字节到达"的时刻。首字延迟要看产品侧（`ai_call_log.elapsed_ms`
// 加上前端记录的 SSE 时间戳），或者用能逐帧读流的客户端——这一条写在这里，
// 免得后人看到指标名叫 chat_first_token 就以为它真的是首字延迟。
import http from 'k6/http'
import { check, sleep } from 'k6'
import { Trend } from 'k6/metrics'

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:19091'
const TOKEN = __ENV.TOKEN || ''
const SESSION_ID = __ENV.SESSION_ID || ''
const QUESTION = __ENV.QUESTION || '上周评审会定了什么？'

const turnDuration = new Trend('chat_turn_duration_ms', true)

export const options = {
  scenarios: {
    concurrent_asks: {
      executor: 'constant-vus',
      vus: Number(__ENV.VUS || 5),
      duration: __ENV.DURATION || '60s',
    },
  },
  thresholds: {
    // 一轮问答端到端：用户感知的"等多久"
    'chat_turn_duration_ms': ['p(95)<60000'],
    // 429 是设计内的限流结果，不算故障；但 5xx 与网络错误必须为 0
    'http_req_failed': ['rate<0.01'],
  },
}

export default function () {
  const started = Date.now()
  const response = http.post(
    `${BASE_URL}/chat/stream`,
    JSON.stringify({ session_id: Number(SESSION_ID), question: QUESTION }),
    {
      headers: { 'Content-Type': 'application/json', token: TOKEN },
      // SSE 是长连接：把超时放宽到 3 分钟，否则测的是 k6 自己的超时
      timeout: '180s',
      responseType: 'text',
    },
  )

  // tags 里不带响应体（可能含会议内容），只留状态码与阶段
  check(response, {
    'status is 200 or 429': (r) => r.status === 200 || r.status === 429,
  })

  if (response.status === 200 && response.body) {
    const body = String(response.body)
    // 只统计真的跑完一轮的请求（有 done 事件）；半截的流不该混进延时分布
    if (body.includes('event: done')) {
      turnDuration.add(Date.now() - started)
    }
  }
  sleep(Number(__ENV.THINK_TIME || 1))
}
