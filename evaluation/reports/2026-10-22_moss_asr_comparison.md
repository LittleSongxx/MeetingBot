# MOSS 本地 ASR 引擎直连复测（部分结果，2026-10-22）

> 执行：asr-runner（GPU 侧车）；本报告在会话中断后由 Lead 依据已落盘产物代写，**只包含已完成的有效场次**（R8003 的 cpCOR 由 Lead 用已落盘 task/raw 离线补算，无新增模型调用）。
> 口径：**直连侧车**（`scripts/moss_asr_server.py` 直接收音频 → 返回 segments → 适配为 task JSON → `asr/cpcor.py` 算 cpCER），**不是产品 HTTP 真实链路**。
> 预注册：产品真实链路验证规则见 `evaluation/LEDGER.md §41`；本直连复测属探索性，不替代 §41。
> 证据：`evaluation/asr/first_test/moss_direct/`（`single_*.json`、`raw/*.raw.json`、`cpcor_report_moss_direct.json`、`cpcer/`）；基线 `evaluation/asr/first_test/cpcor/cpcor_report_c3600_8m.json`。

## 1. 结论先行

- **方向强正向，n=3 不能外推**：三场有效配对都是大幅改善——
  R8001_M8004 **cpCER 25.88% vs 云端 c3600 42.04%（−16.16pp，相对 −38.4%）**；
  R8003_M8001 **11.60% vs 27.18%（−15.58pp，相对 −57.3%）**；
  R8008_M8013（8 场最长 37.3min）**12.87% vs 36.53%（−23.66pp，相对 −64.8%）**；
  三场宏平均 **16.78% vs 35.25%（−18.47pp，相对 −52.4%）**。
- **修复后长音频跑通**：三场覆盖率 **99.94% / 99.98% / 99.83%**、`truncated=false`、0 失败；最长 37.3min 场无 OOM（峰值显存约 4.7GB/16GB）。
- **与旧配置形成直接对照**：2026-09-27 旧配置 8 场里 3 场失败于加载（ModelScope 无此模型）、1 场静默截断（R8007_M8010 只覆盖 12.4%、cpCER 90.86% 是伪影）；新配置已消除这两类根因。
- **但样本只有 3 场**：原计划 5 场（R8001/R8008/R8003/R8007_M8011/R8009_M8020），会话中断时 R8003 推理已完成、只差 `cpcor.compute`；Lead 用已落盘 task/raw 产物离线补算（无新增模型调用，single 记录标 `recovered_by`）。R8007_M8011/R8009_M8020 未跑。**不得把 3 场宏平均当 8 场结论**。
- **建议**：按 `LEDGER §41` 预注册进入产品真实链路验证窗口（8 场、先做超时/时长上限/覆盖率守卫改造），而不是直接切默认引擎。

## 2. 逐场结果（有效 3/5）

| 会议 | 音频时长 | cpCER（MOSS） | cpCER（c3600 云端） | Δpp | 相对 | CER（MOSS） | Δcp | 覆盖率 | hyp/ref speakers | 耗时 | RTF | 截断 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---|
| R8001_M8004 | 26.2min | **25.88%** | 42.04% | **−16.16** | −38.4% | 29.32% | −3.44pp | 99.94% | 5 / 4 | 607.7s | 0.386 | 否 |
| R8008_M8013 | 37.3min | **12.87%** | 36.53% | **−23.66** | −64.8% | 15.14% | −2.27pp | 99.83% | 4 / 3 | 790.4s | 0.353 | 否 |
| R8003_M8001 | 34.5min | **11.60%** | 27.18% | **−15.58** | −57.3% | 16.00% | −4.40pp | 99.98% | 4 / 4 | 808.7s | 0.391 | 否（中断后离线补算） |
| R8007_M8011 | 31.0min | — | 33.06% | — | — | — | — | — | — | — | — | 未跑 |
| R8009_M8020 | 31.8min | — | 11.71% | — | — | — | — | — | — | — | — | 未跑 |

宏平均（**仅三场有效配对**）：MOSS **16.78%** vs c3600 同子集 **35.25%**，Δ **−18.47pp**（相对 −52.4%）。
逐场配对方向：3/3 改善；CER 三场均优于基线（29.32% vs 39.04%、16.00% vs 23.92%、15.14% vs 47.04%）。**n=3 的配对结论只能作方向信号；§41 要求 8 场、≥7/8 方向一致、CI>0 才算过门。**

## 3. 旧配置为什么失败、这次修了什么（代码级根因）

| 问题（2026-09-27 旧配置） | 证据 | 修复（本轮） |
|---|---|---|
| 5/8 场 FAILED：`本地ASR服务请求失败：Internal Server Error` | 旧 `task_*.moss.json.error_message`；模型只在 HuggingFace、ModelScope 查无此仓库 | FunASR 1.4.16 适配器要求 `model=<注册名> + model_path=<权重目录> + backend="hf"`；改用 HF revision 钉死 `e8681d68…`，经 hf-mirror 下载 1.8GB 到 `~/.cache/moss-hf/MOSS-Transcribe-Diarize`（不入仓库） |
| R8007_M8010 静默截断但任务 SUCCEEDED（cpCER 90.86% 假读数） | 旧快照仅覆盖 229.3s/1856.3s=12.4%；默认 `max_new_tokens=5120` 只够约 6 分钟音频（MOSS 约 14–15 text token/秒音频） | 按音频时长动态给 `max_new_tokens`（16 tok/s + 6144 余量，最长场 41975）+ `max_length=131072`；侧车返回 `audio_seconds/covered/truncated` 供守卫 |
| 长音频显存碎片/回显 | 旧侧车不重启、连续多场 | 每场重启侧车；峰值显存 4.7GB（16GB 卡余量充足） |
| 上传 wav 时临时文件同名撞车 | `input.wav → input.wav` | 上传件改名 `source.*`，转码目标 `input.wav` |
| 时长/覆盖不可观测 | 旧 task 无覆盖率字段 | 侧车返回 `audio_seconds/elapsed_seconds/truncated`；直连脚本按 `last_end` 计算 covered_seconds |
| speaker 标签形态 | funasr 返回 spk id | 侧车输出 `S01..` 标签；cpCER 按 speaker_label 分组 |

修复后**已开始的 2 场均无失败、无截断**；旧配置的失败根因（hub 选错 + token 上限）有直接代码证据，不是猜测。

## 4. 这个结果能说明什么、不能说明什么

**能说明**：
- MOSS 0.9B 在中文远场会议、8ch 平均下混单声道输入上，cpCER 明显优于当前云端 c3600 基线（两场 −16/−24pp）；
- 先前"MOSS 跑不动/截断"是**工程配置问题**，不是模型能力问题；修好后最长 37.3min 场可一次跑通；
- `cpCER` 与 `CER` 同向改善、`Δcp` 为负（MOSS 的说话人归属性优于云端），且不是靠"说话人数量对齐"的假象（hyp_spk 比 ref 多 1）。

**不能说明**：
- 不能把 19.38% 当 8 场宏平均（只有 2 场；R8003/R8007_M8011/R8009_M8020 未跑）；
- 不能当产品真实链路结果（本报告是直连侧车；产品链路还涉及 HTTP/超时/时长上限/落库）；
- 不能外推到真实会议分布（AliMeeting Eval 8 场、近讲共享 TextGrid 口径本身系统性高估远场错误）；
- cpCER 对"过度分离"不敏感（falarm_speaker=1），**不能用 cpCER 替代 DER**；报告必须同时记 speakers 数。

## 5. 续跑与产品真实链路验证

1. **续跑直连**（补 R8003/R8007_M8011/R8009_M8020）：
   ```bash
   cd /home/song/code/Agent/media_agent/AIMeeting
   bash scripts/run_moss_direct_8.sh R8003_M8001 R8007_M8011 R8009_M8020
   ```
   产物会写入 `evaluation/asr/first_test/moss_direct/`；报告按同表更新宏平均。
2. **产品真实链路验证**（必须按 `evaluation/LEDGER.md §41` 预注册执行）：
   - 前置改造：`ASR_TIMEOUT_SECONDS` 临时放大（历史墙钟 575s/777s > 默认 600s）；MOSS 90min 上限与产品 `TRANSCRIPTION_MAX_DURATION_SECONDS` 对齐/加引擎差异化上限；覆盖率/截断守卫（不足即失败，不允许 SUCCEEDED）；`task.model_name` 写本地引擎名；provenance（revision/参数/GPU/耗时）落报告。
   - 判定：8 场配对 cpCER ≥5pp 改善 + ≥7/8 方向一致 + 配对 CI>0 + CER 不劣化 >1pp + 0 截断 + 真实链路与直连差 ≤2pp。
   - 回滚：`TRANSCRIPTION_ENGINE=dashscope`，侧车停用。
3. **不做的**：不直接翻默认；不在样本 n=2 时对外宣称"cpCER 降到 19%"。

## 6. 复现信息

- 侧车：`scripts/moss_asr_server.py`（`--port 9971 --preload`），环境变量见文件头；模型本地目录 `MOSS_MODEL=~/.cache/moss-hf/MOSS-Transcribe-Diarize`，HF revision `e8681d68e7042738ffca8ac8212bc8fcb1131ab8`。
- 直连驱动：`scripts/moss_direct_cpcor.py`（直连侧车 → 适配 task JSON → `asr/cpcor.py` 计算，工作目录在 `moss_direct/cpcer/`，**不碰 `cpcor/` 既有基线**）；音频预处理 `scripts/prepare_moss_audio.sh`。
- 证据文件：`evaluation/asr/first_test/moss_direct/single_R8001_M8004.moss_direct.json`、`single_R8003_M8001.moss_direct.json`、`single_R8008_M8013.moss_direct.json`、`cpcor_report_moss_direct.json`、`raw/*.raw.json`。
- 运行环境：WSL Ubuntu-22.04 + RTX 5070 Ti 16GB；`scripts/.venv-moss`（torch 2.11+cu128 / funasr 1.4.16 / transformers 5.17）；单侧车串行。
