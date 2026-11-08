# 部署与运维（本地 Docker）

本文档覆盖本地 Docker 部署：前置条件、`.env` 变量表、启动/停止/日志/重建、
首次初始化、验证清单、单进程限制、故障排查。

> 部署资产：`compose.yaml`（项目名 `meetingbot-aim-local`）、`db/Dockerfile`、
> `meetingbot/fastapi-app/Dockerfile`、`meetingbot/vue/Dockerfile`、
> `scripts/compose.sh`（Linux/macOS/WSL）与 `scripts/compose.ps1`（Windows）。

## 前置条件

- **Docker Desktop**（Windows/macOS）或 Docker Engine + Compose v2（Linux）；
  本项目用 Compose 的 `depends_on.condition`、profile 与 `env_file` 长语法，需要 Compose v2.24 以上；
- 宿主机**不需要**安装 MySQL 或中文字体：数据库在 `db` 容器里，后端镜像已带 FFmpeg 与
  `fonts-wqy-zenhei`（TrueType；镜像里链接为源码探测的 Noto 路径，Word/PDF 导出用）；
- 首次构建需要拉取 `python:3.12-slim`、`mysql:8.4.11`、`node:22-alpine`、`nginx:1.30.5-alpine` 等基础镜像，需要网络；
- 常用端口 `19092`（后端）与 `18083`（网页）未被占用；两者都只绑定 `127.0.0.1`。

## 快速启动

```bash
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
# 编辑 .env：至少填写 MYSQL_PASSWORD、MYSQL_ROOT_PASSWORD、JWT_SECRET

docker compose up -d --build
# 或走封装脚本（自动定位仓库根目录与 .env）：
#   ./scripts/compose.sh up -d --build
#   .\scripts\compose.ps1 up -d --build
```

启动后：

- 网页：`http://127.0.0.1:18083`
- 后端（直连调试）：`http://127.0.0.1:19092`
- 演示账号：`admin/admin`、`employee/123456`、`operator/123456`（明文口令，仅本地演示）。

## `.env` 变量表

| 变量 | 必填 | 说明 |
|---|---|---|
| `MYSQL_PASSWORD` | 是 | 应用账号口令（空数据卷首次初始化时生效） |
| `MYSQL_ROOT_PASSWORD` | 是 | MySQL root 口令，仅本机 compose 使用 |
| `MYSQL_DATABASE` | 否 | 默认 `ai_meeting` |
| `MYSQL_USER` | 否 | 默认 `meetingbot` |
| `MYSQL_HOST` / `MYSQL_PORT` | 否 | 本机开发默认 `localhost:3306`；compose 内后端固定为 `db:3306` |
| `SQL_ECHO` | 否 | 是否打印 SQL 日志，默认 `false` |
| `JWT_SECRET` | 是 | JWT 签名密钥（留空则用源码默认值，生产必须覆盖） |
| `JWT_EXPIRE_HOURS` | 否 | Token 有效期，默认 24 |
| `TZ` | 否 | 容器时区，默认 `Asia/Shanghai` |
| `BACKEND_PORT` / `WEB_PORT` | 否 | 宿主机端口，默认 `19092` / `18083` |

`.env` 已被 `.gitignore` 忽略；`.env.example` 只有占位值。

## 首次初始化

数据库结构由 `db` 镜像打进 `/docker-entrypoint-initdb.d/`，**仅在 `mysql_data` 卷为空时执行一次**：

1. `01-schema.sql`：建 13 张表；
2. `02-seed.sql`：插入 2 个部门、3 个演示账号、1 场会议、4 条空 Key 的模型配置、5 条 Prompt 模板。

初始化完成后，四类模型配置的 API Key 为空，需要在网页「AI模型配置」里由管理员填写并启用
（转写 / 纪要 / 自检至少各一条，说话人匹配可选）。

## 常用操作

```bash
docker compose ps                       # 状态
docker compose logs -f backend          # 后端日志
docker compose logs -f db web           # 其它服务日志
docker compose restart backend          # 重启后端
docker compose down                     # 停止并删除容器（保留数据卷）
docker compose down -v                  # 连数据卷一起删（清库，谨慎）
docker compose up -d --build            # 改代码/SQL 后重建
```

修改初始化 SQL 后必须重建 db 镜像，并删除 `mysql_data` 卷才会重新执行（已有卷不会重放）。

## 验证清单

```bash
curl -s http://127.0.0.1:19092/ | head -c 200     # 后端根路由返回 {"code":"200",...}
curl -s -X POST http://127.0.0.1:19092/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"admin"}'
```

浏览器打开 `http://127.0.0.1:18083`，用 `admin/admin` 登录，确认：
会议列表有 1 场演示会议；「会议资料」可上传文件；「AI模型配置」可看到 4 条待填 Key 的配置。

## 单进程限制（重要）

转写、纪要生成、纪要自检三类耗时任务全部跑在 backend 进程内：
`asyncio.create_task` 起协程、进程内字典去重、FastAPI lifespan 里 `recover_*` 恢复。
没有消息队列、没有独立 worker、没有分布式锁，因此：

- backend 只能 `--workers 1`（Dockerfile 已固定）；
- backend 只能有一个副本，扩副本会让同一任务被领两次、SSE 进度只落在其中一个进程；
- 需要横向扩展时必须先把任务编排外置（MQ/worker），不是改 compose 能解决的。

## 故障排查

| 现象 | 排查方向 |
|---|---|
| `docker compose up` 报缺少变量 | `.env` 未创建或 `MYSQL_PASSWORD/MYSQL_ROOT_PASSWORD` 为空 |
| backend 启动即退出 | 看 `docker compose logs backend`；常见为 MySQL 未 healthy 或库名/口令不匹配 |
| 网页能开但接口 502 | backend 未 healthy；先 `docker compose ps` 看状态与日志 |
| 数据是旧的/缺表 | `mysql_data` 卷已存在，初始化脚本不会重放；确认后 `down -v` 重建 |
| 导出 PDF 中文乱码/失败 | 镜像已装文泉驿 TrueType 字体；自建镜像需自行安装中文字体 |
| 转写报「未找到 FFmpeg」 | 非容器方式运行需本机安装 ffmpeg 并加入 PATH |

## 说明

- 本仓库是课程/个人项目：密码明文、JWT 默认密钥、CORS 全开、无速率限制，**不要直接公网部署**；
- 演示媒体文件（`meetingbot/fastapi-app/files/`）不入库，需要时自行放入或上传；
- 根目录的 Navicat dump（`ai_meeting.sql`）含真实 API Key，不入库，也不建议使用。
