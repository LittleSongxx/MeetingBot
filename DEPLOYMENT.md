# 部署与运维（本地 Docker）

本文档覆盖本地 Docker 部署：前置条件、`.env` 变量表、启动/停止/日志/重建、
首次初始化、验证清单、测试命令、单进程限制、故障排查与回滚。

> 部署资产：`compose.yaml`（项目名 `meetingbot-aim-local`）、`db/Dockerfile`、
> `meetingbot/fastapi-app/Dockerfile`、`meetingbot/vue/Dockerfile`、
> `scripts/compose.sh`（Linux/macOS/WSL）与 `scripts/compose.ps1`（Windows）。

## 前置条件

- **Docker Desktop**（Windows/macOS）或 Docker Engine + Compose v2（Linux）；
  本项目用 Compose 的 `depends_on.condition: service_healthy`、profile 与
  `env_file` 长语法，需要 Compose v2.24 以上。
- 宿主机 **不需要** 安装 MySQL、FFmpeg 或中文字体：数据库在 `db` 容器里，
  FFmpeg 与 `fonts-wqy-zenhei` 已打进后端镜像。
- 首次构建需要拉取 `python:3.12-slim`、`mysql:8.4.11`、`node:22-alpine`、
  `nginx:1.30.5-alpine3.24` 等基础镜像，需要网络。
- 常用端口 `19092`（后端）与 `18083`（网页）未被占用；只绑定 `127.0.0.1`。

## 快速启动

```bash
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
# 编辑 .env，至少填写 MYSQL_PASSWORD、MYSQL_ROOT_PASSWORD、JWT_SECRET、APP_ADMIN_PASSWORD，
# 以及使用 AI 功能所需的 DASHSCOPE_API_KEY

docker compose up -d --build
# 也可以走封装脚本（自动定位仓库根目录与 .env）：
#   ./scripts/compose.sh up -d --build          # Linux / macOS / WSL
#   .\scripts\compose.ps1 up -d --build         # Windows PowerShell
./scripts/compose.sh ps
```

首次启动会依次完成：

1. `db` 容器初始化空数据卷，执行 `db/01-schema.sql`（13 张表）→
   `db/02-seed.sql`（2 个部门 + prompt 模板），健康检查通过后启动 `backend`；
2. `backend` 启动脚本 `bootstrap.py`：创建管理员账号（`APP_ADMIN_USERNAME` /
   `APP_ADMIN_PASSWORD`）、把历史明文口令迁移为哈希、按环境变量写入 4 条模型配置
   （转写 / 纪要 / 自检 / 说话人匹配）；`SEED_DEMO=1` 时生成一份演示会议数据；
3. `web` 在 backend 健康后启动，nginx 托管前端并反代 `/api/`、`/files/`。

访问：

- 网页：<http://127.0.0.1:18083>
- 后端健康：<http://127.0.0.1:19092/health>
- 前端健康（nginx）：<http://127.0.0.1:18083/health>

管理员账号见 `.env` 的 `APP_ADMIN_USERNAME` / `APP_ADMIN_PASSWORD`。
**`APP_ADMIN_PASSWORD` 不要使用示例值**。注意 `bootstrap.py` 每次容器启动都会按
`.env` 把该管理员账号的口令、角色、状态重设一遍（幂等执行）：在页面改过管理员
口令后，如果 `.env` 里还是旧值，下次重启会被改回去——要么同步更新 `.env`，
要么固定用 `.env` 作为管理员口令的来源。

## 端口与访问地址

| 服务 | 容器内端口 | 宿主机绑定 | 说明 |
|---|---|---|---|
| web（nginx） | 80 | `127.0.0.1:${WEB_PORT:-18083}` | 唯一需要访问的入口；`/api/`→`backend:9090/`，`/files/`→`backend:9090/files/` |
| backend（uvicorn） | 9090 | `127.0.0.1:${BACKEND_PORT:-19092}` | 仅用于健康检查与调试，不对外暴露 |
| db（MySQL） | 3306 | 不映射 | 只在 compose 网络内由 backend 访问 |

前端生产构建在镜像内注入 `VITE_BASE_URL=/api`（Dockerfile 的构建参数默认值），
浏览器只访问 18083 一个源，属于同源部署，不需要跨域配置。

## .env 变量表

变量统一放在仓库根 `.env`：compose 用它做端口/口令插值，backend 容器通过
`env_file: .env` 读取全部变量。`.env` 已被 `.gitignore` 忽略，**不要提交**。

### MySQL

| 变量 | 含义 | 示例 | 必填 |
|---|---|---|---|
| `MYSQL_HOST` | 后端连接 MySQL 的主机；compose 内固定覆盖为 `db` | `db` | 否（默认 `db`） |
| `MYSQL_PORT` | MySQL 端口；compose 内固定覆盖为 `3306` | `3306` | 否 |
| `MYSQL_DATABASE` | 数据库名 | `ai_meeting` | 否（默认 `ai_meeting`） |
| `MYSQL_USER` | 应用账号 | `meetingbot` | 否（默认 `meetingbot`） |
| `MYSQL_PASSWORD` | 应用账号口令，空卷首次初始化时生效 | `change-me` | **是** |
| `MYSQL_ROOT_PASSWORD` | root 口令，仅 compose 内使用 | `change-me-root` | **是** |
| `SQL_ECHO` | 是否打印 ORM SQL（排查用） | `false` | 否（默认 `false`） |

### 认证

| 变量 | 含义 | 示例 | 必填 |
|---|---|---|---|
| `JWT_SECRET` | JWT 签名密钥；**无默认值**，缺失时后端拒绝启动；换值会使已签发 Token 全部失效 | `openssl rand -hex 32` 的输出 | **是** |
| `JWT_EXPIRE_HOURS` | Token 有效期（小时） | `24` | 否（默认 `24`） |
| `APP_ADMIN_USERNAME` | bootstrap 创建的管理员账号 | `admin` | 否（默认 `admin`） |
| `APP_ADMIN_PASSWORD` | 管理员口令；**仅当库中口令与它不一致时才更新**（避免每次重启刷新 bcrypt 盐、让已登录 token 全部失效） | 强口令 | **是**（首次启动） |
| `REGISTER_ENABLED` | 是否开放注册接口 | `false` | 否（默认 `false`） |
| `CORS_ALLOW_ORIGINS` | 允许的跨域来源，逗号分隔；**留空=同源部署不放行跨域** | 空 / `http://localhost:5173` | 否 |

### 模型服务

`bootstrap.py` 只在**首次部署**时按这组变量创建 `ai_model_config`（4 条配置：
转写 / 纪要 / 自检 / 说话人匹配）；配置已存在时以「AI 模型配置」页面为准，
重启不会覆盖页面里改过的 Key / 地址 / 模型名。只有库里 Key 为空、而 `.env`
提供了 Key 时，才会用 `.env` 补齐并启用。真实 Key 只存在于本机 `.env` 与
数据卷，不进镜像、不进仓库。

| 变量 | 含义 | 示例 | 必填 |
|---|---|---|---|
| `DASHSCOPE_API_KEY` | 百炼 API Key；留空时服务能启动、页面能浏览，但 AI 调用失败 | `sk-...` | 使用 AI 功能时必填 |
| `DASHSCOPE_BASE_URL` | 录音文件识别（ASR）服务地址 | `https://dashscope.aliyuncs.com/api/v1` | 否 |
| `LLM_BASE_URL` | 文本模型 OpenAI 兼容地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 否 |
| `LLM_MODEL` | 文本任务默认模型（纪要 / 自检 / 说话人匹配） | `qwen-plus` | 否 |
| `ASR_MODEL` | 录音文件转写模型 | `paraformer-v2` | 否 |
| `LLM_TIMEOUT_SECONDS` | 文本模型单次请求超时（秒） | `600` | 否 |
| `ASR_TIMEOUT_SECONDS` | ASR 请求超时（秒） | `600` | 否 |

### 行为参数

| 变量 | 含义 | 示例 | 必填 |
|---|---|---|---|
| `MAX_CHUNK_CHARS` | 纪要生成单次送给模型的最大转写字符数 | `12000` | 否 |
| `AGENT_REVIEW_MAX_TRANSCRIPT_CHARS` | 自检审查最多送入的转写字符数（超出截断） | `20000` | 否 |
| `CHUNK_SECONDS` | 转写时 FFmpeg 单个音频分片时长（秒） | `1200` | 否 |
| `LOG_LEVEL` | 日志级别 | `INFO` | 否（默认 `INFO`） |
| `LOG_FORMAT` | 日志格式：`text` 便于本地查看，`json` 便于采集 | `text` | 否（`.env.example` 默认 `text`） |
| `SEED_DEMO` | `1`=启动时幂等写入演示会议/AI 链路数据；`0`=不生成（**生产保持 0**） | `0` | 否（`.env.example` 默认 `0`） |
| `DEMO_PASSWORD` | `SEED_DEMO=1` 时演示账号使用的口令 | `change-me` | 否 |
| `DOCS_ENABLED` | 是否暴露 `/docs`、`/redoc`、`/openapi.json` | `0` | 否（默认 `0`） |
| `AI_CALL_LOG_RETENTION_DAYS` | AI 调用日志保留天数，启动时清理更早记录；`0`=不清理 | `365` | 否（默认 `365`） |
| `TZ` | 容器时区（页面时间均为东八区） | `Asia/Shanghai` | 否（默认 `Asia/Shanghai`） |
| `BACKEND_PORT` | 宿主机后端映射端口（只绑 `127.0.0.1`） | `19092` | 否（默认 `19092`） |
| `WEB_PORT` | 宿主机网页映射端口（只绑 `127.0.0.1`） | `18083` | 否（默认 `18083`） |

## 启动 / 停止 / 日志 / 重建

以下命令二选一：直接用 `docker compose ...`，或用封装脚本（自动带
`--project-directory` 与 `--env-file .env`）。

```bash
# 启动 / 更新（改了代码或 Dockerfile 后）
./scripts/compose.sh up -d --build
.\scripts\compose.ps1 up -d --build

# 查看状态与进程
./scripts/compose.sh ps
./scripts/compose.sh top

# 只重启后端（改了 .env 里的模型 Key 等变量后）
./scripts/compose.sh up -d --force-recreate backend

# 日志
./scripts/compose.sh logs -f --tail=100 backend
./scripts/compose.sh logs --tail=200 db
./scripts/compose.sh logs --tail=100 web

# 停止（保留容器、数据卷）
./scripts/compose.sh stop

# 停止并删除容器（保留命名卷，数据不丢）
./scripts/compose.sh down

# 危险：连数据卷一起删除（清空数据库与上传文件），只在确实要重来时用
./scripts/compose.sh down -v

# 进入容器排查
./scripts/compose.sh exec backend sh
./scripts/compose.sh exec db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SHOW TABLES"'
```

## 验证清单

```bash
# 0. 静态校验 compose 编排：只做插值与语法检查，不构建、不启动容器。
#    用 .env.example 提供插值，不需要先创建 .env（真实运行仍然必须创建 .env）。
docker compose --project-directory . --env-file .env.example -f compose.yaml config -q

# 1. 三个服务都在运行，db/backend 应为 healthy
docker compose ps

# 2. 健康检查
curl -fsS http://127.0.0.1:19092/health        # 后端
curl -fsS http://127.0.0.1:18083/health        # 前端 nginx

# 3. 首页可访问（返回 HTML）
curl -fsS http://127.0.0.1:18083/ | head -n 5

# 4. 登录（code=200，data.token 非空）
curl -s -X POST http://127.0.0.1:18083/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<APP_ADMIN_PASSWORD>"}'

# 5. 带 token 访问业务接口（部门列表应有 2 条种子数据）
curl -s http://127.0.0.1:18083/api/department/selectAll \
  -H 'token: <上一步返回的 token>'

# 6. 数据库：13 张表、5 条 prompt 模板、2 个部门
docker compose exec -T db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -Nse \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE();
   SELECT COUNT(*) FROM prompt_template; SELECT COUNT(*) FROM department;"'
```

若第 1~3 步通过但登录失败，先看 `./scripts/compose.sh logs backend`：
`JWT_SECRET` 缺失会让后端启动即失败；`APP_ADMIN_PASSWORD` 为空则没有可登录的管理员。

## 测试

后端单测使用 sqlite 内存库与 mock 模型，不需要 MySQL、不调用真实模型：

```bash
# 在镜像的 test 阶段里跑（环境与生产镜像一致）
docker compose --profile test run --rm test

# 或本地
cd meetingbot/fastapi-app
pip install -r requirements-dev.txt
python -m unittest discover -s tests -p 'test_*.py' -v
ruff check .
```

前端：

```bash
cd meetingbot/vue
npm ci
npm run build
```

## 单进程限制（重要）

转写、纪要生成、纪要自检三类耗时任务全部跑在 **backend 进程内**：

- 用 `asyncio.create_task` 起后台协程，进程内字典去重，`FastAPI lifespan`
  在服务对外提供接口前用 `recover_*` 把中断的任务重新入队；
- 没有消息队列、没有独立 worker、没有分布式锁；任务进度就是数据库状态列，
  前端轮询（自检用 SSE）。

因此：

1. backend 必须 `--workers 1`（`meetingbot/fastapi-app/Dockerfile` 已固定），
   不要通过环境变量加 worker；
2. backend **只能有一个副本**，`docker compose up --scale backend=2` 会让同一个
   任务被两个进程同时领取，进度/SSE 也会落在错误的进程；
3. 需要横向扩展时，必须先把任务编排外置（消息队列 + worker + 分布式锁），
   这不是改 compose 参数能解决的问题；
4. 重启 backend 后，启动恢复逻辑会把中断的任务重新排队，但进程内 SSE 连接会断，
   页面需要刷新重连；转写/纪要任务没有分段断点，会**从头重跑**（可能重复计费）。

## 故障排查

| 现象 | 可能原因与处理 |
|---|---|
| `env file ...\.env not found` | 还没创建 `.env`。执行 `cp .env.example .env` 并填写必填项；不要用 `--env-file .env.example` 直接 `up` |
| `MYSQL_PASSWORD`/`JWT_SECRET` 报 "请在 .env 设置" | compose 插值失败：`.env` 缺这个变量或值为空 |
| 构建很慢 / 构建上下文异常大 | `docker compose build` 的上下文是 `meetingbot/fastapi-app` 等子目录；确认没有把 `.venv/`、`node_modules/` 或大文件放进这些目录，必要时先清理 |
| 拉取基础镜像失败 | 检查网络与镜像源；需要能访问 Docker Hub 拉 `python:3.12-slim`、`mysql:8.4.11`、`node:22-alpine`、`nginx:1.30.5-alpine3.24` |
| `bind: address already in use`（18083/19092） | 端口被占用：`netstat -ano \| findstr :18083`（Linux：`ss -ltnp`），或改 `.env` 的 `WEB_PORT`/`BACKEND_PORT` |
| MySQL 初始化 SQL 没执行 | 初始化只在 `mysql_data` 卷为空时执行。`./scripts/compose.sh logs db` 看 init 日志；确需重来：`down -v` 后重新 `up --build`（会清数据） |
| 中文乱码 | 初始化 SQL 依赖文件头的 `SET NAMES utf8mb4` 与库的 `utf8mb4` 配置，不要删除；已乱码的库只能重建空卷 |
| 转写报 FFmpeg 相关错误 | 镜像内已装 ffmpeg；先在容器里 `ffmpeg -version` 确认。宿主机不需要 ffmpeg |
| 登录 401 / Token 突然失效 | 改过 `JWT_SECRET`（旧 Token 全部失效）或账号被停用；重新登录 |
| AI 调用全部失败 | `DASHSCOPE_API_KEY` 为空/地域不匹配/账号无模型权限；也可在「AI 模型配置」页面检查 base_url 与模型名 |
| backend 一直 unhealthy | `./scripts/compose.sh logs backend`；常见是 `.env` 变量缺失导致启动 fail-fast，或 db 还没 healthy |

## 备份与回滚

> 下面命令以 bash / sh 为例（Linux、macOS、WSL）；Windows PowerShell 下把
> `$(date +%F)` 换成 `(Get-Date -Format yyyy-MM-dd)`，`>` 重定向照常可用。

### 备份

```bash
# 全库逻辑备份（写入宿主机当前目录）
docker compose exec -T db sh -c \
  'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --databases "$MYSQL_DATABASE"' \
  > backup-$(date +%F).sql

# 上传文件卷备份（导出为 tar）
docker run --rm -v meetingbot-aim-local_files:/data -v "$PWD":/backup alpine \
  tar czf /backup/files-$(date +%F).tar.gz -C /data .
```

### 代码回滚

数据卷不动，只回滚代码/镜像：

```bash
# 1. 回到上一个已知可用的提交（不要在回滚提交上继续开发）
git checkout <上一个可用 commit/tag>

# 2. 用旧代码重新构建并启动
./scripts/compose.sh up -d --build

# 3. 确认健康与登录
./scripts/compose.sh ps
curl -fsS http://127.0.0.1:19092/health
```

### 数据库回滚（schema 有变更时）

本项目没有迁移工具，`db/01-schema.sql` 只在空卷时执行。如果新版本改了表结构，
回滚需要 **先备份、再重建空卷、再导入备份**：

```bash
# 0. 先备份（见上）
# 1. 停容器并删除数据卷
./scripts/compose.sh down
docker volume rm meetingbot-aim-local_mysql_data

# 2. 用目标版本代码启动，让空卷重新执行 01-schema.sql / 02-seed.sql
./scripts/compose.sh up -d db
# 等 db healthy 后再起 backend
./scripts/compose.sh up -d

# 3. 导入备份（注意：备份里的结构与当前 schema 必须兼容）
docker compose exec -T db sh -c \
  'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' < backup-YYYY-MM-DD.sql
```

回滚注意事项：

- 回滚前先 `docker compose stop` 停掉业务写入，再做备份/恢复；
- `files` 命名卷里是上传的会议资料，与数据库里的 `meeting_material` 记录对应，
  回滚数据库时不要单独删除该卷；确需整卷回滚时用上面的 tar 备份恢复；
- 命名卷名带 compose 项目前缀（`meetingbot-aim-local_mysql_data`、
  `meetingbot-aim-local_files`），`docker volume ls` 可以确认实际名称；
- 回滚后 `JWT_SECRET` 若也换回旧值，期间新签发的 Token 会失效，需要重新登录。
