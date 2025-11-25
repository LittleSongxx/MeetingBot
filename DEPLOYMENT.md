# MeetingBot 本地 Docker 部署

配置文件为 `meetingbot/fastapi-app/.env`。密钥不进入镜像，也不提交到版本控制。

## 启动

在项目根目录运行：

```bash
./scripts/compose.sh up -d --build
./scripts/compose.sh ps
```

网页：<http://127.0.0.1:18082>。后端健康检查：<http://127.0.0.1:19091/health>。
登录账号和密码分别查看 `.env` 的 `APP_ADMIN_USERNAME`、`APP_ADMIN_PASSWORD`。
**若 `APP_ADMIN_PASSWORD` 留空，种子账号 `admin/admin` 会生效——只适用于本地演示，公网部署必须设置强密码。**

`scripts/compose.sh` 统一从指定位置读取 `.env`，无需把密钥放到项目根目录。
后端可以多 worker / 多副本：登录限流与问答预算的计数已搬到 Redis（`common/ratelimit.py`），
多个进程共享同一份计数。镜像默认 `WEB_CONCURRENCY=2`，按机器核数调整即可。
**未配置 `REDIS_URL` 时会退回进程内存实现**——那种部署下多副本的阈值语义与单进程不同
（每个进程各算一份），要保阈值语义就配 Redis。Redis 不可达时限流与预算闸 **fail-open**
（放行并记 ERROR 日志），这是刻意的取舍：计数组件故障不该把登录与问答一起变成不可用。
MySQL 仅在 Compose 网络内开放，网页及后端仅绑定宿主机回环地址。
数据保存在本项目独立的 `meetingbot-local_mysql_data` 卷；SQL 样例只在空卷初始化时导入。上传文件保存在原项目 `fastapi-app/files` 目录。

## 模型配置

通常只需填写 `DASHSCOPE_API_KEY`。默认地址对应阿里云百炼中国内地（北京）地域，Key 必须与服务地域匹配，账号需要有相应模型的调用权限。

| 参数 | 用途 |
|---|---|
| `DASHSCOPE_API_KEY` | Qwen 与 ASR 共用密钥 |
| `LLM_API_KEY` / `ASR_API_KEY` | 可选的独立密钥，非空时覆盖共用密钥 |
| `LLM_BASE_URL` | Qwen 的兼容 API 地址，默认以 `/compatible-mode/v1` 结尾 |
| `DASHSCOPE_BASE_URL` | ASR 地址，默认以 `/api/v1` 结尾 |
| `LLM_MODEL` | 文本任务默认模型，初始为 `qwen3.8-max-0902` |
| `MINUTES_MODEL` / `AGENT_MODEL` / `SPEAKER_MODEL` | 可分别覆盖纪要生成、Agent 自检、说话人匹配模型 |
| `ASR_MODEL` | 录音文件转写模型，初始为 `qwen-audio-3.1-asr-flash-filetrans` |
| `LLM_TIMEOUT_SECONDS` / `ASR_TIMEOUT_SECONDS` | 请求超时参数 |
| `LLM_ENABLE_THINKING` | Qwen 思考开关；本轮固定为 `false` |
| `LLM_MAX_TOKENS` | 单次文本输出上限（可按调用类型分档：`LLM_MAX_TOKENS_MINUTES` 等） |
| `LLM_INPUT_PRICE_MICRO_PER_1K` / `LLM_OUTPUT_PRICE_MICRO_PER_1K` | 每 1000 token 的单价（单位：微元=1e-6 元）。不填=未定价：成本记 0 但**在观测页标注"未计入"**，不是免费 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | 知识库嵌入模型与维度（默认 `qwen3.7-text-embedding` / 1024，以 `.env.example` 为准）。**换模型需要重建知识库** |
| `REDIS_URL` | 限流与预算的集中式计数（compose 内为 `redis://redis:6379/0`） |
| `PG_DSN` | PostgreSQL/ParadeDB 连接串（pgvector dense + BM25 + LangGraph checkpoint；compose 内由 `PG_*` 拼出） |
| `KB_BM25_TOKENIZER` | PG BM25 中文分词器：`jieba`（默认）/ `chinese_lindera` / `chinese_compatible` |
| `CHAT_CHECKPOINT_ENABLED` | `1` 时在线问答使用 PostgreSQL checkpoint；未配 PG 时不启用（已落库消息仍可读） |
| `JUDGE_MODEL` / `JUDGE_API_KEY` / `JUDGE_BASE_URL` | 可选跨族裁判；未配置时自检复核回退 `AGENT` |
| `CHAT_CONTEXT_MAX_CHARS` | 问答上下文装配预算（system 与当前问题永远保留） |

2026-09-25 根据官方模型文档选择 Qwen3.8-Max 的 `0902` 快照及 Qwen-Audio-3.1-ASR-Flash-Filetrans。前者支持严格 JSON Schema，后者支持长录音与说话人分离，HTTP 请求及结果结构兼容项目已有适配器。模型权限以实际 API 调用结果为准。

更新 `.env` 后，在没有评测或模型任务运行时执行：

```bash
./scripts/compose.sh up -d --force-recreate backend
```

容器启动脚本将配置写入项目已有模型配置表（完整密钥仅供后端读取）；空密钥不会触发模型调用。`.env` 是本地模型配置与管理员密码的启动来源，重启会重新应用非空配置。

模型服务的结构化输出支持和地域说明可参考[百炼结构化输出文档](https://help.aliyun.com/en/model-studio/qwen-structured-output)。

## 验证

```bash
./scripts/compose.sh exec -T backend python -m unittest discover -s tests -v   # 产品 629 项
make -C evaluation test && make -C evaluation smoke                            # 评测 330 项 + 零调用回归
cd meetingbot/vue && npm ci && npm run type-check && npm run lint && npm run test   # 前端 65 项
```

**新增表怎么进已有库**：`chat_session` / `chat_message`、KB 三表、`kb_index_outbox`、
系列/记忆/待办四表（`meeting_series` / `meeting_memory` / `action_item` /
`action_item_event`）、模型路由与在线评测三表（`model_route` / `online_eval_run` /
`online_eval_case`）以及后续增量列都由 aerich 迁移链管理（0–9 号）。

- **空库 / 已经由当前 `db/01-schema.sql` 建好的库**：不要再从 0 号重放迁移
  （会在 2 号报 `Duplicate column`）。用带结构校验的登记脚本：

```bash
cd meetingbot/fastapi-app
.venv/bin/python ../../scripts/stamp_aerich.py
```

  脚本会先检查当前结构的关键表/列；不是当前终态就拒绝 `--fake`。随后创建
  `aerich` 表并登记 0–9 号版本，重复执行是幂等的。

- **从旧版 schema.sql 升级的库**：先备份，按实际已有列手工执行缺少的增量；
  脚本检测到缺列会直接退出，不会把旧库伪装成最新。增量补齐后再跑登记脚本。
- **迁移链本身**由 `tests/test_schema_sync.py` 重放 0–9 号并与 schema.sql 逐列比对；
  空库 / 当前结构两条路径的终点必须一致。

现有业务测试使用内存 SQLite 和 Mock 模型，包含实际 FFmpeg 格式处理，不需要付费 API。
质量评测入口、固定样本、预算与指标口径见 `evaluation/README.md`。默认 dry-run 不调用模型；live 必须显式启用。
本次授权上限是 30 次 Qwen 调用和 2 次录音转写。新增能力的付费预算与预注册见
`evaluation/LEDGER.md`（总上限 ≤300 次调用；在线评测手动触发、单次 ≤50 条判定）。调用数不是金额硬上限，不应在评测期间重启后端或同时手动触发评测任务。
合成样本的参考要点规则评分、模型自评分、真实录音转写运行结果分别报告；没有人工逐字稿时不报告 ASR 字错率。

## 停止与日志

```bash
./scripts/compose.sh stop
./scripts/compose.sh logs --tail=100 backend
```

停止容器保留数据。不要使用 `down -v`，除非确实要删除本项目数据库。

## 知识库 / 问答 / 可观测（2026-10-16 新增）

| 子系统 | 关键开关 | 排障入口 |
|---|---|---|
| 知识库 | `EMBEDDING_MODEL`、`EMBEDDING_DIM` | `GET /kb/status`：PG 索引可达性、嵌入配置、文档/切片/待处理任务计数 |
| 问答 | `CHAT_MAX_ROUNDS`、`CHAT_MAX_TOKENS`、`CHAT_HISTORY_WINDOW`、`CHAT_SUMMARY_THRESHOLD` | `GET /chat/status`：模型是否配好、供应商是否声明支持工具调用、PG 检索索引与嵌入是否就绪、**内容审核是否启用** |
| 限流与预算 | `CHAT_ASK_LIMIT_PER_USER`、`CHAT_ASK_LIMIT_PER_IP`、`CHAT_ASK_WINDOW_SECONDS`、`CHAT_DAILY_TOKEN_BUDGET` | 被拒时返回 429 并带 `X-RateLimit-*` / `Retry-After`；`common/ratelimit.py` 的 fail-open 会记 ERROR |
| 内容审核 | `MODERATION_WORDS` 或 `MODERATION_WORDS_FILE`、`MODERATION_OUTPUT_ACTION`(`flag`/`redact`/`block`) | 命中写 WARNING 日志；留痕存 `chat_message.moderation_json`（只记词与位置，不含上下文） |
| 可观测 | `LOG_FORMAT`(默认 json)、`LOG_LEVEL`、`OTEL_EXPORTER_OTLP_ENDPOINT`（未设=不上报）、`OTEL_SERVICE_NAME`、`METRICS_TOKEN` | `/metrics`（Prometheus 文本）；设了 `METRICS_TOKEN` 后采集器要带 `x-metrics-token` |
| 长任务并发 | `TEMPORAL_MAX_CONCURRENCY_<类型>`（`TRANSCRIPTION`/`MINUTES`/`AGENT_RUN`/`KB_INDEX`） | 每类一个队列；升级时另起兼容 worker 消费旧队列，在途任务不会卡住 |

**MCP 端点**：`POST /mcp`（streamable HTTP，stateless）。客户端需带 JWT
（`Authorization: Bearer <token>` 或本产品的 `token` 头）。`MCP_ALLOWED_HOSTS`
（逗号分隔）用于 Host 白名单：**不设时关闭 DNS 重绑定防护**——库的默认白名单只允许
回环地址，容器部署下会让每个请求 421（实测确认）。工具每个都要 JWT 才能取到数据，
所以默认关在安全上可接受；要开就把它设成实际域名。

**问答是个花钱的入口**：一次提问会触发 2–7 次模型调用（检索嵌入 + 多轮生成），
实测单轮端到端 **12–17 秒**、模型侧 4.6–16.7 秒（本机单副本，2 并发无退化）。
对外部署务必设 `CHAT_DAILY_TOKEN_BUDGET` 与频率上限，并给模型配置填单价——
否则观测页只能显示"未定价"，成本无从判断。

## 表结构变更（迁移）

**结构真源是 `meetingbot/fastapi-app/models.py`**；空库初始化走的是一份 SQL 快照
（`db/01-schema.sql`），两者必须一致——有零调用的漂移测试兜底
（`tests/test_schema_sync.py`，逐表比对列名、可空性、类型口径与时间默认值精度）。

改表的流程：

```bash
cd meetingbot/fastapi-app

# 1. 改 models.py
# 2. 生成迁移（连的是目标库，只读它的当前结构）
MYSQL_HOST=127.0.0.1 MYSQL_PORT=13306 MYSQL_USER=root MYSQL_PASSWORD=*** \
MYSQL_DATABASE=meetingbot JWT_SECRET=x .venv/bin/python -m aerich migrate --name 加了什么
# 3. 应用
…同上环境变量… .venv/bin/python -m aerich upgrade
# 4. **把这次变更同步进 db/01-schema.sql**（保证空库一次初始化就是最新结构）
# 5. 跑一次测试，漂移测试会检查第 4 步有没有漏
python -m unittest discover -s tests -p 'test_*.py'
```

几条要点：

- **基线迁移是幂等的**：`migrations/models/0_*_init.py` 用 `CREATE TABLE IF NOT EXISTS`，
  所以对"已经由 initdb SQL 建好结构的库"跑 `aerich upgrade` 不会报错，
  只在库里补一张 `aerich` 版本表。新环境和存量环境因此走同一条升级路径。
- **两条建库路径的取舍**：initdb 的 `db/01-schema.sql` 是**权威结构**（带列注释、
  显式字符集与排序规则）；aerich 从模型生成的 DDL 没有那些注释。所以
  **空库统一走 initdb**，aerich 只负责增量变更。别反过来用 aerich 建空库——
  能建，但会丢掉列注释这层文档。
- **`generate_schemas` 只在测试里用**（内存 sqlite）。产品运行时不允许它建表。
- **本轮的迁移链**：`0_*_init`（基线，含 KB 三表；IF NOT EXISTS）、
  `1_*_chat_session`（问答两张表）、`2_*_cost_tracking`、`3_*_query_plan`、
  `4_*_contextual_retrieval`、`5_*_recording_time`、`6_*_agent_runtime`（outbox +
  checkpoint/trace 列）、`7_*_runtime_cleanup`（条件删除 write-only 的 `last_event_id`）。
  增量迁移用普通 ALTER 而不是 IF NOT EXISTS：MySQL 的 ADD COLUMN 不支持它，
  而 aerich 的版本表保证每条迁移只执行一次。
- 改结构时记得同步三处：模型、`db/01-schema.sql`、迁移文件。漏了第一处或第二处
  会被测试拦下，漏了第三处会在别的环境上表现为"结构没跟上"。

## 本地测试环境（不依赖 Docker）

产品测试此前需要 `tortoise` 等依赖，缺依赖时测试模块在**导入阶段**就失败（表现为
"Ran N tests, errors"，看起来像测试坏了，实际是环境没装）。已建好隔离虚拟环境：

```bash
cd meetingbot/fastapi-app
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

当前实测：**收集 629 项，623 项通过，6 项条件跳过**（5 个 PG 实库测试无 `PG_TEST_DSN`，1 个评测产物不在本工作区；CI 的 `postgres-integration` 作业会执行前五个）。
评测侧 330 项、前端 65 项，命令见下文「验证」一节；数字以 CI 实跑为准。
其中 74 项是契约层测试（`tests/test_contracts.py`），只依赖 pydantic、不碰数据库。
数字以 CI（`.github/workflows/ci.yml` 的 backend 作业）每次实跑为准。

离线指标套件有两种跑法：

```bash
# 仅用共享文本/时间判据（纯 stdlib，系统 python 即可）
python evaluation/regression/run.py --set all

# 需要契约不变量（契约层依赖 pydantic），用产品的 venv
meetingbot/fastapi-app/.venv/bin/python evaluation/regression/run.py --set all
```

两者都是**零模型调用**。第二种会多产出"契约不变量"小节；第一种会把该小节标为不可用
并说明原因，而不是退回一份近似实现——契约判定没有近似版。

`.venv` 不应提交或打进镜像（`meetingbot/fastapi-app/.dockerignore` 已排除 `.venv`）。

## 部署约束与新配置（2026-09-27 修复轮）

**多 worker 已解禁**：登录/注册限流与问答预算计数已搬到 Redis
（`common/ratelimit.py`，原子 Lua 滑动窗口），backend 不再依赖进程内计数。
Dockerfile 用 `WEB_CONCURRENCY`（默认 2）控制 uvicorn worker 数；Redis 故障时限流
fail-open（放行并记 ERROR），不会把业务打挂。转写/纪要/自检/索引的防双跑仍由
Temporal workflow id 与 activity 内 DB 条件抢占保证，backend/worker 都可多副本。

**必须设置的环境变量**：`JWT_SECRET`（缺失时后端拒绝启动；`openssl rand -hex 32` 生成）、
`PG_PASSWORD`（compose 的 PostgreSQL 初始化口令）。样例与全部可选项见
`meetingbot/fastapi-app/.env.example`（.env 不入库）。

**PostgreSQL（推荐）**：compose 的 `postgres` 用 ParadeDB PG17，一张
`kb.chunk_search` 同时承载 pgvector dense 与 ParadeDB BM25，`langgraph` schema 承载
checkpoint。dense/BM25 都是派生索引，丢了可从 MySQL 的 `kb_chunk` 重建；checkpoint
丢了只影响在途对话续跑，不影响已落库消息。PG 不可用时检索不可用，`/chat/status`
报告 `pg_kb_reachable=false`；不再有第二个向量库。`kb.chunk_search` 的
`embedding` 列 / HNSW 索引 / BM25 索引由 `services/pg_kb.ensure_schema` 在启动时
幂等创建；升级后先启动一次让 DDL 生效，再对三个来源重新索引以补向量。
生产建议配 `PG_DSN` + `CHAT_CHECKPOINT_ENABLED=1`；未启用 checkpoint 时断线不能续跑。
`paradedb/pg_search` 为 AGPL-3.0，分发/商用前需确认许可证口径。

**新增开关**：`REGISTER_ENABLED` 默认关闭，设为 `true` 才开放自助注册；
`CORS_ALLOW_ORIGINS` 留空表示同源、不允许跨域，跨域时写逗号分隔来源；`LOGIN_RATE_MAX_FAILURES/LOGIN_RATE_WINDOW_SECONDS`
登录限流参数。

**索引**：存量库执行 `docker exec -i meetingbot-local-db-1 mysql -umeetingbot -p$MYSQL_PASSWORD
meetingbot < scripts/indexes.sql`（重复执行报 Duplicate key name 可忽略）。

**department 表的遗留列已清掉**：原结构里有 `code`、`parent_id`、`leader_id`、`sort`
四列，全仓（后端 / 前端 / 评测 / 脚本）没有任何读写的代码，`code` 一列自己的注释
还写着"ORM 不使用，仅历史数据保留"。已从 `db/01-schema.sql` 与种子数据里移除，
让表结构与 `models.py` 严格一致（漂移测试会强制这一点）。

存量库如果也想清掉：

```sql
ALTER TABLE department
  DROP INDEX idx_department_parent_id,
  DROP INDEX idx_department_status_sort,
  DROP COLUMN code, DROP COLUMN parent_id, DROP COLUMN leader_id, DROP COLUMN sort,
  ADD INDEX idx_department_status (status);
-- 索引不存在时会报错，可忽略；不清也不影响功能，只是结构比模型多四列
```

**纪要调用缓存目录**：`files/minutes_cache/` 为重试去重缓存（派生数据，
可随时清空，代价是下次重试多付一次调用）。

**密钥泄露处置**：.env 曾被误提交入 git。若仓库曾离开本机，必须到供应商控制台轮换
DASHSCOPE/LLM/MODELSCOPE 密钥与 MySQL/JWT 口令——历史剥离不能替代轮换。

## 新增配置（均为可选，不填则行为与改造前一致）

| 变量 | 作用 |
|---|---|
| `CHEAP_MODEL` / `CHEAP_API_KEY` / `CHEAP_BASE_URL` | 成本路由目标；不填不建 CHEAP 配置，`model_route` 为空时所有调用走原用途 |
| `CHEAP_INPUT_PRICE_MICRO_PER_1K` / `CHEAP_OUTPUT_PRICE_MICRO_PER_1K` | CHEAP 单价；0 表示未定价（成本记 0，观测页单独计数） |
| `JUDGE_MODEL` / `JUDGE_*` | 在线评测与离线判定使用；未配置时回退 AGENT 并在结果里如实标注 |

启用顺序：先建 CHEAP 模型配置 → 在「AI模型配置 → 模型路由」加路由（可只开
`SPEAKER`/`CHAT_SUMMARY`/`KB_QUERY_PLAN`）→ 看成本与缓存看板 → 质量非劣后再扩到
`MINUTES`。结构化输出（纪要/自检/规划/摘要）的路由目标必须支持 strict JSON Schema，
启动时会校验并在不满足时拒绝启动。
