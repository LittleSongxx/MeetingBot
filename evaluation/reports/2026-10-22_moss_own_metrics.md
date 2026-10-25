# MOSS-Transcribe-Diarize 本地 ASR 自身质量指标（AliMeeting Eval 8 场）

- **生成日期**：2026-10-22（Asia/Shanghai；数据截止 19:25）
- **执行**：asr-runner（team task-11）；GPU 侧车在 WSL Ubuntu-22.04
- **政策依据**：`evaluation/LEDGER.md §45` —— 用户已决定 **MOSS 直接采用、不做 c3600 对照实验**。
  本报告只报 **MOSS 自身质量指标**，**不是相对提升**；历史云端数字仅背景一句话，不做逐场配对。
- **口径（直连侧车）**：`8ch 远场 → ffmpeg 单声道 16k → POST scripts/moss_asr_server.py /transcribe
  → 适配 task JSON → evaluation/asr/cpcor.py`；**不是产品 HTTP 真实链路**（产品链路契约已单独验证，见 §7）。
- **数据源**：
  - `evaluation/results/moss_own_metrics.json`（本报告数字源，`scripts/moss_own_metrics.py` 生成）
  - `evaluation/asr/first_test/task_{8 场}.moss_direct.json`、`moss_direct/raw/*.raw.json`、`moss_direct/single_*.moss_direct.json`

---

## 1. 结论先行

- **8/8 场全部成功、0 失败**：覆盖率 99.53%–99.98%，按覆盖口径 **0 场静默截断**
  （R8009_M8019 侧车 `truncated` flag 因“尾部 9.2s 静音”被旧启发式误标，实测尾部 RMS −58 dBFS，
  覆盖率 99.53% ≥99% 门槛，判为非截断，详见 §5）；
- **MOSS 自身 cpCER 宏平均 13.47%**（CER 17.74%，Δcp **−4.28pp**），覆盖 2/3/4 说话人与 26.2–37.3 分钟长场：
  - 最好：R8009_M8020 **4.51%**（2 人场，覆盖率 99.85%）；
  - 最差：R8001_M8004 **25.88%**（4 人场，hyp 5 vs ref 4 过度分离）；
  - 4 人场 cpCER 11.60%–25.88%，2 人场 4.51%–7.79%，3 人场（R8008）12.87%；
- **规模**：8 场音频合计 **252.3 分钟**；GPU 推理墙钟合计 **102.2 分钟**；宏平均 **RTF 0.406**
  （单场 0.317–0.626）；单场最长 **1161.5s**（R8007_M8010，密集语音 4 人场）；
- **GPU**：16GB 卡单路串行可行；实测峰值总用量 **12.67GB**（`nvidia-smi`，含 ~2.7GB 系统基线，
  出现在 30.9min 密集场）、最高温度 64°C；
- **产品启用**：backend 容器 → WSL 宿主侧车 **已实测可达**（`host.docker.internal:9971` 的 `/health`
  200 OK、`/transcribe` 200 OK，30s smoke 5.5s 返回 3 段；v2 侧车推理中 `/health` 仍 0.011s 响应），
  可直接按 host 侧车方案启用；**未改 .env、未重启后端**。步骤/回滚/风险见 §7。

---

## 2. 指标定义（MOSS 自身，不含任何 c3600 对照）

| 指标 | 定义 |
|---|---|
| cpCER | MeetEval `cpwer`（字级 no_punct：汉字逐字空格切分）；参考 = AliMeeting Eval **近讲** TextGrid 共享转写（每说话人一个文件）；假设 = 侧车 segments 的 `speaker_label` + `content` |
| CER(no_punct) | 时间序单说话人拼接的 `cer_tool` no_punct CER（与 cpCER 同归一化口径） |
| Δcp | cpCER − CER（说话人归属损失；负值 = 归属对齐后错误低于单流） |
| 覆盖率 | 最后一段 `end` / 音频时长（侧车 `audio_seconds` vs `duration`） |
| RTF | 推理墙钟 / 音频时长（单场、串行） |
| hyp/ref speakers | 侧车输出的说话人标签数 / 近讲 TextGrid 的说话人数（记录过度/欠分离，不能替代 DER） |
| 失败/截断 | 失败 = 无 task/指标产物；**截断 = 覆盖率 <99%**（尾部静音造成的 <10s 差值不算） |

---

## 3. 逐场结果（8/8 有效，MOSS 自身指标）

| 会议 | 音频min | cpCER | CER | Δcp(pp) | 覆盖率 | hyp/ref spk | 段数 | RTF | 耗时s | 截断 |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|
| R8001_M8004 | 26.2 | **25.88%** | 29.32% | −3.44 | 99.94% | 5/4 | 509 | 0.386 | 607.7 | 否 |
| R8003_M8001 | 34.5 | **11.60%** | 16.00% | −4.40 | 99.98% | 4/4 | 609 | 0.391 | 808.7 | 否 |
| R8007_M8010 | 30.9 | **25.39%** | 37.40% | −12.01 | 99.87% | 5/4 | 1020 | 0.626 | 1161.5 | 否 |
| R8007_M8011 | 31.0 | **12.35%** | 17.82% | −5.47 | 99.86% | 4/4 | 698 | 0.443 | 825.2 | 否 |
| R8008_M8013 | 37.3 | **12.87%** | 15.14% | −2.27 | 99.83% | 4/3 | 645 | 0.353 | 790.4 | 否 |
| R8009_M8018 | 27.6 | **7.34%** | 9.11% | −1.77 | 99.80% | 2/2 | 479 | 0.357 | 591.2 | 否 |
| R8009_M8019 | 32.9 | **7.79%** | 11.10% | −3.31 | 99.53% | 2/2 | 644 | 0.377 | 744.7 | 否（尾部静音，旧 flag 误标） |
| R8009_M8020 | 31.8 | **4.51%** | 6.06% | −1.55 | 99.85% | 2/2 | 474 | 0.317 | 605.6 | 否 |

**字段说明**：cpCER/CER 为百分比；Δcp 为 pp；覆盖率 = covered/audio；hyp/ref 为侧车说话人数 / 参考说话人数；
耗时 = 纯推理墙钟（不含模型加载 ~2s 与 cpCER 计算时间）；RTF = 耗时/音频。
**观察**：Δcp 全为负（−1.55 ~ −12.01pp）→ 说话人归属对齐后错误低于单流 CER，diarization 整体有增益；
R8001（5/4）与 R8007_M8010（5/4）存在过度分离，R8008（4/3）亦然——**cpCER 对过度分离不敏感，不能替代 DER**。

---

## 4. 宏平均（MOSS 自身，8 场等权）

| 指标 | 值 |
|---|---|
| cpCER | **13.47%** |
| CER(no_punct) | 17.74% |
| Δcp | **−4.28pp** |
| 覆盖率 | 宏平均 99.83%（最低 99.53%） |
| RTF | 0.406（0.317–0.626） |
| 截断 | 0 场（覆盖口径；侧车 flag 1 场为尾部静音误标） |
| 失败 | 0 场 |
| n_valid / n_total | **8 / 8** |
| 音频总量 / 推理墙钟总量 | 252.3 min / 102.2 min（单场最长 1161.5s） |
| hyp/ref speakers | 3 场过度分离（R8001 5/4、R8007_M8010 5/4、R8008 4/3），5 场一致 |

> **不做 c3600 配对/相对提升**（§45 用户决定）。历史云端 8 场 cpCER 30.09% 仅作背景一句话，不进入对比表。
> 因此“方向一致性/配对 CI”等相对基线判据在本报告不适用，只对 MOSS 自身做绝对读数。

---

## 5. 失败 / 截断 / 异常记录

- **失败：0/8**；**静默截断（覆盖 <99%）：0/8**。
- **R8009_M8019 的 `truncated=true` 为旧启发式误标**：侧车 v1 用 `duration < 音频-5s` 判截断，
  该场尾部 9.2s 为静音（实测 1964.7–1973.9s 段 RMS **−58.0 dBFS**，对照前 30s 为 −47.4 dBFS；
  输出末句为语义完整的“加油。”），覆盖率 99.53% ≥99%，判为**非截断**。v2 侧车已改为
  **覆盖率 <99% 才置 `truncated`**，避免此类误报。
- **历史遗留清理**：`single_*.moss_direct.json` 中早期 `INTERRUPTED` 记录（会话中断，非引擎失败）
  已被本轮 8 场重跑结果覆盖；旧中断日志仅存 `/tmp/moss_direct_logs/`，仅作排障参考。
- **v1 → v2 侧车变更**：v2 仅把 `generate` 移入线程池（转写期间 `/health` 仍可响应）并把截断判定改为
  覆盖口径；推理参数、segments 契约、指标口径不变。8 场指标由 v1 产出，v2 用 30s smoke 验证等价
  （容器→侧车 HTTP 200、3 段、5.5s，见 §7.2）。

---

## 6. 环境与 provenance

| 项 | 值 |
|---|---|
| GPU | NVIDIA RTX 5070 Ti 16GB（WSL2 Ubuntu-22.04，CUDA 可用）；实测峰值总用量 12.67GB（含系统基线 ~2.7GB）、最高 64°C |
| venv | `scripts/.venv-moss`（Python 3.12，独立于产品/评测 venv） |
| torch / torchaudio | 2.11.0+cu128 |
| funasr / transformers | funasr 1.4.16（MOSS 适配器）/ transformers 5.17.0 |
| 模型 | `OpenMOSS-Team/MOSS-Transcribe-Diarize`，HF revision `e8681d68e7042738ffca8ac8212bc8fcb1131ab8`（1.82GB，bf16） |
| 权重缓存 | `~/.cache/moss-hf/MOSS-Transcribe-Diarize`（HF 快照；ModelScope 查无此仓库） |
| 推理参数 | `backend=hf`、`device=cuda:0`、`dtype=bf16`、`attn_implementation=sdpa`、`max_length=131072`、贪心解码；`max_new_tokens=int(audio_s×16)+6144`（逐场 31k–42k，实际生成未触顶） |
| 音频口径 | 8ch 远场 → `ffmpeg -ac 1 -ar 16000 -c:a pcm_s16le`（与产品 `hyp_*_avg.mp3` 同混音口径，不做通道选择） |
| 侧车 | 串行（信号量 1），**每场重启**（模型加载 1.7–2.0s）；日志 `/tmp/moss_direct_logs/sidecar_<meeting>.log` |
| 下载通道 | `HF_ENDPOINT=https://hf-mirror.com` + `HF_HUB_DISABLE_XET=1`（本机 huggingface.co 不可达） |

---

## 7. 产品启用准备（本轮**未改 .env、未重启后端**）

### 7.1 网络可达性（实测）

| 路径 | 结果 | 说明 |
|---|---|---|
| backend 容器 → `host.docker.internal:9971/health` | **OK 200** | 容器内 `getent ahostsv4 host.docker.internal` → 192.168.65.254（Docker Desktop host-gateway）+ WSL 监听 `0.0.0.0` 即可通；**无需重建 GPU 容器** |
| backend 容器 → `host.docker.internal:9971/transcribe`（30s smoke） | **HTTP 200**，3 段，5.5s，`truncated=false`，speakers `S01` | 真实 multipart 契约、产品容器内发起 |
| backend 容器 → `192.168.65.254:9971/health` | OK 200 | 与 host.docker.internal 等价 |
| backend 容器 → WSL eth1 IP `192.168.1.55:9971` | 连接超时 | 不需要这条路 |
| 容器内 `curl` | **不存在** | 检查用 `python -c "import urllib.request..."` 或 `scripts/check_sidecar_from_backend.sh` |
| 侧车推理中 `/health` | **v2：0.011–0.014s 响应**（v1 会阻塞：generate 同步占事件循环） | 产品单请求不受影响；若监控依赖 health 请用 v2（已安装） |

> 备注：`docs/MOSS-ASR启用.md` §4 写的「host.docker.internal 实测超时」是在**侧车推理中**（v1 事件循环被占）
> 或侧车未启动时测的；侧车空闲/已换 v2 后实测 200 OK。若采用 compose 内 `asr-sidecar`（profile: moss）
> 则完全走 compose 网络、不依赖 host-gateway，二者皆可；host 侧车方案改动更小、无需把 1.8GB 权重复制到 Windows。

### 7.2 启用步骤（host 侧车方案；需 lead 批准后执行）

```bash
# 0) 启动常驻侧车（WSL 内；preload 模型，监听 0.0.0.0:9971）
cd /home/song/code/Agent/media_agent/AIMeeting
bash scripts/moss_sidecar_service.sh start          # 常驻 + 等 /health loaded=true
bash scripts/check_sidecar_from_backend.sh          # backend 容器 → 侧车 /health 应为 OK 200
bash scripts/check_sidecar_from_backend.sh http://host.docker.internal:9971 --post   # 30s 冒烟转写（可选，占 GPU）

# 1) 编辑 meetingbot/fastapi-app/.env（仅验证窗口内）
TRANSCRIPTION_ENGINE=http
ASR_HTTP_URL=http://host.docker.internal:9971/transcribe
ASR_TIMEOUT_SECONDS=1800                  # 单场实测 591–1162s；默认 600s 会误判超时
TRANSCRIPTION_MAX_DURATION_SECONDS=3600   # 建议 60min 保守守卫（MOSS 16GB 卡实测上限见 §7.3）

# 2) 重启 backend（bootstrap.py 启动时会把 ASR_TIMEOUT_SECONDS 写入 ai_model_config.timeout_seconds；
#    若不重启，也可手动 SQL：UPDATE ai_model_config SET timeout_seconds=1800 WHERE model_type='TRANSCRIPTION';）
./scripts/compose.sh up -d backend

# 3) 端到端复核（≥1 场，产品真实链路）
#    上传一场会议 → 产品 /transcription → 检查：
#    · task.model_name == "moss-transcribe-diarize-0.9b(local)"
#    · 任务 SUCCEEDED、分段/文本与直连一致、无超时
#    · 同一场产品链路 cpCER 与直连差 ≤2pp（§45 验证要求）

# 4) 回滚（一条命令级）
#    .env: TRANSCRIPTION_ENGINE=dashscope，ASR_TIMEOUT_SECONDS=600
#    ./scripts/compose.sh up -d backend
#    bash scripts/moss_sidecar_service.sh stop
```

### 7.3 风险清单

1. **超时**：单场推理 591–1161.5s，产品 httpx `timeout=config.timeout_seconds` 默认 600s →
   必须放大；建议 **≥1800s**（若产品 2 workers 并发排队，第二个任务需覆盖“等待+生成”，建议 2400s
   或验证窗口内 `WEB_CONCURRENCY=1`）。该值来自 DB `ai_model_config.timeout_seconds`；改 `.env` 后
   需重启 backend 由 `bootstrap.py` 同步（或直接 SQL 更新）。
2. **时长/显存上限（重要）**：`TRANSCRIPTION_MAX_DURATION_SECONDS` 默认 7200s，但 16GB 卡**不能**安全
   吃满 90min——实测 30.9min 密集场峰值总用量已达 **12.67GB**（含系统基线 2.7GB），其中 KV/encoder
   随音频长度近似线性增长；按外推 **>45–60min 有 OOM/上下文溢出风险**（上下文 131072 token 上限对应
   约 81min，但显存先到上限）。**建议产品守卫设 3600s（60min），>45min 会议先做分段验证**；
   若按 §45 设 5400s，必须先补 >60min 长场验证。
3. **吞吐/排队**：侧车单路串行，RTF 0.32–0.63（30min 会议 10–19min）；产品多 worker/并发转写会排队，
   队列中第二个长任务需把 HTTP 读超时覆盖“等待+生成”时间（见 1）。
4. **常驻依赖 WSL**：侧车进程生命周期绑定 WSL 实例；Windows 休眠/WSL 关闭会掉线 → 产品报
   「本地ASR服务请求失败」。需开机自启（WSL 任务计划 + `moss_sidecar_service.sh start`）或改用
   compose `asr-sidecar`（随 Docker Desktop 自启）。
5. **模型路径/权重**：权重在 `~/.cache/moss-hf/...`（1.8GB，不入仓库，需预置并钉 revision）；
   离线部署需自带。
6. **说话人标签形态**：侧车返回 `S01/S02…`（历史旧版为 `01-S01`）；产品 `parse_segments` 原样存
   `speaker_label`，下游按不透明字符串处理。启用后抽查一场会议纪要/说话人展示是否正常。
7. **provenance**：http 引擎分支写 `LOCAL_ASR_MODEL_NAME="moss-transcribe-diarize-0.9b(local)"`；
   revision/bf16/sdpa/max_new_tokens 等参数不在 task 记录中，部署时需按本报告 §6 留档。
8. **模型本身**：3 场过度分离（R8001 5/4、R8007_M8010 5/4、R8008 4/3）；Δcp 全负说明 diarization
   有增益但非完美；AliMeeting 近讲参考口径系统性高估远场错误。

### 7.4 与 compose 内 `asr-sidecar` 方案的取舍

仓库另有 compose `asr-sidecar`（profile: moss，见 `compose.yaml`、`docs/MOSS-ASR启用.md`）：
优点是随 Docker 自启、compose 网络内 `http://asr-sidecar:9971/transcribe` 更稳、无 host-gateway 依赖；
代价是需构建 GPU 容器并把 1.8GB 权重复制到 Windows 宿主目录（当前权重在 WSL 内）。
**本轮验证的是 host 侧车路径**（改动小、网络与契约均已实测通）；两条路都可回滚，建议 lead 按部署便利性选择。

---

## 8. 限制与诚实声明

- 本报告只测 **MOSS 自身**，**不含 c3600 逐场对照/相对提升**（用户决定，§45）；云端数字仅背景；
- 8 场为 **直连侧车口径**，非产品真实 HTTP 链路；产品链路差异（超时/落库/重试/时长守卫）本轮只验证了
  容器→侧车的 `/health` 与 `/transcribe` 契约，未跑完整产品转写任务；
- 单评测集（AliMeeting Eval 8 场，far-field 8ch 平均下混），近讲共享 TextGrid 参考；不代表真实会议分布；
- cpCER 是说话人归属性字错误率，不能替代 DER；hyp/ref speakers 已逐场记录（3 场过度分离）；
- GPU 峰值仅采样了 3 场（采样器 18:57 起），R8007_M8010 峰值 12.67GB 为实测；其余场同量级估计；
- 有效 n=8/8；若产品链路复核不过（差 >2pp 或出现截断/超时），以产品链路结果为准。

---

## 9. 复现命令

```bash
cd /home/song/code/Agent/media_agent/AIMeeting

# 环境/模型（一次性；权重不入仓库）
export HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 HF_HOME=/home/song/.cache/hf-moss
scripts/.venv-moss/bin/hf download OpenMOSS-Team/MOSS-Transcribe-Diarize \
  --revision e8681d68e7042738ffca8ac8212bc8fcb1131ab8 \
  --local-dir /home/song/.cache/moss-hf/MOSS-Transcribe-Diarize

# 8ch → mono 16k（幂等，/tmp/moss_direct_audio）
bash scripts/prepare_moss_audio.sh

# 逐场跑（每场重启侧车；产物 → evaluation/asr/first_test/moss_direct/）
bash scripts/run_moss_direct_8.sh                 # 或指定场：bash scripts/run_moss_direct_8.sh R8001_M8004

# 汇总 MOSS 自身指标（纯 CPU）→ evaluation/results/moss_own_metrics.json + Markdown 表
evaluation/.venv/bin/python scripts/moss_own_metrics.py

# 产品启用前网络/契约检查（容器内无 curl，用 python）
bash scripts/moss_sidecar_service.sh start
bash scripts/check_sidecar_from_backend.sh                                        # /health
bash scripts/check_sidecar_from_backend.sh http://host.docker.internal:9971 --post  # 30s 冒烟转写

# 停止
bash scripts/moss_sidecar_service.sh stop
```

---

## 10. 产物清单

- `evaluation/results/moss_own_metrics.json`（本报告数字源：逐场 + 宏平均 + 失败/截断统计）；
- `evaluation/asr/first_test/task_{R8001_M8004,R8003_M8001,R8007_M8010,R8007_M8011,R8008_M8013,R8009_M8018,R8009_M8019,R8009_M8020}.moss_direct.json`；
- `evaluation/asr/first_test/moss_direct/raw/*.raw.json`（侧车原始响应：audio/covered/elapsed/truncated/段）；
- `evaluation/asr/first_test/moss_direct/single_*.moss_direct.json`（逐场 cpCER/CER/Δcp/覆盖率/RTF）；
- `evaluation/asr/first_test/moss_direct/cpcer/`（MeetEval STM/cpwer 中间产物，独立目录，未碰 `cpcor/` 基线）；
- `scripts/moss_asr_server.py`（v2：线程池推理 + backend=hf + 动态 token + 覆盖口径 truncated）、
  `scripts/moss_direct_cpcor.py`、`scripts/moss_own_metrics.py`、`scripts/run_moss_direct_8.sh`、
  `scripts/prepare_moss_audio.sh`、`scripts/moss_sidecar_service.sh`、`scripts/check_sidecar_from_backend.sh`、
  `scripts/moss_sidecar_smoke.sh`、`scripts/requirements-moss.txt`；
- 运行日志（临时 /tmp）：`/tmp/moss_direct_logs/sidecar_<meeting>.log`、`/tmp/moss_gpu_samples.log`（显存采样）。
