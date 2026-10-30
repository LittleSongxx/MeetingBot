# MeetingBot 后端只读审计报告（main 重写后版本）

- 审计对象：`\\wsl.localhost\Ubuntu-22.04\home\song\code\Agent\media_agent\MeetingBot`（Linux 路径 `/home/song/code/Agent/media_agent/MeetingBot`），运行栈 `meetingbot-aim-local-*`（backend `127.0.0.1:19092`、web `127.0.0.1:18083`、db 容器 `meetingbot-aim-local-db-1`）。
- 审计方式：**只读**。通读 `meetingbot/fastapi-app` 全部 api/services/common/models/bootstrap/demo_seed/tests、`db/*.sql`、`compose.yaml`、`Dockerfile`、`DEPLOYMENT.md`、`.env`/`.env.example`；用 `docker exec` 只做 SELECT，用 `curl` 只做 GET + 一次 admin 登录（仅签发 token，不写库）；**未修改仓库、未写数据库、未做 git 操作、未调用外部大模型**。
- 运行时库中数据为演示数据（`SEED_DEMO=1`），真实业务数据为空，因此并发/重试用例只做“静态推演 + 只读证据”，不实际制造写操作。

---

## 一、结论概览

共记录 **33 条**问题：**高 2 条 / 中 17 条 / 低 14 条**。

当前实现里 **bcrypt+JWT pv、零外键唯一索引、lifespan 启动恢复顺序、时区口径、单进程约束文档化** 等改造是到位的（见第六节“已核实无问题”）。真正需要优先处理的是两件事：

1. **演示数据重播种会把后端启动流程打崩**（演示数据被删后重启进入崩溃循环，整栈不可用）；
2. **任意“能看到这场会议”的员工都能点“重新生成纪要”，把已确认归档的纪要内容清空/覆盖并夺走 `created_by`**，无版本校验、不可恢复。

其余集中在三类：Agent 中断/恢复边界、零外键下的删除级联与并发删除、API 参数与权限口径。

### Top 5（严重度）

| # | 严重度 | 问题 |
|---|---|---|
| 1 | **高** | 删除演示转写任务/演示会议后重启，`demo_seed` 用固定 `AICDEMO0001…` 撞 `ai_call_log.request_id` 唯一索引，`bootstrap.py` 无异常兜底且 Dockerfile 用 `&& uvicorn` → 容器崩溃循环，后端不可用 |
| 2 | **高** | `POST /meetingMinutes/generate/{task_id}` 只校验“会议可见”，任意参会人可清空/覆盖 CONFIRMED 纪要（confirmed 信息一并清空）并把 `created_by` 改成自己 |
| 3 | 中 | Agent 在“最后一轮重写步被中断”后点恢复，`execute_agent_run` 先命中“轮数已满→直接 finalize”而跳过未完成重写，最终提示“已按 N 条问题重写”，但修订稿是上一轮的 |
| 4 | 中 | bootstrap 每次启动无条件 upsert 4 条模型配置（`id` 最小那条）：页面里改的 Key/地址/模型/启用状态在重启后被 `.env` 覆盖；`DASHSCOPE_API_KEY` 为空时会把库里的 Key 清空并禁用 |
| 5 | 中 | 分页参数无上/下界：负数/0 直接触发 `tortoise ParamsError`（实测返回“系统错误”），`page_size` 可任意大，叠加每行 4~5 条 SQL 与整段 `full_text`，可单请求打爆内存/DB |

---

## 二、问题总表

> 位置一律为仓库相对路径 `path:line`。触发条件以“现有 API + 现有数据”为准，标注【实测】的是本轮在运行栈上验证过的，标注【静态推演】的给出复现步骤但未实际写库。

### 2.1 高

| ID | 严重度 | 位置 | 触发条件 | 影响 | 建议 |
|---|---|---|---|---|---|
| H1 | 高 | `meetingbot/fastapi-app/demo_seed.py:166-185`（`request_id=f"AICDEMO{index:04d}"`，:171-173）；`models.py:250`（`request_id unique=True`）；`db/01-schema.sql:196`（`uk_ai_call_request_id`）；`api/meeting_minutes.py:132-143`（删纪要保留 `ai_call_log`，:137 注释“ai_call_log 保留作为审计记录”）；`api/meeting.py:352-382`（删会议不删 `ai_call_log`）；`bootstrap.py:251`、`bootstrap.py:255-265`（`seed_demo()`/`main()` 全链路无 try/except）；`Dockerfile:33`（`python bootstrap.py && exec uvicorn ...`）；`compose.yaml:52-53`（`restart: unless-stopped`） | `SEED_DEMO=1`（当前 `.env:38`、`.env.example:59` 默认就是 1）下，管理员把演示会议或演示转写任务删掉（页面允许：`api/transcription.py:274-301` 仅拦 PENDING/PROCESSING；`api/meeting.py:352` 只要求 admin），`ai_call_log` 里 10 条 `AICDEMO0001…0010` 被保留 → 下次容器重启时 `seed_ai_demo` 因“演示转写任务不存在”重新播种，`_create_call_logs` 再次插入同名 `request_id` → `IntegrityError` 从 `seed_ai_demo` 一路抛到 `asyncio.run(main())` → `bootstrap.py` 退出码非 0 → `&&` 短路，uvicorn 不启动 → 容器按 restart 策略反复重启，**后端持续不可用**（web 依赖 backend healthy，接口全挂） | ① 演示数据 `request_id` 改随机（如 `"AICDEMO"+uuid4().hex.upper()`）或先按固定集合 upsert/清理；② `seed_demo()` 外包 try/except：失败只记录并告警，**绝不阻断 uvicorn 启动**（启动脚本里 demo 数据属于“锦上添花”）；③ 给 demo 数据加显式标记（如 `call_type` 前缀/专用标记列或单独 demo 数据表），重播种前先清理标记数据；④ 补一条回滚：临时把 `SEED_DEMO=0` 重启可恢复服务（运维手册写明） |
| H2 | 高 | `meetingbot/fastapi-app/api/meeting_minutes.py:94-129`（`generate`：:101 只做 `get_accessible_meeting`；:112-126 复用旧行并清空内容；:124 `created_by=current_user.id`） | 任意对该会议有可见权限的账号（创建人/主持人/参会人）对任意已存在的纪要调 `POST /meetingMinutes/generate/{task_id}`：已 CONFIRMED 纪要会被直接改回 `GENERATING` 并清空 `summary/五个 JSON/confirmed_by/confirmed_time`，`created_by` 变成调用者；随后若生成失败（模型 Key 被清、模板被停用、模型报错），**原内容永久丢失**（无版本表、无软删除）；同时调用者获得该纪要的删除/取消确认/apply 修订权限 | 为 `generate` 增加权限：至少 admin 或 `minutes.created_by`（重新生成属于覆盖写）；CONFIRMED 纪要要求先 `unconfirm` 或二次确认；重新生成前把旧内容落到版本表/审计字段；`created_by` 语义若要保留历史，另设 `regenerated_by/last_generated_by` |

### 2.2 中

| ID | 严重度 | 位置 | 触发条件 | 影响 | 建议 |
|---|---|---|---|---|---|
| H2b | 中 | `api/meeting_minutes.py:109-117` | 同一 `source_task_id` 尚无纪要时，两个请求并发 `generate`：都过存在性检查后都 `create`，`meeting_minutes.source_task_id` 唯一索引抛 `IntegrityError`，`generate` 未捕获 | 用户拿到“系统错误”，第二条请求未落库/未调度 | 参考 `api/transcription.py:217-233` 的 `IntegrityError` 兜底处理 |
| M1 | 中 | `services/agent_service.py:386-390`（`if run.current_round >= run.max_rounds: finalize_run; return`）**先于** `:398-411` 的“中断恢复续跑重写”判断 | 自检运行到第 2 轮（`current_round=max_rounds=2`）的 `REFINE` 步被中断（用户点“中断”或进程被杀）→ 点“恢复”：`execute_agent_run` 先命中轮数上限直接 finalize，`:400-411` 的 `resume_issues` 永远不会生效。`finalize_run` 用上一轮的 `revised_json` + 本轮的 `issue_count/score` 拼出“已按 N 条问题重写纪要…达到 2 轮上限”，置 `WAITING_CONFIRMATION` | 用户看到“第 2 轮问题已修复”的结论，实际拿到第 1 轮修订稿；人工采纳后写入错误纪要；第 2 轮审查发现的问题从未被修复 | 将“已领取但未完成的重写”判断前置到轮数上限判断之前；`finalize_run` 增加“最后一轮 REFINE 是否 SUCCEEDED”的校验，未完成时回写真实状态（如重新入队/标失败），`final_result` 按实际完成轮次生成 |
| M2 | 中 | `api/agent.py:267-278`（`resume` 无 CAS：`:273-275` 无条件 `update`）；`services/agent_service.py:478-494`（`_running_agents` 进程内去重）、`:459-466`（CancelledError 处理） | ① `interrupt` 把库里状态改 `INTERRUPTED` 后 `cancel_agent_run` 尚未被旧协程处理，用户立刻 `resume`：`schedule_agent_run` 发现旧 task 未 done 直接 return，旧 task 随后收尾退出 → 库里 `RUNNING` 但**没有任何执行者**；② 双击 `resume`：第二个请求在第一个执行完成（如轮数已满快速 finalize）后把状态从 `WAITING_CONFIRMATION` 改回 `RUNNING` 再调度一次，产生重复 FINAL 步骤 | 自检“假 RUNNING”：SSE 每秒轮询永不结束、接口层 `resume` 只认 `INTERRUPTED`、`delete` 拒绝 RUNNING → 只能重启服务恢复；②重复步骤/状态抖动 | `resume` 用 CAS（`filter(id, status="INTERRUPTED").update(status="RUNNING")`，受影响行数为 0 就报“状态已变化”）；`schedule_agent_run` 不应在旧 task 未结束时静默返回，应排队或等旧 task 结束后再调度（`await`/回调）；interrupt 接口在 cancel 后置一个短暂“收口”状态或让 resume 复用同一 task |
| M3 | 中 | `api/agent.py:193-203`、`:280-289`；`services/agent_service.py:467-475` | `AgentRun` 失败后（模型异常/结构校验失败/模板被停用）状态为 `FAILED`：`resume` 只允许 `INTERRUPTED`，没有 retry 接口；用户只能 `delete` 后重新 `start`（重跑要重新计费、且历史运行/步骤被删） | 失败自检无法续跑；删除后 `ai_call_log` 孤儿（见 M8/M13），观测页统计口径漂移 | 增加 `POST /agent/retry/{run_id}`：`FAILED` 且 `current_round < max_rounds` 时允许从已完成步骤续跑；或允许 `resume` 接受 FAILED 并按 M1 修复后的续跑逻辑处理 |
| M4 | 中 | `api/agent.py:205-252`（`apply`：`:214-219` 只查 `CONFIRMED` 与权限，:236-251 直接整体覆盖） | 自检运行 `WAITING_CONFIRMATION` 期间，纪要可能已被“重新生成”（`generate` 清空重写）或被别人 `PUT /meetingMinutes/update` 修订过；此时 apply 仍把**旧版本**修订稿写回，没有 `update_time`/内容 hash 的版本绑定 | 新版本纪要（或用户手工编辑）被静默覆盖；若 `minutes.status=GENERATING` 也不会被拦（只拦 CONFIRMED） | apply 前校验 `minutes.update_time`/内容指纹与 run 创建时一致（run 上记录 `base_minutes_updated_at` 或内容 hash），不一致提示“纪要已变更，请重新自检”；同时拦 `GENERATING/FAILED` 状态 |
| M5 | 中（重点核实项，已复核对） | `bootstrap.py:114-156`（`upsert_model_configs`：:135 取 `model_type` 下 `id` 最小的一条；:144-147 **无条件 update** `name/provider/base_url/api_key/model_name/timeout_seconds/enabled`；:149-150 无 Key 时保持禁用；:152-155 同用途只留最新一条启用）；`bootstrap.py:73-100`（`ensure_admin`：:79 每次重新 `hash_password`、:96-99 每次 update `password/role/status`）；`api/ai_model.py:112-175`（页面可编辑 Key/地址/模型/启用） | 每次容器启动：① 管理员在「AI 模型配置」页面改过的那条（通常就是 bootstrap 创建、id 最小的一条）Key/base_url/model_name/enabled 被 `.env` 覆盖；② `.env` 里 `DASHSCOPE_API_KEY` 留空时，会把库中该条 `api_key` 清成 `""` 并 `enabled=False`（管理员在页面填的 Key 永久丢失）；③ 管理员口令被重设为 `.env` 值，因 bcrypt 重新加盐 → 行变化 → JWT `pv` 变化，**所有已登录管理员 token 在每次重启后全部失效**（README/DEPLOYMENT 只说口令会被改回，未提 token 失效）；④ 该配置一旦被业务数据引用（`api/ai_model.py:159-175` 禁止删除），页面编辑又多会被重启覆盖，实际变成“不可管理” | 文档已说明覆盖行为（`DEPLOYMENT.md:97-100`），仍建议：bootstrap 仅在配置**不存在或 api_key 为空**时写入，已存在则只记录差异不覆盖；提供“以 .env 为准/以页面为准”开关（如 `BOOTSTRAP_MODEL_CONFIG=seed\|fill\|off`）；`ensure_admin` 只在密码校验失败时重设，避免无意义改行导致 token 失效；为“重启后管理员掉线”补文档 |
| M6 | 中 | `.env.example:59`（`SEED_DEMO=1`）、`:61`（`DEMO_PASSWORD=change-me`）；`bootstrap.py:183-185`（`SEED_DEMO` 默认 “0”，但代码外默认值 `DEMO_PASSWORD="123456"`）、`:194-209`（创建 employee/operator 演示账号）；`demo_seed.py:9`（注释“生产部署不要开启 SEED_DEMO”） | 按 README 的 `cp .env.example .env` 部署到生产/半生产环境时，默认会创建 `employee`/`operator` 两个已知口令的 EMPLOYEE 账号和公开的演示会议；若 `DEMO_PASSWORD` 未设置，代码直接退回 `"123456"` | 固定弱口令账号（可登录、可被用于消耗模型额度/上传文件）；演示数据混入真实数据库；与“生产不要开”的注释自相矛盾 | `.env.example` 默认改 `SEED_DEMO=0`；`DEMO_PASSWORD` 缺失时拒绝创建演示账号（而不是回退 123456）；demo 账号加 `is_demo` 标记并在启动时检测/告警；README 部署清单里加“生产必须 SEED_DEMO=0” |
| M7 | 中（实测） | 分页参数全部无上下界：`api/meeting.py:464-466`、`api/transcription.py:342-344`、`api/meeting_minutes.py:195`、`api/agent.py:189`、`api/meeting_material.py:194-196`、`api/user.py:306`、`api/department.py:155`、`api/ai_model.py:195-197`、`api/prompt_template.py:101` | GET `...?page_size=-1` / `pageNum=0` → `offset`/`limit` 为负 → `tortoise.exceptions.ParamsError`（实测 backend 日志：`Limit should be non-negative number`）→ 被全局处理器转成 `{"code":"500","msg":"系统错误"}`（且 uvicorn 再记一条 “Exception in ASGI application”）；`page_size=100000` 可正常返回 | ① 前端任何分页组件传 0/负数就报“系统错误”，无法定位；② 大 `page_size` 单请求触发全表扫描 + 每行 N+1 + 返回整段 `full_text`（见 M15），任何登录用户可造成 CPU/内存/带宽压力 | 统一分页校验/包装（`1 <= page_num`，`1 <= page_size <= 100`，越界返回业务错误而非 DB 异常）；列表接口不返回 `full_text` 等大字段（见 M15） |
| M8 | 中 | `api/meeting.py:352-382`（会议删除：:361-364 先删磁盘文件，:366-380 删各表，**无“是否有运行中任务”检查、不删 `ai_call_log`**）；`services/transcription_service.py:482-492`、`services/minutes_service.py:187-191`、`services/agent_service.py:185-193/429-433`（后台任务写库均无“行仍存在”保护）；`api/meeting_minutes.py:137`（保留调用日志） | 管理员在转写/纪要/自检**运行中**删除会议（页面允许），或删除时序与后台写库交错：`TranscriptionTask/MeetingMinutes/AgentRun` 行已删，后台仍在跑 → `transcript_segment`/`agent_step` 仍会被创建（零外键，`bulk_create`/`create` 不会报错），`ai_call_log` 也被继续写入；后台任务还会继续消耗模型费用直到跑完 | 孤立行（`transcript_segment.meeting_id`、`agent_step.run_id` 指向已删除对象）、观测页/审计口径包含已删业务、费用不可控；与“删除会议=清掉全部业务数据”的接口语义不符 | 删除会议前检查三类任务是否有 `PENDING/PROCESSING/GENERATING/RUNNING`，有则拒绝或先中断；后台任务每次写库前确认父行仍存在（`update` 返回 0 即终止）；明确 `ai_call_log` 是审计保留还是随业务删除（当前“保留”但无任何清理/保留期限） |
| M9 | 中 | `services/transcription_service.py:406-501`（无断点续跑，:448-464 全部 chunk 在内存，:493-501 失败即 FAILED）；`:522-535`（启动把 PROCESSING 全退回 PENDING 重排）；`services/minutes_service.py:143-195`、`:216-224`（同理，纪要从头生成）；`services/transcription_service.py:93-123`（`request_id` 随机，无幂等键） | 容器重启/崩溃时任务进行到一半（转写已调用 3/4 段、纪要已调 2/3 段）：`recover_*` 直接从头重跑；同一分段/纪要分块会被重复调用模型、重复写 `ai_call_log`（随机 `request_id` 无法去重）；转写 `retry_count` 只在用户点“重试”时 +1，恢复重跑不计数 | 重复计费、观测页调用量/失败数偏高；`max_retry` 可被重启绕过；用户看到的 `retry_count` 与实际消耗不符 | 分段/分块结果落库（如 `transcription_task` 记录已完成 chunk、`ai_call_log` 用 `(call_type,biz_id,chunk_no)` 业务幂等键或唯一索引）；恢复时跳过已完成部分；恢复重跑也计入 `retry_count` 或单独 `recover_count` 并告警；为 `ai_call_log` 增加业务幂等键避免重复计费记录 |
| M10 | 中（单进程约束已文档化） | `services/transcription_service.py:409-418`（`status="PENDING"` 抢占，OK）；`services/minutes_service.py:145-151`（`status="GENERATING"`+`exclude(stage="已领取")`）；`services/agent_service.py:375-377`（同）；`services/agent_service.py:185`、`services/minutes_service.py:166`（首步进度更新即覆盖 `stage`，此后 `exclude(stage="已领取")` 不再有任何保护作用；transcription 用 `status` 抢占、不依赖 stage）；`compose.yaml:13-21`、`Dockerfile:27-33`、`DEPLOYMENT.md`（单进程/单副本约束） | 一旦单进程约束被破坏（`--workers>1`、`--scale backend=2`、误手起第二个容器、滚动发布出现新旧重叠）：minutes/agent 的“已领取”锁只保护到第一次进度更新前，之后另一个进程的抢占 update 会再次成功 → 同一任务被两个进程同时跑（重复模型调用、`agent_step` 唯一索引冲突→运行失败、SSE 落在错误进程）；`recover_*` 还会把另一个进程**正在运行**的任务重新入队（只有 transcription 用 PROCESSING 保护，minutes/agent 的 `GENERATING/RUNNING` 不区分“正在跑”和“被中断”） | 重复执行、重复计费、任务状态互踩（一个进程标记 FAILED 覆盖另一个的 SUCCEEDED） | ① 任务表增加 `lock_owner(instance_id)/locked_at/heartbeat_at`，抢占后周期性心跳，`recover_*` 只回收心跳超时的任务；② 所有“领取/状态流转”都改成带条件的 CAS 并保留独立 `claimed` 列，不依赖会被进度更新覆盖的 `stage`；③ 启动时用 DB 级 advisory lock（MySQL `GET_LOCK`）保证只有一个进程做 recover；④ 在 compose/CI 里加显式防呆（如启动时检测 worker 数>1 直接拒绝启动） |
| M11 | 中 | `api/agent.py:291-320`（SSE：`:300 while True`、`:301 每秒 AgentRun.get`、`:303 run_dict(...,True)` 内含 6~8 次查询、`:312 sleep(1)`、无心跳/超时/退避、`:301` 未捕获 `DoesNotExist`）；`api/agent.py:76-128`（`run_dict` 每次查 User/Meeting/AiModelConfig + `material_name_of_minutes`（再 3 查）+ steps） | 每个打开自检详情的浏览器页面每秒执行约 8 条 SQL；运行卡 `RUNNING`（M2）时连接和查询永久持续；运行被删除（admin 删会议/删 run）后 `AgentRun.get` 抛 `DoesNotExist` 未被捕获，流内 500；超过 nginx `proxy_read_timeout 900s` 无数据会断流（长模型调用期间无心跳） | DB 压力随查看人数线性放大；异常日志噪声；被删运行的事件流报 500；前端反复重连 | SSE 改用进程内事件（调度时 push，无变化不查库）+ 心跳（`: ping\n\n` 每 15s）+ 总时长上限/仅在 RUNNING 时轮询；`AgentRun.get_or_none` 为 None 时 yield 一条 terminated 事件并结束；`run_dict` 拆轻量事件结构（不查 User/Meeting/模型等静态信息，或缓存） |
| M12 | 中 | 权限口径散落：`api/meeting_material.py:316-342`（资料删除/批量删除只校验会议可见，任意参会人可删他人上传资料）；`api/transcription.py:239-271`（重试只校验会议可见，可替他人重试消耗额度）；`api/meeting_minutes.py:205-218`（`update` 任意会议可见者可改他人 DRAFT 纪要）、`:221-230`（`confirm` 任意会议可见者可确认他人纪要）、`:157-170`（retry 任意可见者）、`:94-129`（generate 任意可见者）；对照 `:132-143`/`:233-243`/`api/agent.py:205-219` 删除/取消确认/apply 才要求 admin 或创建人 | 普通参会员工可以看到会议 → 即可：删除别人上传的资料、改写/确认别人的纪要、重新生成纪要（H2）、发起自检/转写重试消耗模型额度 | 同一业务对象上“读/写/删”的权限口径不一致，越权面明显；模型额度可被普通员工消耗（成本 DoS） | 收敛成统一规则：读=会议可见；写/删=admin 或对象创建人（资料上传人、转写发起人、纪要创建人、运行发起人）；对 generate/retry/start 加频率限制或按人配额；前端按钮显隐只是辅助，服务端必须一致 |
| M13 | 中 | `services/observability_service.py:88-96`（把日期范围内**全部** `AiCallLog` 一次读进内存）、`:110-148` Python 循环聚合、`:205-215`；`db/01-schema.sql:196-199`（`ai_call_log` 无 `create_time` 前置索引，只有 `(call_type,create_time)`/`(model_config_id,create_time,status)`）；`services/observability_service.py:56-77`（员工过滤：SPEAKER_MATCH 恒 False；MINUTES 按 `created_by`、随重新生成漂移）、`:99-108`；`api/agent.py:198-203`（`selectByMinutes` 只校验会议可见，返回他人运行的详情） | 管理员默认查“最近 30 天”会全表扫描 `ai_call_log`（无可用索引）并在应用内存聚合；数据量增长后观测页变慢/内存升高；员工侧：自己的 SPEAKER_MATCH 日志永远看不到（漏），纪要被他人重新生成后原创建人看不到历史日志、新创建人能看到全量历史（口径漂移/跨用户）；员工可通过 `selectByMinutes` 看到同事的自检详情 | 观测页性能随日志量线性劣化，极端情况下 OOM；员工观测口径不一致；轻微越权（自检运行详情） | 增加 `(create_time)` 或 `(create_time,model_config_id,call_type)` 索引，用 SQL `GROUP BY` 聚合替代全量入内存，并对范围/分页上限做硬限制；`ai_call_log` 增加保留策略（分区/归档/定期清理）；员工过滤按 `biz_id` 归属 + `created_by` 历史快照（日志落 `owner_user_id` 冗余列）；`selectByMinutes` 按 `get_accessible_run` 同口径过滤或只返回自己的 |
| M14 | 中 | `api/transcription.py:274-301`（:286-295 删除任务时调用 `delete_minutes`）；`api/meeting_minutes.py:132-136`（`delete_minutes` 要求 admin 或 `minutes.created_by`） | 转写发起人删除自己的转写任务，但该转写生成的纪要已被别人“重新生成”过（H2 会把 `created_by` 改成对方）→ `delete_minutes` 抛 403 → 整个删除接口失败，**发起人无法删除自己的转写任务**；若纪要是 admin 创建的也会出现同样问题 | 删除链路被权限交叉卡死，用户只能找管理员；错误提示（“只有管理员或纪要创建人可以删除”）与用户操作（删除转写）不匹配 | 转写删除时的级联删除用“系统内部级联”权限（已通过转写删除权限校验即可），不要复用面向用户的 `delete_minutes` 权限；或在 `delete_minutes` 增加 `system=True` 参数 |
| M15 | 中 | `api/transcription.py:69-118`（`task_dict` 返回整段 `full_text`，:108；每行再查 Meeting/Material/User/Config/片段数 5 条 SQL）`:342-347`；`:121-148`+`:358-381`（`select_by_id` 对每条片段查用户 2 次、按标签再查用户）；`api/agent.py:76-128`（每行 4~6 条）；`api/meeting.py:175-221`（每个参会人 2 条，列表逐会议调用）；`api/meeting_minutes.py:64-91`（每行 4 条 + `material_name_of_minutes` 3 条）；`api/department.py:28-45`（每部门 1 条 count）；`api/user.py:263-281`（每用户查部门） | 任意列表页：`page_size=100` 时约 500+ 条 SQL、响应体包含 100 份 `full_text`（1 小时会议可达数百 KB~数 MB）；`transcription/selectById` 对 1000 条片段执行约 1000~2000 条 SQL；部门/会议候选人下拉全量 + N+1 | 页面加载慢、DB 压力大；大 `page_size`（M7）时可直接拖垮后端 | 列表 DTO 与详情 DTO 分离，列表不返回 `full_text`/六块纪要 JSON；`select_related`/批量 `id__in` 预取（Tortoise 不支持 join 时用映射表）；`selectById` 片段用户/部门一次性查字典；统计用 `GROUP BY` |
| M16 | 中 | `services/agent_service.py:467-475`（`except Exception` 后**无条件** `AgentRun.filter(id).update(status="FAILED", ...)`，未带 `status="RUNNING"` 条件）；`services/transcription_service.py:493-501`、`services/minutes_service.py:192-195`（同类吞错，仅落库）；`bootstrap.py:123-124`（`int(os.getenv("ASR_TIMEOUT_SECONDS","600"))` 未校验）；全仓失败路径无告警/指标 | ① 用户先点“中断”（状态 INTERRUPTED），随后在飞的模型调用抛普通异常（网络/协议错误而非 CancelledError）→ 被 `except Exception` 覆盖成 FAILED，界面显示“自检失败”而非“已中断”；② 转写/纪要/自检失败只写 DB + 日志，无任何告警/重试通知（运维只能靠用户反馈）；③ `.env` 里 `ASR_TIMEOUT_SECONDS=10m` 这类手误 → bootstrap `ValueError` → 服务起不来（且无明确提示） | 状态口径被覆盖；失败静默；启动失败原因不直观 | 状态更新全部带 CAS 条件（`filter(id, status="RUNNING")`）；后台任务失败接入日志告警（日志采集侧对 `level=ERROR` + `task failed` 规则）；启动参数统一走 `settings._env_int` 并在启动时打印/校验；`bootstrap` 对可恢复错误做重试/降级 |
| M17 | 中（重点核实项，见 M5 说明） | 同 M5 | 同 M5 | 同 M5 | 同 M5 |

> 说明：M5/M17 为同一问题（模型配置被 bootstrap 覆盖），此处并列是为了在“重点核实项”里单独可检索；总计数按 1 条计。

### 2.3 低

| ID | 严重度 | 位置 | 触发条件 | 影响 | 建议 |
|---|---|---|---|---|---|
| L1 | 低 | `main.py:43`（`FastAPI(lifespan=lifespan)` 未关闭 docs）；实测 `GET http://127.0.0.1:19092/docs` 200、`/openapi.json` 54KB 无鉴权 | 本机其他用户、同 compose 网络内其他容器可直接访问完整 API 文档；若将来改绑 `0.0.0.0` 或加反代即外网暴露 | 攻击面/接口信息泄露 | 生产设 `docs_url=None, redoc_url=None, openapi_url=None`（或用环境变量开关）；nginx 明确不反代 `/docs`（当前确实没反代） |
| L2 | 低（实测） | `api/files.py:61-73`（`GET /files/download/{filename}` 无任何鉴权依赖） | 拿到头像 URL 即可未登录下载文件；实测 `/files/download/1.png` 无 token 返回 200、118296 字节 | 员工头像等 PII 未授权可读（文件名 uuid 随机，降低可枚举性，但 URL 会出现在前端/日志里） | 给 `/files/download` 加 `Depends(get_current_user)`（头像可用签名 URL 或静态前缀），或把头像校验到同部门/公开白名单 |
| L3 | 低 | `settings.py:82-88`（CORS 缺省 localhost:5173）、`common/exception_handler.py:54-58`（全局异常处理器硬编码 `Access-Control-Allow-Origin: *`） | 未设 `CORS_ALLOW_ORIGINS` 时默认放行本地 5173；任何未处理异常响应都会带 `ACAO:*`（即使 CORS 中间件未启用） | 与“同源部署、无 CORS”的配置语义不一致；异常响应可被任意源读取（内容仅“系统错误”，影响小） | 默认值改为空（同源）；全局异常处理器不要手写 CORS 头，交由中间件层处理 |
| L4 | 低 | `settings.py:78`（`REGISTER_ENABLED = _env_bool(..., True)`，注释写“默认关闭”）；`api/__init__.py:82-110` | 新部署漏配 `REGISTER_ENABLED` → 开放注册，任何人可创建 EMPLOYEE 账号（随后可建会议/上传 500MB/发起 AI 调用） | 与注释/安全预期相反 | 默认值改为 `False`；注册接口加频率限制 |
| L5 | 低 | `api/transcription.py:255-271`（条件 update 的结果未判断就 `schedule`；`retry_count` 用旧读值计算）；`api/meeting_minutes.py:157-170`（retry 不刷新 `model_config_id`） | 并发点重试时后一个请求拿到 0 行更新仍返回成功；管理员失败后换了新模型配置，纪要重试仍用旧 `model_config_id`（与转写 retry 行为不一致） | 用户误以为已重试；模型配置切换后纪要重试可能继续失败 | 检查 update 返回值，0 则报“状态已变化”；纪要 retry 与 transcription retry 对齐：重新取当前启用配置并更新 `model_config_id` |
| L6 | 低 | `services/transcription_service.py:299`（`request_size=音频文件字节数`）、`:311`（`response_size=len(str(detail))` **字符数**）、`:114-115`；`services/model_factory.py:66/81`（UTF-8 **字节数**）；`services/model_factory.py:87-99`（成功才写 `usage_json`，失败为 None）；`demo_seed.py:143-165`（固定假数值，`MINUTES_CHUNK` request 12000 与 `.env MAX_CHUNK_CHARS=6000` 不符） | 观测页“请求/响应大小”“Token/失败率”口径混用字节与字符；失败调用 token=0；演示数据与真实链路数值不可比 | 图表/排行口径失真，跨模型对比不公平 | 统一为 UTF-8 字节（或统一字符）并在文档/字段注释写清；失败调用记录可得的 usage（部分 Provider 错误也带 usage）；demo 数据用真实量级并注明样例 |
| L7 | 低 | `demo_seed.py:188-199`（幂等 guard 仅判断“该音频已有转写任务”） | 首次播种在“建任务成功后、写纪要/自检/日志前”失败（进程崩溃等）：下次启动 guard 命中直接跳过 → 演示数据永远缺纪要/自检/日志，且不会补 | 演示环境数据不完整，排查困难 | guard 改为按“各子块是否齐全”逐块幂等补齐；播种过程加事务或“已播种标记”表 |
| L8 | 低 | `services/minutes_service.py:99-104`、`services/speaker_service.py:38-43`、`services/agent_service.py:253-262/298-308`（`render_prompt` 顺序 `str.replace`，无转义） | 会议标题/纪要/转写内容里出现 `{transcript}`、`{issues}`、`{partials}` 等占位符时，会被后续变量的 `.replace` 二次替换（如标题里写 `{transcript}` 会把整段转写塞进标题位置） | 提示词内容被用户可控文本污染（轻量 prompt injection 面） | 单遍渲染（正则一次替换，替换值不再参与匹配），或先转义内容里的 `{`/`}` |
| L9 | 低 | `services/model_factory.py:121-122`（`raise RuntimeError(f"{label}调用失败：{detail[:1000]}")`）；`api/transcription.py:453-457`（把异常文本直接转 `CustomException`）；`api/meeting_minutes.py:88`（`error_message` 前端展示） | 上游模型/HTTP 错误原文（可能含请求 ID、内部 URL、schema 细节）透传到页面/DB | 轻微信息泄露，错误提示不友好 | 对外错误摘要化（“模型服务调用失败，请稍后重试/联系管理员”），详细原因只进日志与 `ai_call_log.error_message` |
| L10 | 低 | `bootstrap.py:159-166`（`elif not rows` 永远走不到：`missing` 已覆盖空表情况） | 代码维护性 | 死代码误导 | 删除 `elif`，或空表单独告警 |
| L11 | 低 | `api/meeting.py:393-424`（`selectOptions` 不分页返回全部可见会议）；`api/meeting.py:175-221`（每个会议 N+1 查人/部门） | 历史会议多时下拉框接口越来越慢/响应大 | 前端卡顿、DB 压力 | 下拉框加 `limit`/搜索条件，DTO 精简（只返回 id/编号/标题/状态/时间） |
| L12 | 低 | `api/meeting.py:32-50`、`api/meeting_material.py:89-111`、`api/user.py:132-165`、`api/department.py:64-77`（无任何长度/字符集校验，只有非空） | 标题/文件名/姓名等超 255/100 字符 → MySQL `DataError` → 被全局处理器吞成“系统错误” | 用户看到“系统错误”，输入没有友好提示 | Pydantic `Field(max_length=...)` + 统一业务错误 |
| L13 | 低 | `_create_call_logs`/demo 数据的业务日期写死（`demo_seed.py:75-96` 决策期限 2026-09-01 等）；`bootstrap.py:216-221`（演示会议状态固定 `SCHEDULED` 却挂已完成转写/纪要/自检） | 演示数据语义不一致（会还没开始就有纪要自检；期限早于当前日期） | 演示/截图可信度、评审误导 | 演示数据日期相对 `now()` 生成，会议状态与数据一致（如 `FINISHED`） |
| L14 | 低 | 全仓：`ai_call_log` 无任何删除/归档路径（`api/meeting.py:352-382`、`api/meeting_minutes.py:132-143`、`api/transcription.py:274-301`、`api/agent.py:280-289` 都保留）；`transcription_task/metering` 亦然 | 长期运行后 `ai_call_log`、`transcript_segment` 无限增长（转写片段已随任务删，日志不删） | 磁盘/观测查询持续劣化 | 增加保留策略与清理任务（按天分区或定期归档），文档写明“审计保留 N 天” |

---

## 三、重点专题结论（对应任务要求 1~7）

### 3.1 异步任务：领取锁 / 恢复 / 失败态 / 幂等

- **单进程内基本安全**：`schedule_*` 用进程内字典去重；`execute_transcription` 用 `status=PENDING` 的 CAS 抢占，`execute_minutes`/`execute_agent_run` 用 `status + exclude(stage="已领取")` 抢占；`recover_*` 在 lifespan 中先于 `yield` 执行。实测启动日志顺序正确：`Tortoise-ORM started → 异步任务恢复完成 → Application startup complete`。
- **锁不是锁（M10）**：`stage` 是展示字段，第一步 `open_step`/progress `update` 就把它改掉；抢占条件“`exclude(stage='已领取')`”只在第一次有意义，之后任何再调度都能抢占成功。单进程靠字典，多 worker/多副本即双跑。
- **恢复会把“正在跑”当“被中断”（M10/M9）**：transcription 用 `PROCESSING` 保护自己，但进程重启后会全部退回 PENDING 重跑；minutes/agent 的 `GENERATING/RUNNING` 不区分执行者，另一个进程（或滚动发布中的新容器）会把它们重新入队。
- **崩溃/重启状态**：三类任务都没有“已领取但未完成”的断点：转写分段结果只在最后一次性写库（`transcription_service.py:469-480`）、纪要/自检从头再跑，导致**重复调用模型、重复写 ai_call_log**（M9）；`retry_count` 不因恢复重跑而增加，`max_retry` 防不住重启。
- **失败态重试**：转写（`api/transcription.py:239-271`）与纪要（`api/meeting_minutes.py:157-170`）有 retry；**Agent 运行没有 retry**，失败后只能删除重建（M3）。
- **幂等**：表级唯一索引覆盖了 `meeting_material(meeting_id,file_hash)`、`transcription_task(material_id)`、`meeting_minutes(source_task_id)`、`agent_step(run_id,step_no)`；但 AI 调用日志没有业务幂等键（随机 `request_id` + 固定 demo ID 反而引出 H1）。

### 3.2 Agent：轮次边界 / 中断 / 恢复 / SSE

- **轮次边界**：`agent_graph.py` 的 review→refine 循环、`round_no > max_rounds` 兜底、`recursion_limit(max_rounds)=max_rounds*2+2` 都正确，单测覆盖充分。`current_round` 在 review 完成时写入；**未完成 REFINE 时 `current_round` 已等于轮数**，这是 M1 的根因。
- **恢复入口**：`resume` 只允许 `INTERRUPTED`，会清 `error_message/finished_at` 并重新调度；续跑时先杀掉上次残留 RUNNING step（`agent_service.py:382-385`），再从 `current_round/revised_json` 接续——**但“最后一轮重写中断”被上限判断抢先**（M1）。
- **中断**：`interrupt` 状态 CAS + `cancel`，`CancelledError` 分支会把在飞步骤标 FAILED、不覆盖 INTERRUPTED（这条做对了）。但 `resume` 没有 CAS，且进程内调度器在旧 task 未 done 时静默返回 → 可能卡 RUNNING（M2）。
- **finalize 条件更新**：`finalize_run` 用 `filter(status="RUNNING").update(...)` 做 CAS，避免覆盖 INTERRUPTED；这点是好的。缺陷在 `final_result` 文案不校验“最后一轮是否真的完成”（M1），以及 `except Exception` 分支无条件标 FAILED（M16）。
- **SSE**：`/agent/events/{run_id}` 每秒 `AgentRun.get` + `run_dict(True)`（6~8 条 SQL），无心跳/无上限/无异常保护（M11）；终止状态判断覆盖 SUCCEEDED/FAILED/INTERRUPTED/REJECTED/WAITING_CONFIRMATION（正确）。

### 3.3 数据一致性：零外键级联覆盖表 / 孤儿 / 并发删除

`db/01-schema.sql` 声明零外键、靠应用层维护。逐表核对删除路径：

| 表 | 删除入口 | 覆盖情况 |
|---|---|---|
| department | 删除/批量 | 先校验无 user 引用，OK |
| user | 删除/批量 | `has_business_reference` 覆盖 9 处引用，OK（检查与写入非原子，零外键下仍有理论孤儿） |
| meeting | 会议删除（admin） | participant/material(+磁盘)/transcription_task/transcript_segment/meeting_minutes/agent_run/agent_step 全删；**不删 ai_call_log；不拦运行中任务**（M8） |
| meeting_participant / transcript_segment | 随父删 | OK |
| meeting_material | 删除/批量 | 有转写任务即拒删；删除文件+行，OK |
| transcription_task | 删除（发起人/admin） | 删片段、删纪要（含 run/step）；**保留 ai_call_log；受纪要权限牵连**（M14）；PENDING/PROCESSING 拒删（TOCTOU） |
| ai_model_config | 删除（admin） | 校验三张业务表引用；**不校验 ai_call_log**（孤儿 `model_config_id`） |
| meeting_minutes | 删除/随任务删 | 删 agent_run/step；**保留 ai_call_log** |
| agent_run | 删除/随纪要删 | 删 agent_step；**保留 ai_call_log** |
| agent_step | 随 run 删 | OK |
| ai_call_log | **没有任何删除入口** | 永久增长（L14），且删除业务后成为孤儿（观测口径/审计问题） |

并发删除 × 后台执行：`api/meeting.py:352-382` 的会议删除不与三类运行中任务互斥，后台任务写库不带“父行仍存在”条件 → 可产生 `transcript_segment`/`agent_step` 孤儿并继续消耗模型费用（M8）。
`meeting_minutes.source_task_id` 唯一 + regenerate 语义：`generate` 复用旧行（不会撞唯一索引），但**并发首次生成未捕获 IntegrityError**（H2b）；更严重的是覆盖已确认内容（H2）。

### 3.4 API 边界 / 权限 / 暴露面

- **分页**：9 个列表接口全部无上下界（M7，实测负数返回“系统错误”）。
- **批量删除 body**：五个批量删除入口已改为 `Body(list[int])`，空列表处理不一致（资料/用户拒绝，部门静默成功）；无 body 时 FastAPI 422 被转成“请求参数错误”，可接受。
- **权限口径**（M12 汇总）：读=会议可见；但资源删除（资料）、转写重试、纪要编辑/确认/重新生成、自检发起都只要求“会议可见”；而纪要删除/取消确认/apply 要求 admin 或创建人 → 同一对象读写删不一致。
- **`/docs`、`/openapi.json` 无鉴权**（L1）；`/files/download` 无鉴权（L2，实测可未登录下载头像文件）；CORS 默认/异常处理器硬编码 `*`（L3）；注册默认开（L4）。
- **其它实测**：`/meetingMaterial/download/1` 无 token 返回 HTTP 200 但 body 是 `{"code":"401",...}`（业务层鉴权生效，不是越权）；`/health` 含 DB 探活（正确）。

### 3.5 bootstrap / demo_seed（重点核实）

**已核对结论**：

1. **每次启动都会 upsert 4 条模型配置，且无条件覆盖**：`upsert_model_configs` 取同用途 `id` 最小的一条，`update(name, provider, base_url, api_key, model_name, timeout_seconds, enabled)`。→ 页面改的 Key/地址/模型名/启用状态在重启后被 `.env` 覆盖；`DASHSCOPE_API_KEY` 为空时把库中 Key 清空并禁用（M5）。`DEPLOYMENT.md:97-100` 已明说“每次重启都会按 .env 重新 upsert … 要长期保留请直接写进 .env”，属于**已文档化的设计取舍，但风险仍在**（管理员在页面保存成功却在重启后静默回退，且会覆盖成空 Key）。
2. **管理员口令每次启动被重设**：`ensure_admin` 每次都 `hash_password(APP_ADMIN_PASSWORD)` 并 update → bcrypt 新盐 → `password` 列变化 → token 里的 `pv` 失效 → **每次重启所有管理员被强制重新登录**（README/DEPLOYMENT 只提到口令会被改回，未提 token 失效）。实测 DB：`user` 表中 admin 的 `update_time=2026-10-30 14:04:36`（= 最近一次 bootstrap 启动时间），其余用户仍为建库时间，证明每次启动都在改密码行。
3. **demo 数据幂等性有致命缺口**（H1）：AI 演示日志用固定 `AICDEMO0001…`，而 `ai_call_log` 被有意保留、演示业务行又允许删除；“删除演示转写任务/演示会议 + 重启”= bootstrap `IntegrityError` → 服务崩溃循环。`README` 只教“不需要时把 SEED_DEMO 改 0”，没有说明“通过页面删除演示数据会导致重启起不来”。
4. **SEED_DEMO 默认开启**（`.env.example:59`）+ 演示账号固定弱口令（`bootstrap.py:185` 默认 `123456`、`.env` 为 `123456`）→ 直接 `cp .env.example .env` 部署就带固定口令账号和演示数据（M6）。
5. **demo 与真实业务交互**：
   - 演示音频资料 `material_id` 被演示转写任务占用（`transcription_task.material_id` 唯一）→ 真实用户对该演示音频点“发起转写”会收到“该资料已完成转写”，无法重转；
   - 真实用户若先转写演示音频（仅在演示数据被删且未重播种的窗口内可能），后续 `seed_ai_demo` 的 guard 命中 → 演示 AI 数据不再补齐（L7）；
   - 真实用户“重新生成”演示纪要会清掉演示内容（同 H2），重启后 seed 因“任务已存在”跳过，演示数据不会恢复。
6. `bootstrap.py:251` 的 `seed_ai_demo` 在 `seed_demo` 末尾、无保护；`bootstrap.py:255-265` 的 `main()` 只在 `finally` 关连接，没有 `try/except` → 任何播种异常都阻断服务启动（与 `Dockerfile:33` 的 `&&` 组合放大）。

### 3.6 可观测

- **口径**：`request_size/response_size` 字节 vs 字符混用（L6）；失败调用没有 usage（token 统计偏低）；`demo_seed` 的数值是假数据且与 `.env` 配置不一致。
- **性能**：默认 30 天范围把整段 `ai_call_log` 读进内存聚合（M13），`ai_call_log` 缺 `create_time` 前置索引 → 全表扫描；`ai_call_log` 无清理策略。
- **员工过滤**：按 `call_type` 选集合（TRANSCRIPTION→initiator、MINUTES→created_by、AGENT→run.user_id）；SPEAKER_MATCH 恒不可见（漏）；MINUTES 归属随重新生成漂移；`selectByMinutes` 可看到他人的自检详情（轻微越权）。管理员侧无过滤，看到全部（符合预期）。

### 3.7 异常与日志

- 全局 `@app.exception_handler(Exception)` 返回 HTTP 200 + `{"code":"500","msg":"系统错误"}`，**不向客户端泄漏堆栈**（正确），服务端 `logger.exception` 有完整堆栈（正确）；但会把所有未处理异常（含 `ParamsError`、`DoesNotExist`）都变成“系统错误”，前端无法区分（M7/M11）。
- 大量 `except Exception` 只落库 + 日志：转写/纪要/自检三类后台任务失败**没有任何告警/指标**，用户不刷新页面就不知道失败；`execute_agent_run` 的兜底会覆盖 `INTERRUPTED`（M16）。
- 日志格式：JSON/text 可切换；bootstrap 日志包含「已更新管理员账号」「模型未配置 Key」等提示，但**没有“本次覆盖了页面配置/密码被重设”的显式告警**，运维难以及时发现 M5。
- 敏感信息：未发现密码/Key 明文进日志；`api_key` 列表只回掩码（`api/ai_model.py:35-43`）；模型错误原文会透传页面（L9）。

---

## 四、已核实无问题 / 防护到位（避免误报）

1. **FastAPI lifespan + Tortoise 初始化顺序**：实测启动日志 `Tortoise-ORM started` → `异步任务恢复完成` → `Application startup complete`，恢复函数在对外服务前执行，无“Tortoise 未初始化”问题。
2. **时区口径**：`TORTOISE_ORM` 的 `use_tz=False/timezone="Asia/Shanghai"` 被 Tortoise 1.1.8 正确读取（`USE_TZ/TIMEZONE` 环境变量 + cache 重置）；`auto_now_add` 落库为本地墙钟，显式 `now()`（+08 aware）也按本地墙钟落库，读回 `format_datetime` 统一 +08，实测一致。
3. **认证**：bcrypt 哈希 + 登录明文迁移 + JWT `pv` 指纹（改密/改口令后旧 token 失效）+ 角色以数据库为准 + 停用账号拦截；单测覆盖。
4. **零外键幂等约束**：4 组唯一索引覆盖了重复上传、重复转写、重复纪要、重复步骤；`IntegrityError` 在资料上传/转写发起处有兜底（纪要生成处缺，见 H2b）。
5. **安全边界**：`/files/download` 有路径穿越防护（`Path(filename).name != filename`）；上传大小限制 5MB/500MB；文件名清洗；SQL 全部 ORM（仅 `/health` 一条 `SELECT 1`）；全局异常不泄漏堆栈。
6. **部署约束**：`compose.yaml` 只绑 `127.0.0.1`、`Dockerfile` 固定 `--workers 1`、`compose.yaml:13-21` 与 `DEPLOYMENT.md` 明确写出“单进程/单副本、不能扩副本”的原因——这一点文档是到位的（但锁实现仍不健壮，见 M10）。
7. **健康检查**：`/health` 真的查 DB，失败返回 503（容器 healthcheck 依赖它）。

---

## 五、建议修复顺序

**P0（本周内）**

1. H1：demo 日志 `request_id` 随机化/幂等化；`seed_demo()` 整体 try/except 不阻断启动；补“删除演示数据后如何恢复”的运维说明。
2. H2：`generate` 加 admin/创建人权限；CONFIRMED 需先取消确认或二次确认；落旧版本/审计字段；`created_by` 不随重新生成覆盖（或另设字段）。
3. M5：bootstrap 模型配置改为“仅缺失/空 Key 时写入”，或在日志中显式打印“覆盖了页面配置”；`ensure_admin` 改密码逻辑避免重启必改行（token 全失效）。
4. M1/M2：Agent 恢复顺序修正 + `resume` CAS + 卡 RUNNING 的恢复通道。
5. M7：统一分页校验（1..100）与错误提示，全部列表接口生效。

**P1（两周内）**

M8/M9/M10（删除/运行互斥、断点续跑与幂等键、任务心跳锁）；M11（SSE 事件化 + 心跳 + 超时）；M12（权限口径统一）；M13（观测页 SQL 聚合 + 索引 + 员工口径）；M6（SEED_DEMO 默认关、demo 口令策略）；M14。

**P2（持续）**

M15 N+1/大字段、M16 告警与 CAS、L1~L14（docs/文件鉴权/CORS/注册默认/错误摘要化/数据保留策略等）。

---

## 六、附录：本轮只读验证证据（节选）

1. 容器/健康：`meetingbot-aim-local-backend-1 Up healthy 127.0.0.1:19092->9090`、`-db-1 healthy`、`-web-1 healthy`；`GET /health → 200 {"status":"ok","db":"up"}`。
2. 启动日志（最近一次 backend 启动）：
   ```
   14:04:36 INFO [__main__] 已更新管理员账号
   14:04:36 INFO [demo_seed] AI 演示数据已存在，跳过
   14:04:36 INFO [__main__] bootstrap 完成
   14:04:38 INFO [tortoise] Tortoise-ORM started ...
   14:04:38 INFO [main] 异步任务恢复完成
   14:04:38 INFO [uvicorn.error] Application startup complete.
   ```
3. DB（只读 SELECT）：
   - `ai_model_config` 4 条（id=1 TRANSCRIPTION/DASHSCOPE、id=2 MINUTES、id=3 AGENT、id=4 SPEAKER，均 `enabled=1`，`api_key` 长度 116，与 `.env` 一致）；
   - `user`：admin `update_time=2026-10-30 14:04:36`（证明每次 bootstrap 重写密码），employee/operator 为建库时间；
   - `ai_call_log` 共 10 条，全部为演示数据（`AICDEMO0001`~`AICDEMO0010`，含 1 条 FAILED、usage 缺失）——**这 10 个固定 ID 是 H1 的触发条件**；
   - `agent_run` AGR-DEMO-001 `WAITING_CONFIRMATION`、`current_round=2/2`、4 个步骤；`transcription_task` 1 条 SUCCEEDED、`meeting_minutes` 1 条 DRAFT；孤儿日志检查（按 call_type 关联业务表）当前均为 0。
4. HTTP（仅 GET + 一次 admin 登录）：
   - `GET /docs → 200`、`GET /openapi.json → 200 (54291 bytes)` 无鉴权（L1）；
   - `GET /files/download/1.png` 无 token → `200`、`118296` 字节（L2）；`GET /meetingMaterial/download/1` 无 token → HTTP 200 但 body `{"code":"401",...}`（业务鉴权生效，不算绕过）；
   - `GET /meeting/selectPage?page_size=-1`、`?page_num=0&page_size=5`；`/transcription/selectPage?pageNum=-1`；`/meetingMinutes/selectPage?pageNum=-5`；`/agent/selectPage?pageSize=-1` → 均 `{"code":"500","msg":"系统错误"}`（M7 实测）；`page_size=100000` 正常返回；
   - backend 日志对应：`ERROR 未处理异常：ParamsError('Limit should be non-negative number')` + `tortoise.exceptions.ParamsError` 堆栈（并伴随 uvicorn 的 `Exception in ASGI application` 重复堆栈）。
5. 说明：H1 的崩溃循环为**静态推演**（依据：固定 `AICDEMO` request_id + 唯一索引 + 删任务/会议不删日志 + bootstrap 无 try/except + Dockerfile `&&`）；本轮未实际删除演示数据/重启容器（不允许写操作）。复现步骤：`SEED_DEMO=1` 下用 admin 在转写任务页删除演示任务（或会议管理删除演示会议）→ `docker compose restart backend` → 观察 backend 日志 `IntegrityError` 与容器反复重启；把 `.env` 的 `SEED_DEMO` 改 0 再重启可恢复。
