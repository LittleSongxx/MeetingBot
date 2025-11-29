# P0-2 重复 fresh 运行 ×2 报告（run1+run2+run3，2026-10-23）

- **预注册**：`evaluation/LEDGER.md` §46（先于调用写死；判定线未改）。
- **一句话结论**：§46 三项判定为 **PASS / PASS / FAIL（边界）**，总判定 **FAIL → 不切换**：
  mixed100 主口径继续使用组装集，本次只登记方差结论，不改质量闸输入、不重冻基线。
- **对"faithfulness −4.19pp 是波动还是真实变差"的回答**：**主要是运行级波动，不是稳定的真实退化**。
  run2/run3 的 faithfulness 为 89.98% / 90.02%（≈组装集 89.21%），run1（85.02%）是低异常；
  三次均值 88.34%，比组装集低 **0.87pp**（在 2pp 非劣线内）。但 run1 并非单题随机抖动，
  而是**整次运行级别偏移**（逐题符号检验：27 个有差异 case 中 24 个 run1 更低，p≈4.9e-5），
  即单次读数在 faithfulness 上可出现 ~5pp 的运行级波动。
- **边界说明（必须披露）**：第 3 项极差的**精确计数**为 **5.000085pp**（532/591 − 505/594），
  比 5pp 线高 0.000085pp；按两位小数展示恰为 5.00pp。按"精确计数 + 不得事后放宽"判 **FAIL**
  （与 `quality_gate` 用未四舍五入 rate 比较 floor 的先例一致）。若把口径明确为"报告两位小数"，
  第 3 项将为 PASS、三项全过 → 应切换为三次均值——此口径分歧请 Lead/用户确认（见 §9）。
- **产物**：`results/eval100_fresh_answers_run{2,3}.json`、`..._packet_run{2,3}.json`、本报告。
  未改产品代码 / `.env` / quality_gate / baseline / LEDGER / 其它既有结果；未跑 GPU。

---

## 1. 目的与预注册

§46：run1（2026-10-22，§43b）faithfulness 85.02%，比组装集低 4.19pp，按 §43 判 FAIL；
但单次运行方差未知。再跑 2 次**同配置** fresh 运行，回答：
(1) 运行间方差有多大（四指标极差与 SD）；(2) 三运行均值是否足以替换组装集。

判定（先写死）：①三次运行事故率均 ≤5%；②四指标**均值**均 ≥组装集 −2pp 且 ≥ `FLOORS.mixed100`；
③faithfulness 三次极差 ≤5pp。三项全过 → 三次均值作为 mixed100 主口径；任一失败 → 保留组装集，只登记方差结论。
预算：2 ×（CHAT 抓取 ≤1500 + deepseek 判定 ≤260）；失败题不剔分母。

执行顺序：预检（health / 产品自报开关 / qa 集完整性 / run1 产物）→ run2 抓取→组包→判定 →
run3 抓取→组包→判定 → 汇总（零新增调用）。判定线在调用前已读入，未改。

---

## 2. 运行配置与 provenance

| 项 | 值 |
|---|---|
| 仓库 | `f14df00a4a7379755638a08e9387e48ca5c9101d`（branch `feat/frontier-agent`），工作树有未提交改动（评测/产品既有 WIP + 本轮产物） |
| Python | `evaluation/.venv/bin/python` 3.12.13 |
| QA 集 | `evaluation/results/qa_eval100_fresh.json`（100 题；sha256 `e8e1c39f…57bd`；与 run1 完全相同，未改） |
| 样本口径 | 每题新建 session、per-case scope、workers=4；产品默认配置，未按题调 prompt / 检索参数 |
| 判定 | `judges/rag.py --judge --provider deepseek --label a`，`deepseek-flash`、thinking=off、每 case 2 次调用 |
| 判定仪器 | `evaluation/judges/rag.py` 与 HEAD 零 diff（与 run1、组装集同一代码路径/提示词） |
| 产品开关 | 三次运行 `captured_from.switches` **逐字相同**：qwen3.8-max-0902、rerank 开（top_k=3 / candidates=24）、query plan / contextual / dedupe 关、chunk=300 |
| 环境变化 | 两轮之间产品后端重启过（MOSS 转录引擎切换）；`/chat/status` 自报 chat 配置三次完全相同，chat 路径不受转录引擎影响 |

三次运行的抓取/判定时间与读数规模：

| 运行 | 抓取起止（UTC） | 抓取墙钟 | 上下文段数 | 断言数 | 判定起止（UTC） | 判定墙钟 | 判定调用 |
|---|---|---|---|---|---|---|---|
| run1（§43b） | 10-22 10:15:00 → 10:18:35 | 3m35.2s | 357 | 594 | 10:18:45 → 10:24:05 | 5m19.7s | 200/260 |
| run2 | 10-23 01:14:50 → 01:18:23 | 3m32.8s | 374 | 599 | 01:18:29 → 01:24:11 | 5m42.1s | 200/260 |
| run3 | 10-23 01:24:16 → 01:27:49 | 3m33.0s | 356 | 591 | 01:27:54 → 01:33:38 | 5m43.9s | 200/260 |

---

## 3. 预算实耗

| 预算项（§46：每次运行独立预算） | 授权/预算 | 实耗 | 说明 |
|---|---|---|---|
| run2 CHAT 抓取 | ≤1500 | **194 次进入模型的提问**（第 1 次尝试 94 + 第 2 次 100） | 另有 429 被前置拒绝的重试请求（不进入模型、不计费）约 7+ 次，未单独计数；见 §4 |
| run2 deepseek 判定 | ≤260 | **200**（100 题 × 2，重试 0） | `calls_made=200` |
| run3 CHAT 抓取 | ≤1500 | **100 次进入模型的提问** | 另有 429 重试（重试成功，未单独计数） |
| run3 deepseek 判定 | ≤260 | **200**（100 题 × 2，重试 0） | `calls_made=200` |
| 合计（task 预算 3000 CHAT / 520 judge） | — | **294 CHAT / 400 judge** | 均未触顶；无"授权用完未判"的 case |

---

## 4. n、事故与失败

| 运行 | n | 抓取 turns | stop_reason | 空答案 | 抓取失败 | 运行事故率 | 判完 | 判定错误 |
|---|---|---|---|---|---|---|---|---|
| run1 | 100 | 100 | `{"finished":100}` | 0 | 0 | 0/100 = 0.0% | 100 | 0 |
| run2 | 100 | 100 | `{"finished":100}` | 0 | 0 | 0/100 = 0.0% | 100 | 0 |
| run3 | 100 | 100 | `{"finished":100}` | 0 | 0 | 0/100 = 0.0% | 100 | 0 |

**失败题清单：无（0 题）。** 四指标分母均无缺失 case（context_precision 100/100 题有判定上下文；
context_recall 100 题 / 167 条金标事实；faithfulness 100 题 / 591–599 条断言；answer_relevancy 100 题）。

**运行级事故（如实登记，不计入三次运行的事故率）**：run2 第 1 次抓取在 **94/100** 处被产品自身限流中断——
`POST /chat/stream` 返回 `HTTP 429 {"code":"429","msg":"提问过于频繁，请 1 秒后再试"}`（m095 持续 429，
harness 默认 6 次退避重试未过），已完成的 94 个 turn 未写入产物（harness 结束时才落盘）。
根因：产品 `CHAT_ASK_LIMIT_PER_USER=30` 次 / 60s 滑动窗口；第 1 次尝试速率 100 问 / 198.9s ≈ **30.2 次/分**，
刚好越过 30/分上限（run1 27.9 次/分、run3 28.2 次/分，均未触发）。
处置：**不改产品参数、不改 `.env`**，仅把抓取 harness 的重试预算 `--retries` 从默认 6 提到 20
（harness 自带 CLI 选项，非实验配置项），run2 重跑 100/100、run3 100/100。若把这次中断也视为"运行事故"，
则 4 次抓取尝试中 1 次运行级中断、题级失败 0；按 §46 的三次最终运行口径事故率为 0%。

---

## 5. 三次运行四指标（池化，§46 主结局）

| 指标 | run1 | run2 | run3 | **三次均值** | 极差 | SD(样本, n=3) | 组装集 | 均值−组装 | ≥组装−2pp | ≥floor |
|---|---|---|---|---|---|---|---|---|---|---|
| context_precision | 71.43% | 72.73% | 71.35% | **71.83%** | 1.38pp | 0.77pp | 71.83% | +0.00pp | 是 | 是（0.60） |
| context_recall | 89.22% | 90.42% | 90.42% | **90.02%** | 1.20pp | 0.69pp | 91.02% | −1.00pp | 是 | 是（0.85） |
| faithfulness | 85.02% | 89.98% | 90.02% | **88.34%** | **5.00pp**（精确 5.000085pp） | 2.88pp | 89.21% | −0.87pp | 是 | 是（0.80） |
| answer_relevancy | 99.00% | 99.00% | 99.00% | **99.00%** | 0.00pp | 0.00pp | 97.00% | +2.00pp | 是 | 是（0.90） |

各运行 pooled 明细（成功/分母，Clopper-Pearson 95% CI）：

| 指标 | run1 | run2 | run3 |
|---|---|---|---|
| context_precision | 255/357 = 71.43% [66.44,76.06] | 272/374 = 72.73% [67.91,77.18] | 254/356 = 71.35% [66.35,75.99] |
| context_recall | 149/167 = 89.22% [83.50,93.49] | 151/167 = 90.42% [84.91,94.42] | 151/167 = 90.42% [84.91,94.42] |
| faithfulness | 505/594 = 85.02% [81.89,87.79] | 539/599 = 89.98% [87.29,92.27] | 532/591 = 90.02% [87.31,92.31] |
| answer_relevancy | 99/100 = 99.00% [94.55,99.97] | 99/100 = 99.00% [94.55,99.97] | 99/100 = 99.00% [94.55,99.97] |

三次合并计数（仅供参考，不作判定）：context_precision 781/1087 = 71.85% [69.07,74.51]；
context_recall 451/501 = 90.02% [87.05,92.50]；faithfulness 1576/1784 = 88.34% [86.76,89.79]；
answer_relevancy 297/300 = 99.00% [97.11,99.79]。

per-case 均值（每运行、三次平均）：

| 指标 | run1 | run2 | run3 | 三次均值 | 组装集 per-case 均值 |
|---|---|---|---|---|---|
| context_precision | 75.07% | 75.09% | 74.49% | 74.88% | 74.87% |
| context_recall | 91.75% | 92.75% | 92.75% | 92.42% | 92.50% |
| faithfulness | 84.77% | 89.77% | 89.99% | 88.18% | 89.53% |
| answer_relevancy | 99.00% | 99.00% | 99.00% | 99.00% | 97.00% |

---

## 6. §46 判定（按预注册原文）

| 项 | 预注册判定线 | 本次读数 | 结论 |
|---|---|---|---|
| 1) 三次运行事故率 | 均 ≤5% | 三次均为 0/100 = 0.0%（run2 第 1 次尝试的运行级中断见 §4，未产生三次运行读数） | **PASS** |
| 2) 四指标均值非劣 | 均值均 ≥ 组装集 −2pp，且 ≥ `FLOORS.mixed100` | cp +0.00pp ✓、cr −1.00pp ✓、faithfulness −0.87pp ✓、ar +2.00pp ✓；四项均 ≥ floor ✓ | **PASS** |
| 3) faithfulness 三次极差 | ≤5pp | **5.000085pp**（85.02 / 89.98 / 90.02；精确计数），按两位小数展示为 5.00pp | **FAIL（边界）** |

**总判定：FAIL → 不切换**（§46："任一失败 → 保留组装集，只登记方差结论"）。因此本次**没有**执行
mixed100 主口径切换到三次均值、没有重冻基线、没有改质量闸输入。

### 6.1 边界敏感性（必须披露，供 Lead/用户确认口径）

第 3 项是**刀锋边界**，判定取决于极差的计算精度，而 §46 未写明精度：

- **精确计数口径（本报告采用的判定）**：极差 = 532/591 − 505/594 = 5851/117018 = **5.0000854569pp** > 5pp → **FAIL**。
  依据：仓库 `quality_gate.py` 的 floor/容差比较一直用**未四舍五入的 rate**（`float(summary[rate])`），
  预注册纪律要求"不得看到结果后放宽判定线"，故按精确计数从严判 FAIL。
- **报告两位小数口径**：85.02% / 89.98% / 90.02% → 极差 90.02 − 85.02 = **5.00pp ≤ 5pp** → PASS；
  此时三项全过 → 应把三次均值（faithfulness 88.34%）作为 mixed100 主口径并重冻基线。

两种口径的差异只有 **0.000085pp**。本报告的结论按精确口径给 FAIL（不切换）；若 Lead/用户认定
"报告展示精度"即判定精度，则第 3 项应改判 PASS，切换动作为三次均值（由 Lead 执行）。

---

## 7. 方差结论：run1 的 −4.19pp 是波动还是真实变差？

**结论：主要是运行级波动，不是稳定的真实退化；但波动幅度不容忽视（~5pp）。**

1. **faithfulness**：run1 85.02% ⇒ run2 89.98% ⇒ run3 90.02%。run2 与 run3 几乎相同（差 0.03pp），
   run1 比二者低 ≈5.0pp；三次均值 88.34% 仅比组装集 89.21% 低 0.87pp（在预注册 2pp 非劣线内）。
   → 单次 run1 的 −4.19pp 主要是**那一次运行偏低**，不能读成产品在 faithfulness 上稳定退化。
2. **run1 是运行级偏移而非单题噪声**：100 题中 faithfulness 逐题有差异的 case 27 个，
   其中 **24 个 run1 低于 run2 与 run3 两者、仅 3 个高于两者**（符号检验双侧 p ≈ **4.9e-5**）；
   run1 比 run2/run3 均值低 >20pp 的 case 有 14 个，反向为 0 个。
   → 更像"整次运行的抽取/判定环境整体偏移"，不是少数题抖动。可能来源：产品生成采样（qwen 非确定）、
   判定服务端行为；本仓库无法在单次运行内区分两者，只能如实记为运行间方差。
3. **其余三指标稳定**：context_precision 极差 1.38pp、context_recall 1.20pp、answer_relevancy 0.00pp。
   波动集中在 faithfulness（SD 2.88pp），与 run1 的异常方向一致。
4. **逐题波动本身很大**：faithfulness 逐 case 跨三次运行的极差均值 12.90pp、中位 9.55pp、P90 30.00pp；
   极差 >10pp 的 48 题、>20pp 的 28 题（每 case 断言数少，一条 supported 翻转就是 20–50pp）。
   但池化到 100 题后运行级 SD 降到 2.88pp——单次读数仍需按 ~5pp 级别的运行波动理解。
5. **recall-v2 三次完全一致**（见 §8）：149/158、44/50、23/23；三次之间唯一变化的 2 条找到事实
   （m019 / cs09 的 fact 0、1）在 `fact_classes.json` 里是 `metadata`、被 recall-v2 排除，
   因此"完全一致"是真实的，不是计算错误。

---

## 8. recall-v2 辅助（零新增调用，复用各运行既有判定）

| 运行 | mixed100 | cross30 | new_meeting |
|---|---|---|---|
| run1 | 94.30%（149/158，excluded 9） | 88.00%（44/50，excluded 8） | 100.00%（23/23，excluded 0） |
| run2 | 94.30%（149/158） | 88.00%（44/50） | 100.00%（23/23） |
| run3 | 94.30%（149/158） | 88.00%（44/50） | 100.00%（23/23） |

冻结值：mixed100 96.20%、cross30 88.00%、new_meeting 86.96%；floor 0.92 / 0.85 / 0.82。
三次均 ≥ floor，且 recall-v2 对运行不敏感（唯一变化的 2 条事实属 metadata 排除类）。
注：§46 未把 recall-v2 列入三项判定，本节只作辅助对照。

---

## 9. 与组装集对照（§46 第 2 项明细）

组装集（`results/eval100_scope_fixed_packet.json`，同一 100 题、同判定仪器）：71.83% / 91.02% / 89.21% / 97.00%。

| 指标 | 三次均值 | 组装集 | 差值 | ≥组装−2pp | floor | ≥floor |
|---|---|---|---|---|---|---|
| context_precision | 71.83% | 71.83% | +0.00pp | 是 | 0.60 | 是 |
| context_recall | 90.02% | 91.02% | −1.00pp | 是 | 0.85 | 是 |
| faithfulness | 88.34% | 89.21% | −0.87pp | 是 | 0.80 | 是 |
| answer_relevancy | 99.00% | 97.00% | +2.00pp | 是 | 0.90 | 是 |

说明：三次均值是比任一次单次读数更稳的点估计（faithfulness 88.34% vs 单次 85.02–90.02%）；
但它与组装集的差（−0.87pp）不含组装集自身的旧口径/历史拼装不确定性（见 §10）。

---

## 10. 限制（如实登记）

1. **单裁判**：四指标全部来自 LLM 判定（deepseek-flash，label=a），不是人类金标；本次未做跨族复判
   （§44 claim 锚点另案）。判定代码与提示词零 diff，但供应商模型版本的**服务端更新无法锁定**，
   若 run1（10-22）与 run2/3（10-23）之间发生服务端变化，会被计入"运行间方差"而无法识别。
2. **三次运行的方差估计很粗**：n=3，SD/极差本身不确定性大；只能回答"波动大约 5pp 量级"，
   不能给出可靠置信区间。运行间差异无法分解为"产品生成采样 / 判定非确定性 / 环境漂移"。
3. **时间与环境不完全同质**：run1 在 10-22 晚，run2/3 在 10-23 上午；产品后端在两次之间重启过
   （MOSS 转录引擎切换，chat 自报开关三次逐字相同，但不排除其他运行时状态差异）。
4. **组装集仍是旧口径对照**：六份历史 answers 拼装（2026-10-20~22）+ series 重跑，部分来源缺开关 provenance；
   三次均值与它的比较不是受控 A/B。§46 仍以它为预注册对照线，判定线未改。
5. **产品侧环境异常（不影响本次指标读数，但影响"付费调用预算"记账）**：
   - 产品 chat 限流 `30 次/60s/用户` 使 run2 第 1 次尝试在 94/100 处中断（§4），已按 harness 重试解决；
   - 产品每日 token 配额账本键 `quota:chat-tokens:1` 在 run2 前已到 **498,752 / 500,000**（剩余 1,248）；
     观察到的行为是：超限的 spend 不累加（计数器冻结），前置闸因 `remaining=1,248>0` 继续放行——
     即本次两轮抓取**未被产品侧 token 账本记账**。该行为不改变回答内容与指标（仅预算记账），
     但属产品侧会计异常，建议环境属主核查（是否重置/调大 `CHAT_DAILY_TOKEN_BUDGET`）后再做更多付费运行。
     本次未改产品代码/`.env`/Redis 状态。
6. **口径**：context_precision 分母为检索到的全部切片（RAGAS 式）；answer_relevancy 为二值"是否正面回答"，
   与 RAGAS 嵌入版不同；四指标不合成总分（评测宪法第 6 条）；样本无可答题，拒答类指标未测。

---

## 11. 复现命令

```bash
cd /home/song/code/Agent/media_agent/AIMeeting

# run2 / run3 抓取（同一命令，仅输出文件名不同；--retries 20 仅为 429 退避预算）
set -a; . meetingbot/fastapi-app/.env; set +a
meetingbot/fastapi-app/.venv/bin/python evaluation/benchmarks/ask_fresh_sessions.py \
  --qa-set evaluation/results/qa_eval100_fresh.json \
  --answers evaluation/results/eval100_fresh_answers_run2.json \
  --base-url http://127.0.0.1:19091 --workers 4 --retries 20

# 组包（零调用）
evaluation/.venv/bin/python evaluation/judges/rag.py --build \
  --qa-set evaluation/results/qa_eval100_fresh.json \
  --answers evaluation/results/eval100_fresh_answers_run2.json \
  --packet evaluation/results/eval100_fresh_packet_run2.json

# 判定（每次授权 260，实耗 200）
set -a; . meetingbot/fastapi-app/.env; set +a
evaluation/.venv/bin/python evaluation/judges/rag.py --judge \
  --packet evaluation/results/eval100_fresh_packet_run2.json \
  --qa-set evaluation/results/qa_eval100_fresh.json \
  --answers evaluation/results/eval100_fresh_answers_run2.json \
  --provider deepseek --label a --authorized-llm-calls 260

# run3 同上，替换 run2→run3。
# 汇总（零新增调用）：四指标用 judges/rag.py 的 summarize()；
# 均值/极差/SD 用 statistics（样本 SD, ddof=1）；recall-v2 用 lib/recall_quality.retrievable_recall()。
# 汇总脚本本次为临时脚本（/tmp），未写入仓库。
```

---

## 12. 本次改动清单与门禁

**新增（仅 5 个文件，与任务授权 write scope 一致）**：

- `evaluation/results/eval100_fresh_answers_run2.json`（100 turns）
- `evaluation/results/eval100_fresh_packet_run2.json`（100 case，含 judgement + summary）
- `evaluation/results/eval100_fresh_answers_run3.json`（100 turns）
- `evaluation/results/eval100_fresh_packet_run3.json`（100 case，含 judgement + summary）
- `evaluation/reports/2026-10-23_eval100_fresh_repeat.md`（本报告）

**未改**：run1 产物、产品代码、`.env`、`quality_gate.py`、`quality_gate_baseline.json`、`LEDGER.md`、其它既有结果。
**未执行**：mixed100 输入切换、基线重冻（因总判定 FAIL 不切换；切换动作由 Lead 决定）。
产物中无 API key；未跑 GPU/MOSS；两次抓取均只走本机 19091 产品接口，判定只走 deepseek 端点。

---

## 附录 A：faithfulness 逐 case 跨三次运行极差最大的题

| case | source_set | run1 | run2 | run3 | 极差 |
|---|---|---|---|---|---|
| m025 | cross30 | 0.43 | 1.00 | 0.38 | 62.5pp |
| m016 | cross30 | 0.12 | 0.25 | 0.71 | 58.9pp |
| m018 | cross30 | 0.40 | 0.80 | 0.22 | 57.8pp |
| m010 | cross30 | 0.44 | 1.00 | 1.00 | 55.6pp |
| m019 | cross30 | 0.40 | 0.80 | 0.86 | 45.7pp |
| m015 | cross30 | 0.25 | 0.67 | 0.60 | 41.7pp |
| m036 | holdoutA | 0.60 | 0.89 | 1.00 | 40.0pp |
| m071 | newmeeting | 0.60 | 1.00 | 1.00 | 40.0pp |
| m047 | holdoutA | 0.67 | 1.00 | 1.00 | 33.3pp |
| m062 | holdoutD | 1.00 | 0.67 | 1.00 | 33.3pp |
| m012 | cross30 | 0.50 | 0.80 | 0.75 | 30.0pp |
| m045 | holdoutA | 0.71 | 1.00 | 0.83 | 28.6pp |

## 附录 B：run2 首次尝试的 429 事故记录

- 起止：2026-10-23T01:10:56Z 开始，墙钟 3m18.9s 后在 **m095** 处中断（m001–m094 已打印、未落盘）。
- 错误：`RuntimeError: 提问失败 HTTP 429: {"code":"429","msg":"提问过于频繁，请 1 秒后再试"}`（harness stderr）。
- 重试：harness 默认 `--retries 6`，退避 1+2+3+4+5+6s 合计约 21s，不足以跨过产品 60s 滑动窗口 → 中断。
- 产品参数（只读核对）：`CHAT_ASK_LIMIT_PER_USER=30`、`CHAT_ASK_WINDOW_SECONDS=60`（`api/chat.py` 默认值，
  `.env` 未覆盖）；第 1 次尝试速率 ≈30.2 次/分 > 30 次/分上限。
- 处置：同一命令加 `--retries 20` 重跑，100/100 成功；未改产品代码/`.env`/限流参数。