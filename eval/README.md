# 评测环境（与演示栈隔离）

这个目录用于**跑质量评测/模型对比**，不会污染 `meetingbot-aim-local` 演示数据：

| 项 | 演示栈 | 评测栈 |
|---|---|---|
| compose 项目名 | `meetingbot-aim-local` | `meetingbot-aim-eval` |
| 网页 | `http://127.0.0.1:18083` | `http://127.0.0.1:18182` |
| 后端 | `http://127.0.0.1:19092` | `http://127.0.0.1:19192` |
| MySQL 卷 | `meetingbot-aim-local_mysql_data` | `meetingbot-aim-eval_mysql_data` |
| 环境文件 | `.env` | `eval/.env`（gitignored） |

## 1. 准备 eval/.env

```bash
cp eval/.env.example eval/.env
```

本机已生成一份可直接使用的 `eval/.env`（包含随机数据库口令/JWT/管理员口令，并复用了主 `.env` 的 DASHSCOPE Key）。**不要提交 eval/.env**。

需要填写的项：

| 变量 | 必填 | 说明 |
|---|---|---|
| `MYSQL_PASSWORD` / `MYSQL_ROOT_PASSWORD` | 是 | 评测库口令，与演示栈不同即可 |
| `JWT_SECRET` | 是 | `openssl rand -hex 32` 的输出 |
| `APP_ADMIN_USERNAME` / `APP_ADMIN_PASSWORD` | 是 | 评测环境管理员账号 |
| `DASHSCOPE_API_KEY` | 使用 AI 时必填 | 百炼 Key；ASR 与 LLM 可共用 |
| `ASR_API_KEY` / `LLM_API_KEY` | 否 | 分开计费/限流时填写，留空共用上面的 Key |
| `DEMO_PASSWORD` | 否 | `SEED_DEMO=1` 时演示账号口令 |
| `EVAL_BACKEND_PORT` / `EVAL_WEB_PORT` | 否 | 默认 19192 / 18182 |

## 2. 模型怎么选

| 用途 | 默认 | 可选/建议 |
|---|---|---|
| 转写（含说话人分离） | `ASR_MODEL=paraformer-v2` | 这是当前链路实际验证过的模型，评测时建议固定 |
| 纪要/自检/说话人匹配 | `LLM_MODEL=qwen3.8-max-0902` | 与演示栈一致的**质量基线**；`qwen-plus` 可做低成本回归；正式对比时一次只改一个变量 |
| 模型接口 | OpenAI 兼容 `LLM_BASE_URL` | 必须支持 strict `json_schema`（纪要生成强依赖） |

## 3. 启动评测栈

在仓库根目录执行（backend/web 默认复用本地已构建镜像；db 从当前仓库构建）：

```bash
docker compose --project-directory eval --env-file eval/.env \
  -f eval/compose.eval.yaml up -d --build
```

检查：

```bash
docker compose --project-directory eval --env-file eval/.env -f eval/compose.eval.yaml ps
curl -fsS http://127.0.0.1:18182/health
curl -fsS http://127.0.0.1:19192/health
# 登录：账号/口令见 eval/.env 的 APP_ADMIN_USERNAME / APP_ADMIN_PASSWORD
```

网页：<http://127.0.0.1:18182>

> 注意：`backend/web` 使用本地镜像 tag，代码更新后先在仓库根执行
> `docker compose up -d --build backend web` 重新构建，再重启评测栈。

## 4. 跑评测

### 4.1 离线指标（零模型调用）

```bash
docker compose --project-directory eval --env-file eval/.env \
  -f eval/compose.eval.yaml exec -T backend python -m evaluation.run_eval
# 或 Windows PowerShell 包装脚本（在仓库根目录）
pwsh ./eval/run_eval.ps1
```

输出六字段覆盖率、有证据条目占比、引用合法率、AI 草稿→人工终稿修改率、golden set 决策/行动项/证据召回。详细指标定义见 `meetingbot/fastapi-app/evaluation/README.md`。

### 4.2 真实链路评测（会调用模型，产生少量费用）

建议在评测栈上完整跑一次“转写成功 → 生成纪要 → 确认 → 生成行动项 → 问答 → 评测”：

> 演示种子数据自带一条 `WAITING_CONFIRMATION` 的自检运行，会阻止重新生成纪要。
> 先 `GET /api/agent/selectPage` 找到它，再 `DELETE /api/agent/delete/{run_id}` 放弃，
> 之后才能 `POST /meetingMinutes/generate/{task_id}`。

1. 调用 `POST /meetingMinutes/generate/{task_id}`（task_id 从 `/transcription/selectPage` 拿）；
2. 轮询 `/meetingMinutes/selectById/{id}` 直到 `status=DRAFT`；
3. `PUT /meetingMinutes/confirm/{id}`；
4. `POST /actionItem/generate/{id}`（生成 DRAFT 行动项，可按需 `PUT /actionItem/confirmBatch` 确认）；
5. 可选：`POST /meetingMinutes/ask/{id}` 问一个关键问题，检查引用；
6. 再跑 4.1 的评测命令，记录本次模型/参数与指标。

首次真实评测报告：[`meetingbot/fastapi-app/evaluation/reports/2026-10-30-local.md`](../meetingbot/fastapi-app/evaluation/reports/2026-10-30-local.md)（含基线/生成后指标、问答结果、行动项重复问题与后续计划）。

### 4.3 A/B 模型对比

固定其他变量，只改 `LLM_MODEL`（或 Key/温度/分块参数），重启 backend 后重复 4.2：

```bash
# 编辑 eval/.env 里的 LLM_MODEL=qwen-plus（或换回 qwen3.8-max-0902）
docker compose --project-directory eval --env-file eval/.env \
  -f eval/compose.eval.yaml up -d --force-recreate backend
```

注意：`bootstrap` 只在库里没有配置时按 env 建模型配置；**已有配置以页面为准**，所以换模型后请在评测栈的 `AI 模型配置` 页面确认模型名，或清空评测库卷重建：

```bash
docker compose --project-directory eval --env-file eval/.env -f eval/compose.eval.yaml down -v
```

## 5. 隔离与清理

- 评测栈使用独立卷 `meetingbot-aim-eval_mysql_data` / `meetingbot-aim-eval_files`，不影响演示栈；
- 只是停止：`... down`（保留数据）；
- 需要重来（清空评测数据）：`... down -v`。

## 6. golden set 与数据积累

当前 `meetingbot/fastapi-app/evaluation/golden/demo_case.json` 只有演示会议一个用例，适合验证评测管线是否跑通，不足以支撑模型结论。要得到可信评测：

1. 收集 10~20 场真实会议（不同时长、不同会议类型）；
2. 人工标注每场的：决策、行动项（内容/负责人/截止）、关键结论对应的转写片段号；
3. 按 `evaluation/golden/*.json` 格式添加用例，再跑 4.1；
4. 每次改 Prompt/模型/分块参数，跑同一套指标，记录报告到 `evaluation/reports/`。

> 评测结论要区分“管线验证”和“质量结论”：1 个用例只能证明管线通，不能证明模型好。
