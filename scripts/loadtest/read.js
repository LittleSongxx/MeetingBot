// 列表类接口的并发压测（k6）：这些是"打开页面就会打"的读路径，
// 容量瓶颈通常在这里先暴露（数据库连接池 10 条、接口里的多次串行查询）。
//
//   k6 run -e BASE_URL=http://127.0.0.1:19091 -e TOKEN=<JWT> \
//          -e VUS=20 -e DURATION=60s scripts/loadtest/read.js
import http from 'k6/http'
import { check, sleep } from 'k6'

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:19091'
const TOKEN = __ENV.TOKEN || ''

export const options = {
  scenarios: {
    browsing: {
      executor: 'constant-vus',
      vus: Number(__ENV.VUS || 20),
      duration: __ENV.DURATION || '60s',
    },
  },
  thresholds: {
    // 读接口的用户可接受线：p95 低于 800ms
    'http_req_duration{name:selectPage}': ['p(95)<800'],
    'http_req_failed': ['rate<0.01'],
  },
}

// 首页 + 会议列表 + 纪要列表 + 观测页：四个页面首次进入时会打的请求
const PATHS = [
  ['/meeting/selectPage?pageNum=1&pageSize=10', 'selectPage'],
  ['/meetingMinutes/selectPage?pageNum=1&pageSize=10', 'selectPage'],
  ['/transcription/selectPage?pageNum=1&pageSize=10', 'selectPage'],
  ['/observability/overview', 'observability'],
]

export default function () {
  for (const [path, name] of PATHS) {
    const response = http.get(`${BASE_URL}${path}`, {
      headers: { token: TOKEN },
      tags: { name },
    })
    check(response, { 'status is 200': (r) => r.status === 200 })
  }
  sleep(Number(__ENV.THINK_TIME || 1))
}
