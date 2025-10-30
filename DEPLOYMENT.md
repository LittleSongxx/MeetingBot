# AIMeeting 本地 Docker 部署

配置文件为 `ai_meeting/fastapi-app/.env`。密钥不进入镜像，也不提交到版本控制。

## 启动

在项目根目录运行：

```bash
./scripts/compose.sh up -d --build
./scripts/compose.sh ps
```

网页：<http://127.0.0.1:18082>。后端健康检查：<http://127.0.0.1:19091/health>。
登录账号和密码分别查看 `.env` 的 `APP_ADMIN_USERNAME`、`APP_ADMIN_PASSWORD`，本次已生成独立本地密码。

`scripts/compose.sh` 统一从指定位置读取 `.env`，无需把密钥放到项目根目录。
默认使用单个后端 worker，符合项目进程内任务调度方式。MySQL 仅在 Compose 网络内开放，网页及后端仅绑定宿主机回环地址。
数据保存在本项目独立的 `aimeeting-local_mysql_data` 卷；SQL 样例只在空卷初始化时导入。上传文件保存在原项目 `fastapi-app/files` 目录。

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
| `LLM_MAX_TOKENS` | 单次文本输出上限，本轮为 `4096` |

2026-09-25 根据官方模型文档选择 Qwen3.8-Max 的 `0902` 快照及 Qwen-Audio-3.1-ASR-Flash-Filetrans。前者支持严格 JSON Schema，后者支持长录音与说话人分离，HTTP 请求及结果结构兼容项目已有适配器。模型权限以实际 API 调用结果为准。

更新 `.env` 后，在没有评测或模型任务运行时执行：

```bash
./scripts/compose.sh up -d --force-recreate backend
```

容器启动脚本将配置写入项目已有模型配置表（完整密钥仅供后端读取）；空密钥不会触发模型调用。`.env` 是本地模型配置与管理员密码的启动来源，重启会重新应用非空配置。

模型服务的结构化输出支持和地域说明可参考[百炼结构化输出文档](https://help.aliyun.com/en/model-studio/qwen-structured-output)。

## 验证

```bash
./scripts/compose.sh exec -T backend python -m unittest discover -s tests -v
```

现有业务测试使用内存 SQLite 和 Mock 模型，包含实际 FFmpeg 格式处理，不需要付费 API。
质量评测入口、固定样本、预算与指标口径见 `evaluation/README.md`。默认 dry-run 不调用模型；live 必须显式启用。
本次授权上限是 30 次 Qwen 调用和 2 次录音转写。调用数不是金额硬上限，不应在评测期间重启后端或同时手动触发评测任务。
合成样本的参考要点规则评分、模型自评分、真实录音转写运行结果分别报告；没有人工逐字稿时不报告 ASR 字错率。

## 停止与日志

```bash
./scripts/compose.sh stop
./scripts/compose.sh logs --tail=100 backend
```

停止容器保留数据。不要使用 `down -v`，除非确实要删除本项目数据库。

## 本地测试环境（不依赖 Docker）

产品测试此前需要 `tortoise` 等依赖，缺依赖时测试模块在**导入阶段**就失败（表现为
"Ran N tests, errors"，看起来像测试坏了，实际是环境没装）。已建好隔离虚拟环境：

```bash
cd ai_meeting/fastapi-app
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

当前实测：**87 项通过，1 项跳过**（缺 ffmpeg 时的条件跳过）。其中 36 项是契约层测试
（`tests/test_contracts.py`），只依赖 pydantic、不碰数据库。

离线指标套件有两种跑法：

```bash
# 仅用共享文本/时间判据（纯 stdlib，系统 python 即可）
python evaluation/regression/run.py --set all

# 需要契约不变量（契约层依赖 pydantic），用产品的 venv
ai_meeting/fastapi-app/.venv/bin/python evaluation/regression/run.py --set all
```

两者都是**零模型调用**。第二种会多产出"契约不变量"小节；第一种会把该小节标为不可用
并说明原因，而不是退回一份近似实现——契约判定没有近似版。

`.venv` 不应提交或打进镜像（`ai_meeting/fastapi-app/.dockerignore` 已排除 `.venv`）。

## 部署约束与新配置（2026-09-27 修复轮）

**单进程约束**：backend 必须以 `--workers 1` 运行（Dockerfile 已钉住）。转写/纪要/自检的
防双跑依赖进程内字典（`_running_tasks` 等）与条件更新；多 worker/多副本会双跑双计费。
分布式部署前需先引入集中式锁或任务队列。

**必须设置的环境变量**：`JWT_SECRET`（缺失时后端拒绝启动；`openssl rand -hex 32` 生成）。
样例与全部可选项见 `ai_meeting/fastapi-app/.env.example`（.env 不入库）。

**新增开关**：`REGISTER_ENABLED=false` 关闭自助注册（对外部署建议）；
`CORS_ALLOW_ORIGINS` 收紧跨域来源；`LOGIN_RATE_MAX_FAILURES/LOGIN_RATE_WINDOW_SECONDS`
登录限流参数。

**索引**：存量库执行 `docker exec -i aimeeting-local-db-1 mysql -uaimeeting -p$MYSQL_PASSWORD
ai_meeting < scripts/indexes.sql`（重复执行报 Duplicate key name 可忽略）。

**department 表**：`ai_meeting.sql` 已把 `code` 列改为可空并移除其唯一索引（ORM 不使用
该列，原 NOT NULL 会让新初始化的库在建部门时报错）。只影响空卷初始化；存量库无需处理，
如需对齐可执行 `ALTER TABLE department MODIFY code varchar(50) NULL DEFAULT NULL, DROP INDEX uk_department_code;`（后者已不存在时会报错，可忽略）。

**纪要调用缓存目录**：`files/minutes_cache/` 为重试去重缓存（派生数据，
可随时清空，代价是下次重试多付一次调用）。

**密钥泄露处置**：.env 曾被误提交入 git。若仓库曾离开本机，必须到供应商控制台轮换
DASHSCOPE/LLM/MODELSCOPE 密钥与 MySQL/JWT 口令——历史剥离不能替代轮换。
