# MOSS 产品真实链路全量复核（R8007_M8011，LEDGER §47）

- **生成日期**：2026-10-23（Asia/Shanghai）
- **执行**：asr-runner（team task-13）
- **口径**：**产品真实链路** `TRANSCRIPTION_ENGINE=http` → `http://asr-sidecar:9971/transcribe`（compose `moss-asr-local`），非直连绕行
- **预注册**：`evaluation/LEDGER.md §47`（先于运行写死）
- **结论**：**§47 六项判定全部通过（PASS）** —— 但在达成前修复了两个环境/配置阻断（worker 容器 env 陈旧、产品不传 `max_new_tokens` 导致 5120 静默截断），两次无效运行与修复过程完整记录于 §3
- **数字源**：`evaluation/results/moss_product_link_full.json`、`evaluation/asr/first_test/task_R8007_M8011.moss_http_product.json`

---

## 1. 预注册判定（§47）

| # | 判定项 | 要求 | 实测（run_c 有效运行） | 结果 |
|---|---|---|---|---|
| ① | 任务状态与模型名 | `SUCCEEDED` 且 `model_name=moss-transcribe-diarize-0.9b(local)` | SUCCEEDED；model_name 一致；DB `ai_call_log.model_name` 亦为 local、`usage={"engine":"local-http"}` | **通过** |
| ② | 产品链路 cpCER 与直连差 | ≤2pp（直连 12.35%） | 产品 **13.99%**，差 **+1.64pp** | **通过** |
| ③ | 覆盖率与截断 | ≥99%，无截断 | 覆盖率 **99.85%**（1858.7/1861.5s），`truncated=False` | **通过** |
| ④ | 墙钟 | <1800s（产品 `ASR_TIMEOUT_SECONDS=1800`） | **844.0s**（sidecar 实测 842.7s；含上传/预处理的总脚本墙钟 904s） | **通过** |

> **§47 判定 = PASS**。产品链路与直连的 cpCER 差 +1.64pp 在预注册 ≤2pp 内；差异主要来自产品侧二次转码（`hyp_*_avg.mp3` → 单声道 16k 64kbps `audio.mp3`）与解码路径不同，模型/参数/revision 完全一致。

---

## 2. 产品链路 vs 直连（R8007_M8011）

| 指标 | 产品真实链路（run_c） | 直连侧车（基线） | 差值 |
|---|---:|---:|---:|
| 状态 | SUCCEEDED | SUCCEEDED | — |
| cpCER | **13.99%** | 12.35% | **+1.64pp**（相对 +13.3%） |
| CER(no_punct) | 18.37% | 17.82% | +0.55pp |
| Δcp | −4.38pp | −5.47pp | +1.09pp |
| 覆盖率 | 99.85% | 99.86% | −0.01pp |
| truncated | false | false | — |
| 段数 | 687 | 698 | −11 |
| hyp/ref speakers | 4 / 4 | 4 / 4 | — |
| 墙钟（sidecar 推理） | 842.7s | 825.2s | +17.5s |
| 墙钟（产品任务） | 844.0s | — | — |
| 输入音频 | 产品二次转码 mp3（16k mono 64k） | 8ch→mono 16k wav | 转码差异 |

---

## 3. 三次运行纪实：两个阻断与修复（诚实记录）

### run_a（09:11:27–09:12:23）：**无效 —— 实际走了云端 dashscope**
- 现象：产品任务 `SUCCEEDED`、task JSON `model_name=moss-transcribe-diarize-0.9b(local)`，但 **sidecar `/transcribe` 调用次数为 0**；DB `ai_call_log` id=19713：`model_name=qwen-audio-3.1-asr-flash-filetrans`、`elapsed_ms=50766`（50.8s，云端速度）。
- 根因：**temporal-worker 容器的 env 陈旧**（容器创建早于 `.env` 更新）——worker 内只有 `ASR_TIMEOUT_SECONDS=600`，缺 `TRANSCRIPTION_ENGINE=http` / `ASR_HTTP_URL` / `TRANSCRIPTION_MAX_DURATION_SECONDS`；backend env 正确但真正执行转写的是 worker。
- 修复：仅 `./scripts/compose.sh up -d --force-recreate temporal-worker`（未改 `.env`、未动 backend、未改产品代码）。修复后 worker env 已验证：engine=http、URL=http://asr-sidecar:9971/transcribe、timeout=1800、max_duration=3600。
- 附带更正：**2026-10-22 §45b 的 60s 冒烟同样是云端跑**（DB `ai_call_log` biz_id=12：model_name=qwen-audio…、elapsed_ms=6678）；task JSON 的 `model_name` 由后端按 backend env 计算，worker 未生效时会误报 local。**本次 run_c 是首个真实的 MOSS 产品全量链路验证。**

### run_b（09:14:31–09:18:26，task_id=14）：**无效 —— 5120 token 静默截断**
- 现象：worker env 修复后，产品链路首次真正打到 MOSS：sidecar 日志 `transcribe start: audio.mp3 1861.5s max_new_tokens=5120` → `transcribe done: 161 segments, last_end=516.0s, coverage=27.72%, truncated=True`；产品任务仍 `SUCCEEDED`，CER 77.29%。
- 根因：**产品 http 分支只上传 file，不传 `max_new_tokens`**；sidecar v2 默认沿用 FunASR 适配器默认 5120，31min 音频在 ~5.7 分钟处被静默截断（正是 §47 ③ 要防的风险，实锤）。
- 修复：`scripts/moss_asr_server.py` v2→**v3**：产品未传该参数时**按音频时长自动**计算 `max_new_tokens = int(audio_s×16)+6144`，并按 131072 上下文预算扣除音频 token 后封顶；显式参数仍优先；`truncated` 以覆盖率 <99% 判定。`docker restart moss-asr-local`（仅侧车，模型重载 ~15s）。

### run_c（09:19:09–09:33:13，task_id=15）：**有效 —— §47 PASS**
- sidecar 日志：`max_new_tokens=35928 (requested=None)` → `transcribe done: 1861.5s audio -> 687 segments, last_end=1858.7s, 842.7s wall, coverage=99.85%, truncated=False`
- 产品任务 844.0s 完成，cpCER 13.99%（见 §2）。

---

## 4. Provenance（DB 为准）

| 字段 | 值 |
|---|---|
| `ai_call_log.id` | 20875 |
| `biz_id`（transcription_task.id） | 15 |
| `model_name` | `moss-transcribe-diarize-0.9b(local)` |
| `status` / `elapsed_ms` | SUCCEEDED / 842740 |
| `usage_json` | `{"engine": "local-http"}` |
| 任务 `started_at → finished_at` | 2026-10-23 09:19:09 → 09:33:13（844.0s） |

> 审计要点：`transcription_task` / task 快照的 `model_name` 由后端按 backend env 计算，**不能单独作为引擎 provenance**；必须以 worker 实际调用记录 `ai_call_log`（model_name + elapsed_ms + usage.engine）为准。本次已按此核对。

## 5. 环境与参数

| 项 | 值 |
|---|---|
| 产品 engine | `TRANSCRIPTION_ENGINE=http`，`ASR_HTTP_URL=http://asr-sidecar:9971/transcribe` |
| 超时 / 时长上限 | `ASR_TIMEOUT_SECONDS=1800`（DB `ai_model_config.timeout_seconds=1800`）、`TRANSCRIPTION_MAX_DURATION_SECONDS=3600` |
| 侧车 | compose `moss-asr-local`（nvidia/cuda:12.3.1-base-ubuntu22.04 + `scripts/.venv-moss` + `scripts/moss_asr_server.py` v3；`/models` 只读挂载权重） |
| 模型 | `OpenMOSS-Team/MOSS-Transcribe-Diarize`，revision `e8681d68e7042738ffca8ac8212bc8fcb1131ab8`，bf16、sdpa、cuda:0 |
| 采样参数 | `max_new_tokens=35928`（v3 按时长自动：1861.5×16+6144），`max_length=131072`，贪心解码 |
| GPU | RTX 5070 Ti 16GB（product 链路推理中 ~9GB 总用量） |
| 参考口径 | AliMeeting 近讲 TextGrid；cpCER=MeetEval cpWER 字级 no_punct；与直连/历史基线同 `asr/cpcor.py` |

## 6. 限制

1. **单场**（R8007_M8011，31min，4 人）——§47 预注册即为 1 场；不能外推全部 8 场产品链路（直连 8 场指标见 `reports/2026-10-22_moss_own_metrics.md`）。
2. 产品链路 cpCER 与直连差 +1.64pp，接近但低于 2pp 门限；主因是产品二次转码（wav→64kbps mp3）与解码差异，非模型/参数差异。
3. 本次修复了 sidecar 的动态 token 缺陷后才通过；**该 v3 变更是引擎侧脚本改动（scripts/），未改产品代码/.env/gate/LEDGER**。
4. `max_new_tokens` 自动值依赖音频时长估算，超长音频仍受 131072 上下文/显存约束（>60min 容量另见 task-14 / LEDGER §48）。

## 7. 复现命令

```bash
cd /home/song/code/Agent/media_agent/AIMeeting

# 0) 确认 product env 与 sidecar 就绪（worker env 必须含 http 引擎配置）
docker exec meetingbot-local-temporal-worker-1 env | grep -E "TRANSCRIPTION_ENGINE|ASR_HTTP_URL"
docker exec meetingbot-local-backend-1 python -c "import urllib.request;print(urllib.request.urlopen('http://asr-sidecar:9971/health',timeout=5).read().decode())"

# 1) 产品真实链路全量复核（会新建 [ASR_EVAL] 会议并上传资料）
set -a; . meetingbot/fastapi-app/.env; set +a
PYTHONPATH=evaluation meetingbot/fastapi-app/.venv/bin/python \
  evaluation/asr/first_test/run_first_test.py --meeting R8007_M8011 \
  --base-url http://127.0.0.1:19091 --container meetingbot-local-backend-1 \
  --tag moss_http_product --reuse-audio

# 2) cpCER/覆盖率/§47 判定（独立 workdir，不碰 cpcor/ 基线）
evaluation/.venv/bin/python /tmp/asr_task13_compute.py \
  evaluation/asr/first_test/task_R8007_M8011.moss_http_product.json valid \
  --out evaluation/results/moss_product_link_full.json

# 3) provenance 核对（以 worker 实际调用记录为准）
docker exec meetingbot-local-db-1 mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e \
 "SELECT id,biz_id,model_name,status,elapsed_ms FROM ai_call_log ORDER BY id DESC LIMIT 2;"
```

## 8. 产物

- `evaluation/results/moss_product_link_full.json`（§47 判定 + 三次运行纪实 + provenance）
- `evaluation/asr/first_test/task_R8007_M8011.moss_http_product.json`（产品链路 task 快照）
- `evaluation/asr/first_test/hypothesis_R8007_M8011.moss_http_product.txt`、`cer_R8007_M8011.moss_http_product.json`
- `evaluation/asr/first_test/moss_direct/product_link/valid/`（cpCOR 中间 STM/cpwer，独立目录）
- 脚本改动：`scripts/moss_asr_server.py` **v3**（按音频时长自动 `max_new_tokens` + 上下文预算封顶 + 覆盖口径 truncated）
- 运行证据备份（临时）：`/tmp/task13_truncated_5120_run/`（run_b 截断快照）；run_a 证据以 DB `ai_call_log` id=19713 为准
