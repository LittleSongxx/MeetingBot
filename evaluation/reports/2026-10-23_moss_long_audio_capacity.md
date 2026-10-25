# MOSS >60min 合成长场容量验证（LEDGER §48，synthetic_concat 62.87min）

- **生成日期**：2026-10-23（Asia/Shanghai）
- **执行**：asr-runner（team task-14）
- **口径**：**直连侧车** `http://asr-sidecar:9971/transcribe`（从 backend 容器发起，与产品同容器网络；未走 `/transcription` 业务链路）
- **材料**：`synthetic_concat_62min.mp3` = `hyp_R8007_M8011_avg.mp3` + `hyp_R8009_M8020_avg.mp3` 顺序拼接（单声道 16k mp3，**3772.02s = 62.87min**，30.2MB）
- **预注册**：`evaluation/LEDGER.md §48`（合成材料只验证容量，**不报准确率、不做质量结论**）
- **结论**：**§48 判定 = PASS**（62.87min 单次跑完：无 OOM、覆盖率 99.92%、truncated=false）；**但代价极高**：推理墙钟 **2753.6s（45.9min，RTF 0.730）**、峰值显存 **15,976/16,303 MiB（98%，仅 ~327MiB 余量）** → **没有放宽产品 60min 守卫的安全余量**，且若放开还需同步把 `ASR_TIMEOUT_SECONDS` 提到 >2800s。
- **数字源**：`evaluation/results/moss_long_audio_capacity.json`

---

## 1. 结论与产品含义

| 项 | 结果 |
|---|---|
| 产品守卫（>3600s 拒绝） | **生效**：上传 62.87min 后 `/transcription/start` 返回 HTTP 400「音频时长 62 分钟，超过单次转写上限 1 小时…」（零 GPU 消耗） |
| 直连侧车 62.87min 单次推理 | **跑完**：1013 段，覆盖 3769.1/3772.0s（**99.92%**），`truncated=false`，墙钟 **2753.6s**（RTF **0.730**） |
| 显存 | 峰值 **15,976 MiB / 16,303 MiB（98.0%）**，仅剩 ~327MiB；最大利用率 100%，最高 59°C；**无 OOM、容器 0 重启** |
| §48 判定 | **PASS**（① 无 OOM/崩溃 ② coverage≥99% ③ truncated=false；④ 记录可运行时长） |

**产品启用建议（保守，未改任何产品配置）**：
1. **保持 `TRANSCRIPTION_MAX_DURATION_SECONDS ≤ 3600s`（60min）**：62.87min 虽能跑完，但显存只剩 327MiB、RTF 0.73，叠加其它占用/多任务排队即可能 OOM；90min 门槛（5400s）明确不可取。
2. **守卫与超时的一致性风险**：当前产品 `ASR_TIMEOUT_SECONDS=1800`（30min）。62.87min 实测生成 2753.6s，**即使把守卫放宽到 3600s，50–60min 的会议也会先撞 1800s 超时**。二者必须成对调整：
   - 方案 A（推荐，最小改动）：守卫收窄到 **2700s（45min）**，使生成墙钟（RTF≈0.55–0.73 → 1485–1970s）基本落在 1800s 内；
   - 方案 B：守卫保持 3600s，但把 `ASR_TIMEOUT_SECONDS` 提到 **≥3000–3600s**（且接受单任务 25–45min 的墙钟与排队放大）。
3. **>60min 会议**：产品守卫拒绝 + 提示拆段，与 §48 结论一致；不要把 90min 作为上限放开。

---

## 2. 产品守卫测试（步骤②，零 GPU）

- 上传 `synthetic_concat_62min.mp3`（30,176,972 bytes）→ material_id=15，`POST /transcription/start/15`。
- 返回：**HTTP 400** `{"code":"400","msg":"音频时长 62 分钟，超过单次转写上限 1 小时（说话人分离在超长音频上不可靠）。请将录音拆分为多段后分别上传。"}`
- 结论：`reject_overlong_audio`（`ffprobe` 读时长 → 与 `TRANSCRIPTION_MAX_DURATION_SECONDS=3600` 比较）**按预期拒绝，未创建任务、未消耗 GPU**。

## 3. 直连容量测试（步骤③）

| 项 | 值 |
|---|---|
| 端点 | `http://asr-sidecar:9971/transcribe`（从 backend 容器内发起；与产品同容器网络） |
| 侧车 | compose `moss-asr-local`：`scripts/moss_asr_server.py` **v3**（auto max_new_tokens） |
| 请求参数 | `max_new_tokens=66496`（= int(3772.02×16)+6144；v3 自动规则同值），`max_length=131072`，客户端 timeout 3000s |
| 音频 | 3772.02s / 62.87min，单声道 16k mp3 |
| 输出 | **1013 段**，`last_end=3769.1s`，覆盖 **99.92%**，`truncated=false` |
| 推理墙钟 | **2753.6s**（RTF **0.730**） |
| GPU 采样 | 每 2s 采样 1293 点：峰值 **15,976 MiB @09:57:32**（相对本段起始 +1,933 MiB，但基线已含模型权重 14,043 MiB）；最大利用率 100%，最高 59°C |
| 稳定性 | **无 OOM、无 traceback、sidecar 容器 running、restarts=0、OOMKilled=false** |

### 客户端中断说明（诚实记录）

- 请求开始 **32s** 时，`docker exec` 客户端进程被 **SIGKILL（rc=137）**；日志/容器时间戳显示原因是 **backend 容器在 09:36:40 被外部重建**（`created=2026-10-23T01:36:40Z`，非本任务操作、非侧车问题）。
- 侧车**服务端生成未中断**，继续运行至完成（`transcribe done` 行完整），HTTP 端最终记录 200；客户端响应因此缺失，本报告数字全部取自 **sidecar 服务端日志**（`/tmp/moss_capacity_sidecar_final.log`）+ GPU 采样，未做任何推断。
- 采样器在客户端被杀后立即重启（09:38:18 起至结束），覆盖了显存峰值出现的全时段（峰值在 09:57:32）。

## 4. §48 判定

| # | 判定项 | 结果 | 依据 |
|---|---|---|---|
| ① | 无 OOM/进程崩溃 | **通过** | 无 OOM/traceback；容器 running、restarts=0 |
| ② | coverage ≥99% | **通过** | 99.92%（3769.1/3772.0s） |
| ③ | truncated=false | **通过** | sidecar `truncated=False`（覆盖率口径） |
| ④ | 记录可安全运行的时长 | **通过（附限制）** | 62.87min 可一次跑完，但 RTF 0.73、显存余量仅 327MiB → **不作为放开门槛的依据** |

> 说明：§48 判定“PASS”仅表示**容量上可行**；结合显存余量与墙钟，产品守卫仍应保持 45–60min 的保守配置（见 §1）。合成材料**不报任何准确率**，1013 段文本未做质量评估。

## 5. 限制

1. **合成材料**（两场拼接、无自然会议边界）：只验证容量，**不得**用于 cpCER/CER 或任何质量结论。
2. 单次运行、单卡 16GB；未做重复性/多场验证；峰值显存采样每 2s，可能漏掉更短瞬时峰值（实测最高 15,976MiB，距上限 ~327MiB）。
3. 客户端被外部中断，HTTP 响应缺失；结论依赖服务端日志（日志含完整 `transcribe done` 行与耗时/覆盖率，可信）。
4. 本次未改产品代码/.env/gate/LEDGER；侧车 v3（auto max_new_tokens）为 task-13 引入的引擎侧改动。

## 6. 复现命令

```bash
cd /home/song/code/Agent/media_agent/AIMeeting

# ① 合成材料（≈62.87min）
ffmpeg -y -v error -i evaluation/asr/first_test/hyp_R8007_M8011_avg.mp3 \
  -i evaluation/asr/first_test/hyp_R8009_M8020_avg.mp3 \
  -filter_complex "[0:a][1:a]concat=n=2:v=0:a=1[out]" -map "[out]" \
  -ac 1 -ar 16000 -b:a 64k \
  evaluation/asr/first_test/synthetic_concat_62min.mp3

# ② 产品守卫（>3600s 应 400 拒绝；零 GPU）
set -a; . meetingbot/fastapi-app/.env; set +a
meetingbot/fastapi-app/.venv/bin/python /tmp/asr_task14_guard.py

# ③ 直连容量测试（GPU ~46min；从 backend 容器发请求，GPU 每 2s 采样）
bash /tmp/asr_task14_capacity.sh     # 输出 /tmp/moss_capacity_*.log / *.json

# ④ 合并结果 + §48 判定
python3 /tmp/asr_task14_merge.py     # → evaluation/results/moss_long_audio_capacity.json
```

## 7. 产物

- `evaluation/results/moss_long_audio_capacity.json`（守卫/直连/GPU/判定全量数据）
- `evaluation/asr/first_test/synthetic_concat_62min.mp3`（62.87min 合成材料，30.2MB）
- 证据（临时 /tmp）：`moss_capacity_sidecar_final.log`（完成行）、`moss_capacity_gpu_samples.log`（1293 点采样）、`capacity_watch.out`
