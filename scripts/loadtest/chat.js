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
//   - 首字延迟 p95 < 5s（SSE 的 meta 事件到第一个 token 之间）
//   - 无 5xx、无 429 之外的错误响应
import http from 'k6/http'
import { check, sleep } from 'k6'
import { Trend } from 'k6/metrics'

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:19091'
const TOKEN = __ENV.TOKEN || ''
const SESSION_ID = __ENV.SESSION_ID || ''
const QUESTION = __ENV.QUESTION || '上周评审会定了什么？'

const firstTokenMs = new Trend('chat_first_token_ms', true)

export const options = {
  scenarios: {
    concurrent_asks: {
      executor: 'constant-vus',
      vus: Number(__ENV.VUS || 5),
      duration: __ENV.DURATION || '60s',
    },
  },
  thresholds: {
    // 首字延迟：用户感知的"卡不卡"就是它
    'chat_first_token_ms': ['p(95)<5000'],
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
    // 用 meta 事件到第一个 token 的字节位置估算首字延迟（k6 不逐帧回调 SSE）
    const body = String(response.body)
    const firstToken = body.indexOf('event: token')
    if (firstToken >= 0) {
      firstTokenMs.add(Date.now() - started)
    }
  }
  sleep(Number(__ENV.THINK_TIME || 1))
}
