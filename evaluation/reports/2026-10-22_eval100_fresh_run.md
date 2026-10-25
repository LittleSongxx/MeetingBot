# P0-2 单次 100 题 fresh 运行报告（2026-10-22）

- **预注册**：`evaluation/LEDGER.md` §43（先于调用写死；判定线未改）。
- **结论**：**§43 三项判定为 PASS / FAIL / PASS，总判定 FAIL → 不切换质量闸 `mixed100` 输入，保留组装集**，
  如实登记"单次运行在 faithfulness 上更差（−4.19pp，超过预注册的 2pp 非劣线）"。
- **一句话读数**：100/100 题抓取与判定完成、运行事故率 0%；四指标 pooled 为
  context_precision 71.43%、context_recall 89.22%、faithfulness 85.02%、answer_relevancy 99.00%。
  四指标中 faithfulness 低于组装集读数 4.19pp，其余三项在 −2pp 内或更好；四项仍高于 `FLOORS.mixed100` 绝对下限。
- **预算**：抓取 100/1500 CHAT 提问、判定 200/260 次调用；均未触顶，无截断。
- **产物**：`evaluation/results/eval100_fresh_answers.json`（本次新增）、
  `evaluation/results/eval100_fresh_packet.json`（本次新增，含判定与 summary）、本报告。
  未改产品代码 / `.env` / quality_gate / baseline / LEDGER / 其它结果；未跑 GPU。

---

## 1. 预注册与执行顺序

§43（LEDGER 第 2628 行起）在**本次调用之前**已写死：目的、样本与口径、主结局、三项判定线、预算与限制、
产物清单。"不得在看到结果后改判定线"——本报告只按已写死的三项给结论，不改线、不换主指标。

执行顺序（与任务书一致）：

1. 预检：`curl /health` → `{"status":"ok"}`；`qa_eval100_fresh.json` 100 题、id 唯一；
   `.env` 凭据注入（`set -a; . meetingbot/fastapi-app/.env; set +a`；抓取侧 harness 自读 admin 凭据）。
   先跑 2 题冒烟（m001/m090，验证登录 / SSE / `/chat/messages` 落库 / `retrieved` 形状），冒烟产物只落 `/tmp`，
   正式抓取重新新建 session，不复用。
2. 抓取（每题新建 session，per-case scope，workers=4）。
3. 组包（零调用）。
4. 判定（deepseek，label=a，授权 260 次）。
5. 汇总（本报告：零新增调用；四指标 pooled + per-case 均值 + Clopper-Pearson 95% CI +
   来源分层 + recall-v2 + §43 三项判定）。

---

## 2. 运行配置与 provenance

| 项 | 值 |
|---|---|
| 仓库 | `f14df00a4a7379755638a08e9387e48ca5c9101d`（branch `feat/frontier-agent`），**工作树有 56 个未提交改动**（见 §10 限制） |
| Python | `evaluation/.venv/bin/python` 与 `meetingbot/fastapi-app/.venv/bin/python` 均 3.12.13 |
| 被测产品 | `http://127.0.0.1:19091`，`/health` = ok；抓取时产品 `GET /chat/status` 自报开关（见下） |
| 抓取方式 | `evaluation/benchmarks/ask_fresh_sessions.py`，`mode=fresh_session_per_case`，`--workers 4`，每题新建 session（100 个不同 session_id） |
| 抓取时间 | 2026-10-22T10:15:00Z → 10:18:35Z（Asia/Shanghai 18:15:00 → 18:18:35），墙钟 3m35s |
| QA 集 | `evaluation/results/qa_eval100_fresh.json`：100 题，`label_source=mixed_existing_golds`，`human_checked=false`，`discipline=单次运行、每题新建 session、per-case scope；与组装集不可混算` |
| 题源构成 | cross30 30 / holdoutA 20 / holdoutD 20 / newmeeting 15 / holdoutB 15；不可答题 0；金标事实 167 条；90 题限定单会议、10 题限定 series |
| 判定 | `evaluation/judges/rag.py --judge --provider deepseek --label a`；实测 `model=deepseek-flash`、`thinking=off`、每 case 2 次调用（context + answer）、每次 `total_deadline=240s` |
| 判定仪器版本 | 判定代码 `evaluation/judges/rag.py` 与 HEAD **零 diff**（文件 mtime 2026-10-18；`git log` 最后改动 2026-10-19），即与组装集判定是同一条代码路径、同一提示词 |
| 判定时间 | 2026-10-22T10:18:45Z → 10:24:05Z，墙钟 5m19.7s |

产品自报开关（`captured_from.switches`，随 answers 落盘）：

```json
{"model": "qwen3.8-max-0902", "kb_rerank_enabled": true, "kb_query_plan_enabled": false,
 "kb_chunk_target_chars": 300, "rerank_top_k": 3, "rerank_candidates": 24,
 "rerank_input_price_micro_per_1k": 500, "kb_dedupe_enabled": false,
 "kb_dedupe_overlap_seconds": 30, "kb_contextual_enabled": false,
 "pg_kb_table": "kb.chunk_search", "vector_dim": null}
```

即：qwen3.8-max CHAT、rerank 开（top_k=3 / candidates=24）、query plan / contextual / dedupe 均关、chunk=300。
这是产品**默认配置**，未按题调参、未改检索开关（§43 要求）。

---

## 3. 预算实耗

| 预算项 | 授权/预算 | 实耗 | 占比 | 说明 |
|---|---|---|---|---|
| CHAT 抓取（`POST /chat/stream` 提问） | ≤1500 | **100**（+2 题预检冒烟，未计入正式产物） | 6.7% | 每题 1 次提问；另每题 1 次 `POST /chat/session`（建会话，不产生模型调用），共 100 个会话 |
| 判定 LLM 调用 | ≤260 | **200**（100 题 × 2；`calls_made=200`） | 76.9% | 重试 0 次；无"授权用完未判"的 case |
| 截断 | — | **无** | — | 100/100 全部抓取并判定，无需按已完成题数截断 |

429/限流：harness 对 429/「频繁」内置最多 6 次退避重试（`--retries 6`）；本次运行无报错、无重试耗尽，
100 题全部产出。harness 不单独计数"发生过的 429"，因此不能给出 429 次数（如实说明，不编造）。

---

## 4. n、失败与空答案

| 审计项 | 结果 |
|---|---|
| n（分母） | **100**，不剔除任何题 |
| 抓取 turn 数 | 100（id 唯一 100，session 唯一 100） |
| stop_reason 分布 | `{"finished": 100}`；`stalled` = 0 |
| 空答案 | **0** |
| 抓取失败（无 turn / 未落库） | **0** |
| 判定覆盖 | 100/100 case 有 judgement；`context_error` 0、`answer_error` 0 |
| 四指标分母缺失 case | 0（context_precision 100 题/357 段上下文；context_recall 100/167 条事实；faithfulness 100/594 条断言；answer_relevancy 100 题） |
| **运行事故率（§43 第 1 项）** | **0/100 = 0.0%**（阈值 ≤5%）→ **PASS** |

**失败题清单：无（0 题）。** 另记一处与指标无关的产物瑕疵：m016 的回答正文引用了 `[4]`，但该消息的引用快照为空
（`unknown_refs=[4]`，即页面上的死链探针命中 1 次）；该题的上下文/断言判定仍按落库 `retrieved` 完成，
不影响分母，也不从任何指标剔除。其余 99 题 `unknown_refs` 为空。

工具轨迹：本次 100 题共 155 次工具调用（search_knowledge 106、query_meeting_state 29、list_facts 12、
get_minutes 7、get_transcript_excerpt 1），工具级错误 0。回答正文合计 37,889 字符。

---

## 5. 四指标：pooled（主结局）/ per-case 均值 / CP 95% CI

§43 主结局是**四指标 pooled**；下表 pooled 为判定依据，per-case 均值只作补充（两者不等权，不做判定）。
区间为 Clopper-Pearson 精确 95% CI（仓库 `lib.stats.clopper_pearson_interval`）。

| 指标 | pooled (成功/分母) | pooled rate | CP 95% CI | per-case 均值 (n=100) | 组装集 pooled | 差值 | ≥组装−2pp | ≥FLOORS.mixed100 |
|---|---|---|---|---|---|---|---|---|
| context_precision | 255/357 | **71.43%** | [66.44%, 76.06%] | 75.07% | 71.83% | **−0.40pp** | 是 | 是（floor 0.60） |
| context_recall | 149/167 | **89.22%** | [83.50%, 93.49%] | 91.75% | 91.02% | **−1.80pp** | 是 | 是（floor 0.85） |
| faithfulness | 505/594 | **85.02%** | [81.89%, 87.79%] | 84.77% | 89.21% | **−4.19pp** | **否** | 是（floor 0.80） |
| answer_relevancy | 99/100 | **99.00%** | [94.55%, 99.97%] | 99.00% | 97.00% | **+2.00pp** | 是 | 是（floor 0.90） |

组装集读数取自 `evaluation/results/eval100_scope_fixed_packet.json` 的 `summary`（同一判定仪器、同一 label=a）。
组装集 CP 95% CI 供对照：context_precision [66.84%, 76.45%]、context_recall [85.62%, 94.89%]、
faithfulness [86.43%, 91.59%]、answer_relevancy [91.48%, 99.38%]。

**faithfulness 的次口径（不用于判定）**：内容级（剔除出处说明类元陈述）461/522 = **88.31%**，
组装集 468/519 = 90.17%，差 **−1.86pp**（在 2pp 内）；但 §43 主结局写的是全部断言口径的 faithfulness，
不能事后换成内容级口径来"过线"。元陈述 72 条（组装集 74 条），提示"出处说明类句子多/少"不是本次差异主因。

---

## 6. 来源分层（题源分层四指标，零新增调用）

按 case 的 `source_set`（QA 集自带字段）分层，**不另调模型**；每层 pooled：

| source_set | n | context_precision | context_recall | faithfulness | answer_relevancy |
|---|---|---|---|---|---|
| cross30 | 30 | 60.58% (83/137) | 75.86% (44/58) | 79.06% (185/234) | 96.67% (29/30) |
| holdoutA | 20 | 65.22% (45/69) | 100.00% (32/32) | 89.72% (96/107) | 100.00% (20/20) |
| holdoutB | 15 | 84.44% (38/45) | 95.24% (20/21) | 95.52% (64/67) | 100.00% (15/15) |
| holdoutD | 20 | 73.33% (44/60) | 90.91% (30/33) | 88.79% (95/107) | 100.00% (20/20) |
| newmeeting | 15 | 97.83% (45/46) | 100.00% (23/23) | 82.28% (65/79) | 100.00% (15/15) |

同分层与组装集的差值（fresh − assembled，pp；正数=fresh 更好）：

| source_set | n | context_precision | context_recall | faithfulness | answer_relevancy |
|---|---|---|---|---|---|
| cross30 | 30 | −6.08 | −5.17 | −5.41 | ±0.00 |
| holdoutA | 20 | +4.56 | +3.12 | −0.11 | +5.00 |
| holdoutB | 15 | +8.36 | ±0.00 | −1.03 | +6.67 |
| holdoutD | 20 | −0.44 | −3.03 | **−9.90** | ±0.00 |
| newmeeting | 15 | +3.95 | ±0.00 | **−7.33** | ±0.00 |

读法：faithfulness 的下降是**分散的**（100 题里 34 题变差 / 18 题变好 / 48 题持平，逐题均值 −4.76pp），
不是单一来源的定向回归；降幅最大的是 holdoutD 与 newmeeting，而 holdoutA/B 基本持平。
context_precision 呈正负混合（cross30 降 6.08pp、holdoutB 升 8.36pp）；context_recall 仅 5 题有变化。

---

## 7. recall-v2（§43 第 3 项；零新增调用，复用本次判定）

`recall-v2` 的计算只依赖 packet 里已有的判定（`context.facts[].found`）+ 冻结清单
`evaluation/gold/fact_classes.json`，**不需要新模型调用**。因此第 3 项直接用预注册的真实口径计算
（cross30 / 新会议子层），任务书允许的"题源分层四指标"另见 §6——两者都报，判定线不变。

| scope | 清单集合 | 成功/分母 | rate | excluded | 冻结值 | floor | 判定 |
|---|---|---|---|---|---|---|---|
| mixed100（整体，参考） | eval_100_mixed | 149/158 | **94.30%** | 9 | 96.20% | 0.92 | PASS（高于 floor；低于冻结 −1.90pp，仍在 2pp 容差内） |
| **cross30 子层** | cross_30 | 44/50 | **88.00%** | 8 | 88.00% | 0.85 | **PASS** |
| **new_meeting 子层** | new_meeting_15 | 23/23 | **100.00%** | 0 | 86.96% | 0.82 | **PASS** |

- 子层分母与冻结读数**完全相同**（cross30 50 条、new_meeting 23 条、mixed100 158 条）→ 是同一清单、
  同一"可检索口径"下的干净对照；cross30 持平（88.00%）、new_meeting 从 86.96% 升到 100%（23/23）。
- 第 3 项只要求 cross30 / 新会议子层不跌破对应下限 → **PASS**。
- 整体 mixed100 recall-v2 94.30%，比冻结 96.20% 低 1.90pp（未跌破 floor 0.92，也未超 2pp 容差）；
  这不是第 3 项的判定对象，但作为对照如实登记。

---

## 8. 与组装集对照的方法与结果

**对照物**：`evaluation/results/eval100_scope_fixed_packet.json`（旧口径组装集，mtime 2026-10-22 15:18 本地；
`adjudicator_disclosure` = deepseek-flash/provider deepseek，与本次同仪器同 label=a）。

**可比性核查**（同题、同金标、近同题量）：

- 100 个 case_id 完全相同；**100/100 题 question 文本相同、100/100 题 expected_facts 完全相同** → 逐题配对可行；
- 题目结构近乎同量：检索上下文 357 段（组装集 355 段）、金标事实 167 条（组装集 167 条）、
  断言 594 条（组装集 593 条）、answer_relevancy 分母 100（组装集 100）；
- 组装集不是单次运行：其 answers 的 `mode = "assembled + series-scope rerun"`，由 cross30_fresh / holdout /
  hb_floor0 / hd / he_fix_off / new_meeting_scoped 六份历史 answers 拼装，来源 captured_at 跨 2026-10-20T01:34Z–10-22T06:32Z，
  并对 10 道 series 题重跑过一次（详见 §9 限制）。

**逐题配对变化**（per-case 分数 fresh − assembled）：

| 指标 | 配对 n | fresh 更好 | 持平 | fresh 更差 | 逐题差值均值 |
|---|---|---|---|---|---|
| context_precision | 100 | 10 | 77 | 13 | +0.20pp |
| context_recall | 100 | 2 | 95 | 3 | −0.75pp |
| faithfulness | 100 | 18 | 48 | **34** | **−4.76pp** |
| answer_relevancy | 100 | 3 | 96 | 1 | +2.00pp |

**判定层面的比较结果**：context_recall（−1.80pp）最接近非劣线但未越线；faithfulness **−4.19pp 越线**；
context_precision、answer_relevancy 均非劣。四项**都高于** `quality_gate.FLOORS.mixed100`
（0.60/0.85/0.80/0.90），即"绝对下限合格、但相对组装集在 faithfulness 上非劣失败"。

---

## 9. §43 判定（三项，按预注册原文）

| 项 | 预注册判定线 | 本次读数 | 结论 |
|---|---|---|---|
| 1) 运行事故率 | ≤5%（空答案 / `stop_reason=stalled` / 抓取失败） | 0/100 = 0.0% | **PASS** |
| 2) 四指标非劣 | 四指标均 ≥ 组装集读数 −2pp，且 ≥ `FLOORS.mixed100` | cp −0.40pp ✓；cr −1.80pp ✓；**faithfulness −4.19pp ✗**；ar +2.00pp ✓；四项均 ≥ floor ✓ | **FAIL** |
| 3) recall-v2 子层 | cross30 / 新会议子层不跌破对应下限 | cross30 88.00% ≥ 0.85；new_meeting 100.00% ≥ 0.82 | **PASS** |

**总判定：FAIL → 保留组装集**（§43："任一项失败 → 保留组装集并如实登记'单次运行更差/不可用'"）。

因此本次**没有**执行：把 quality_gate 的 `mixed100` 输入切换为单次 packet、重冻基线、改仪器版本号。
组装集继续作为 mixed100 的主口径；本单次 packet 作为历史对照与后续复跑起点保留。

说明：第 2 项失败只发生在 faithfulness（−4.19pp）；其余三项与两项绝对/相对判定均过。
按预注册纪律，不因"只有一项"而放宽判定线，也不把主指标换成内容级口径（内容级 −1.86pp 在 2pp 内，但非主结局）。

---

## 10. 限制（如实登记）

1. **单裁判**：四指标全部来自 LLM 判定（deepseek-flash，label=a），不是人类金标（`not_gold`）；
   本次未做第二裁判族交叉复核（§44 的 claim 锚点另案进行）。判定仪器与组装集相同（代码零 diff）降低"换仪器"风险，
   但供应商模型版本可能服务端更新，无法在本仓库内锁定。
2. **单次运行**：n=100 题只跑了一次，没有重复运行，因此 −4.19pp 是"实际差值"，无法进一步分解为
   "真实退化 / 运行间波动 / 产品代码或配置漂移"。逐题配对显示变差 34 题、变好 18 题，呈现明显的逐题波动；
   本报告不把单次差值解释为"产品在 faithfulness 上确定性退化"。若要区分波动与真实差异，需要另行预注册重复运行。
3. **组装集仍是旧口径对照**：组装集不是同日同构建的单次运行（六份历史 answers 拼装 + series 重跑），
   其中 holdout 来源产物（2026-10-20 采集）缺 `captured_from` 开关 provenance；而单次运行用的是当前工作树产品
   （HEAD `f14df00` + 56 个未提交改动）。两者差值包含构建/日期/配置漂移，**不是受控 A/B**。
   预注册仍以这套历史读数为对照线（判定线不改），故本次 FAIL 结论有效，但原因归属应保守解读。
4. **口径**：context_precision 的分母是"检索到的全部切片"（含未引用，RAGAS 式），不是引用集；
   answer_relevancy 是"正面回答是否为是"的二值判定，与 RAGAS 嵌入版口径不同；四指标不合成总分（评测宪法第 6 条）。
5. **样本**：100 题全部来自既有 mixed 金标（无不可答题），不可答题的拒答率/硬凑率本运行未测。
6. **预算与覆盖**：判定只做了主判 label=a；未做跨族复判、未做 claim 级人工锚点；抓取侧无 429 计数。

---

## 11. 复现命令

```bash
cd /home/song/code/Agent/media_agent/AIMeeting

# 1) 抓取（100 题，每题新建 session，4 并发）
set -a; . meetingbot/fastapi-app/.env; set +a
meetingbot/fastapi-app/.venv/bin/python evaluation/benchmarks/ask_fresh_sessions.py \
  --qa-set evaluation/results/qa_eval100_fresh.json \
  --answers evaluation/results/eval100_fresh_answers.json \
  --base-url http://127.0.0.1:19091 --workers 4

# 2) 组包（零调用）
evaluation/.venv/bin/python evaluation/judges/rag.py --build \
  --qa-set evaluation/results/qa_eval100_fresh.json \
  --answers evaluation/results/eval100_fresh_answers.json \
  --packet evaluation/results/eval100_fresh_packet.json

# 3) 判定（授权 260 次；本次实耗 200）
set -a; . meetingbot/fastapi-app/.env; set +a
evaluation/.venv/bin/python evaluation/judges/rag.py --judge \
  --packet evaluation/results/eval100_fresh_packet.json \
  --qa-set evaluation/results/qa_eval100_fresh.json \
  --answers evaluation/results/eval100_fresh_answers.json \
  --provider deepseek --label a --authorized-llm-calls 260

# 4) 汇总（零新增调用）：pooled/CI 用 evaluation/judges/rag.py 的 summarize()，
#    recall-v2 用 evaluation/lib/recall_quality.py 的 retrievable_recall()；
#    本次汇总脚本为临时脚本（/tmp），未写入仓库。
```

---

## 12. 本次改动清单与门禁

**新增（仅 3 个文件，与任务授权一致）**：

- `evaluation/results/eval100_fresh_answers.json`（100 turns，`captured_from` 含 base_url/mode/captured_at/switches）
- `evaluation/results/eval100_fresh_packet.json`（100 case，含 judgement + summary；`adjudicator_disclosure` 已写）
- `evaluation/reports/2026-10-22_eval100_fresh_run.md`（本报告）

**未改**：产品代码、`meetingbot/fastapi-app/.env`、`evaluation/quality_gate.py`、`evaluation/baselines/quality_gate_baseline.json`、
`evaluation/LEDGER.md`、其它既有结果。**未执行**：质量闸 mixed100 输入切换、基线重冻、`make quality-gate` 重跑（无需，因判定 FAIL 不切换）。
产物中无 API key（已 `grep 'sk-'` 检查为 0 命中）；抓取与判定均只走本机 19091 产品接口与 deepseek 判定端点，未跑 GPU/MOSS。

---

## 附录 A：faithfulness 变化最大的题（配对，fresh − assembled）

| case | source | fresh | assembled | Δ(pp) |
|---|---|---|---|---|
| m016 | cross30 | 0.12 | 0.83 | −70.8 |
| m010 | cross30 | 0.44 | 0.88 | −43.1 |
| m036 | holdoutA | 0.60 | 1.00 | −40.0 |
| m065 | holdoutD | 0.60 | 1.00 | −40.0 |
| m071 | newmeeting | 0.60 | 1.00 | −40.0 |
| m044 | holdoutA | 0.40 | 0.75 | −35.0 |
| m007 | cross30 | 0.67 | 1.00 | −33.3 |
| m067 | holdoutD | 0.67 | 1.00 | −33.3 |
| m081 | newmeeting | 0.50 | 0.78 | −27.8 |
| m011 | cross30 | 0.25 | 0.50 | −25.0 |
| m012 | cross30 | 0.50 | 0.75 | −25.0 |
| m064 | holdoutD | 0.75 | 1.00 | −25.0 |

变好幅度最大：m042（holdoutA，0.20→1.00，+80.0pp）、m026（cross30，0.60→1.00，+40.0pp）、
m084（newmeeting，0.75→1.00，+25.0pp）、m053（holdoutD，0.75→1.00，+25.0pp）、m019（cross30，0.17→0.40，+23.3pp）。

逐题数值可从 `evaluation/results/eval100_fresh_packet.json` 的 `summary.per_case` 与
`evaluation/results/eval100_scope_fixed_packet.json` 的 `summary.per_case` 按 case_id 对齐复核。