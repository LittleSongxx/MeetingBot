# MOSS 本地 ASR 引擎启用与质量口径（2026-10-22）

> 用户决定：MOSS-Transcribe-Diarize **直接采用**，不再做 c3600 对照实验；只测 MOSS 自身质量指标。
> 本文件是启用手册 + 指标口径。变更登记见 `evaluation/LEDGER.md §45`，指标见
> `evaluation/reports/2026-10-22_moss_own_metrics.md`。

## 1. 质量口径（MOSS 自身，不报相对提升）

- 主指标：**cpCER**（MeetEval cpWER 数值等价；字级 no_punct，参考=AliMeeting 近讲 TextGrid 共享转写）；
- 同时报：CER(no_punct)、**Δcp=cpCER−CER**、**覆盖率**（covered_seconds/audio_seconds）、
  hyp/ref speakers、RTF/墙钟、失败/截断数、n_valid/n_total；
- 历史 c3600 数字只作背景，**不做逐场配对对照**（按用户决定）；
- 已知边界：cpCER 对“过度分离”不敏感（R8001 hyp_spk 5 vs ref 4），不能替代 DER；
  AliMeeting Eval 是公开评测集，不等于真实会议分布。

## 2. 组成

```
backend ──HTTP──▶ asr-sidecar（compose profile: moss，GPU）
                    └─ scripts/moss_asr_server.py：FunASR + MOSS-Transcribe-Diarize 0.9B
权重目录（宿主）──▶ /models（只读挂载）：MOSS_MODEL_HOST_DIR
```

- 侧车串行推理（信号量 1）；单次生成按音频时长动态 `max_new_tokens`（16 tok/s + 6144 余量），
  回传 `covered/truncated/elapsed`；单场失败/截断必须显式可见，不允许静默 SUCCEEDED。
- 单次音频上限：MOSS 规格 90min，但 **16GB 卡容量实测 62.87min 合成场峰值 15,976/16,303 MiB（98%）、
  RTF 0.730、墙钟 2753.6s**（无 OOM 但仅 327MiB 余量）→ **没有放宽余量，守卫取 2700s（45min）**；
  >45min 会议需拆段。若要单次 60min，需把超时提到 ≥3000s 并在目标机验证 55–60min 显存余量。
- 超时：31min 场实测 844s；45min×RTF0.73≈1971s → `ASR_TIMEOUT_SECONDS=2400`（DB
  `ai_model_config.timeout_seconds` 同步；默认 600 会误判超时）。

## 3. 启用步骤（本仓库默认 compose）

1. **准备权重**：`MOSS-Transcribe-Diarize`（HuggingFace，Apache-2.0；HF revision `e8681d68…`）
   下载/复制到宿主目录，例如 `C:/moss-model`；`.env` 设
   `MOSS_MODEL_HOST_DIR=C:/moss-model`。
2. **构建并启动侧车**：
   ```bash
   ./scripts/compose.sh --profile moss build asr-sidecar
   ./scripts/compose.sh --profile moss up -d asr-sidecar
   ./scripts/compose.sh ps asr-sidecar   # STATUS 应为 healthy
   ```
3. **切换 backend 引擎**（`.env`）：
   ```bash
   TRANSCRIPTION_ENGINE=http
   ASR_HTTP_URL=http://asr-sidecar:9971/transcribe
   ASR_TIMEOUT_SECONDS=2400
   TRANSCRIPTION_MAX_DURATION_SECONDS=2700
   ```
   注意：`ASR_TIMEOUT_SECONDS` 只在初始化写进 `ai_model_config.timeout_seconds`；已存在库需同步：
   ```sql
   UPDATE ai_model_config SET timeout_seconds=2400 WHERE model_type='TRANSCRIPTION';
   ```
   然后重建 backend **和 temporal-worker**（执行侧是 worker）：
   `./scripts/compose.sh build backend temporal-worker && ./scripts/compose.sh up -d backend && ./scripts/compose.sh up -d --force-recreate temporal-worker`。
4. **验证**：
   ```bash
   docker exec meetingbot-local-backend-1 python -c \
     "import urllib.request;print(urllib.request.urlopen('http://asr-sidecar:9971/health',timeout=5).read().decode())"
   # 真实链路：上传一段会议音频走产品 /transcription，确认 task.model_name =
   # moss-transcribe-diarize-0.9b(local)、覆盖率≥99%、无 truncated、墙钟 < 超时
   # 已有冒烟脚本：scripts/product_link_smoke_moss.py（60s 音频，产物 moss_product_link_smoke.json）
   ```

> **本机已验证（2026-10-23，含一处重要更正）**：
> - **执行侧是 temporal-worker，不是 backend**：只重建 backend 时 worker 仍用旧 env，可能出现
>   "详情显示 local、实际走云端"。启用/回滚后必须 `--force-recreate temporal-worker`；
>   引擎 provenance 一律看 `ai_call_log.model_name`/`usage.engine`，不看 `task.model_name`。
> - **全量产品链路验证**（R8007_M8011，31min）：产品 cpCER **13.99%** vs 直连 12.35%（+1.64pp）、
>   覆盖率 99.85%、墙钟 844s、`model_name=moss-transcribe-diarize-0.9b(local)`、DB 日志
>   `usage.engine=local-http`。（10-22 的 60s 冒烟实际是云端，已作废；见 LEDGER §47b。）
> - **静默截断风险**：产品 http 分支曾不传 `max_new_tokens`，侧车默认 5120 在 ~5.7min 截断
>   （覆盖率 27.72%）。现侧车 v3 自动按音频时长计算，产品也显式传 `16 tok/s+6144` 并拒绝
>   truncated/覆盖率<99% 的输出（失败而非 SUCCEEDED）。
> - **镜像纪律**：真正执行转写的是 temporal-worker，它有自己的镜像；产品代码变更后必须
>   `compose build backend && compose build temporal-worker` 并重建两个服务，只重建 backend 会出现
>   “UI/详情是新代码、执行是旧代码”的错配。
> - 本机运行形态：`scripts/moss_sidecar_container.sh` 在 compose 网络内跑挂载 WSL venv 的
>   sidecar 容器（镜像构建走 Dockerfile 留给可移植部署）；回滚：`.env` 改回 dashscope +
>   重建 backend + force-recreate worker + 停侧车。
5. **回滚**：`.env` 改回 `TRANSCRIPTION_ENGINE=dashscope`，`./scripts/compose.sh up -d backend`；
   侧车可停（`--profile moss stop asr-sidecar`）。回滚不动数据。

## 3.5 本机脚本（Docker Desktop + WSL2）

```bash
./scripts/moss_sidecar_container.sh          # 起 sidecar 容器（挂载 WSL venv + C:/moss-model）
meetingbot/fastapi-app/.venv/bin/python scripts/product_link_smoke_moss.py   # 产品链路冒烟
```
可移植部署仍用 Dockerfile/compose profile（首次构建要下载 torch/CUDA，约数 GB）。

## 4. 运维与限制

- GPU：0.9B bf16 权重约 4.9GB 显存（实测 300s 音频峰值 4.87GB），16GB 卡可单路串行；
  并发转写会排队（信号量 1）。
- 延迟：RTF 实测 0.35–0.39（远场混音单声道），37min 会议约 13min；**云端 API 仍更快**，
  切换后转写任务墙钟显著变长，属预期取舍。
- 依赖/版本：镜像 `nvidia/cuda:12.3.1-runtime-ubuntu22.04` + `scripts/requirements-moss.txt`
  （torch cu128 / funasr / transformers）；模型 revision 应在部署时钉死并记录。
- 网络：backend 在 Docker Desktop、侧车在 WSL 宿主时，**侧车空闲/v2 下实测 `host.docker.internal:9971`
  可达**（200；v1 推理中会被事件循环阻塞而表现为超时）；v2 已把生成放进线程池。两条路都可：
  host 侧车（改动小、不用把 1.8GB 权重复制到 Windows）或 compose `asr-sidecar`（不依赖 host-gateway）。
  本机实际采用 **compose 网络内容器**（`scripts/moss_sidecar_container.sh`），因为镜像构建网络太慢。

## 4.5 MOSS 自身质量指标（2026-10-22，8/8 场）

| 会议 | cpCER | CER | Δcp(pp) | 覆盖率 | hyp/ref | RTF |
|---|---:|---:|---:|---:|---|---:|
| R8001_M8004 | 25.88% | 29.32% | −3.44 | 99.94% | 5/4 | 0.386 |
| R8003_M8001 | 11.60% | 16.00% | −4.40 | 99.98% | 4/4 | 0.391 |
| R8007_M8010 | 25.39% | 37.40% | −12.01 | 99.87% | 5/4 | 0.626 |
| R8007_M8011 | 12.35% | 17.82% | −5.47 | 99.86% | 4/4 | 0.443 |
| R8008_M8013 | 12.87% | 15.14% | −2.27 | 99.83% | 4/3 | 0.353 |
| R8009_M8018 | 7.34% | 9.11% | −1.77 | 99.80% | 2/2 | 0.357 |
| R8009_M8019 | 7.79% | 11.10% | −3.31 | 99.53% | 2/2 | 0.377 |
| R8009_M8020 | 4.51% | 6.06% | −1.55 | 99.85% | 2/2 | 0.317 |

**宏平均 cpCER 13.47% / CER 17.74% / Δcp −4.28pp / 覆盖率 99.83%（min 99.53%）/ RTF 0.406**；
0 失败、0 截断；合计音频 252.3min、GPU 102.2min。详见
`evaluation/reports/2026-10-22_moss_own_metrics.md`、`evaluation/results/moss_own_metrics.json`。
（按用户决定：只报自身指标，不做 c3600 对照。）

## 5. 与评测的关系

- MOSS 的 cpCER/CER/覆盖率等自身指标用 `scripts/moss_direct_cpcor.py`（直连侧车）或
  `evaluation/asr/first_test/run_first_test.py`（产品真实链路）产出，口径同 `asr/cpcor.py`；
- 切换引擎后，轴 2–4 的绝对数全部是“MOSS 基板”读数，需要重基线（不得与旧云端基板数字相减）；
- 产品侧详情接口在 http 引擎分支显示 `LOCAL_ASR_MODEL_NAME`（本轮已修并冒烟验证：
  `moss-transcribe-diarize-0.9b(local)`），不得再把云端 `config.model_name` 当读数。
