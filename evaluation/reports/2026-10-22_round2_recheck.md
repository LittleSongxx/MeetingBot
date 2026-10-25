# 第二轮整改复核报告（2026-10-22，窄范围）

> 复核员：**verifier2**（独立验证，不写功能代码）
> 复核方式：**零模型调用、零 GPU、未重启后端**；只在 `/tmp` 副本上做篡改/重跑；**只新增本报告**，未修改任何代码/数据/文档/LEDGER。
> 快照：2026-10-22 18:03–18:08；被复核整改文件 mtime 均为 18:00–18:02（见附录 A），复核期间未再变化。
> 对象：round2 独立验证（`2026-10-22_round2_verification.md`）问题清单的整改闭合情况（task-7 窄范围）。

---

## 0. 结论：**有条件 PASS**（P1 与 P2-1/2/3 功能项已闭合；P2 文档一致性 1 项未收全 + 4 项 P3）

**已闭合（逐项实测）**

| 编号 | 整改 | 复核结果 |
|---|---|---|
| **P1-1** | 4 条单审计误分类（`cs06#0-2`、`n01#0`）改回分母；`fact_index` 回退改 1-based | **闭合**：`FORCE_RETRIEVABLE` 生效、重建清单可复现；五组数字复算 = 44/50=88.00%、20/23=86.96%、152/158=96.20%、35/36=97.22%、119/123=96.75%；基线重冻；`make quality-gate` **PASS** |
| **P2-1** | `fact_classes.json` sha256 入冻结；缺失/变更 → FAIL | **闭合**：基线 sha 与实物一致；`/tmp` 副本删清单/改一条分类/清空清单均 **FAIL（exit 1）**；缺失 packet 报错 exit 2 |
| **P2-2** | p0 旧口径 CI 改三轮合并计数；claim 行补 CI | **闭合**：p0 CI = 合并计数 CP（recall `[0.855,0.999]`、precision `[0.551,0.727]`、faith `[0.853,0.946]`、relev `[0.815,1.000]`）；claim 行 `[0.058,0.141]（规则上界口径）`；gate 25/25 行都有 CI |
| **P2-3** | 撤 MISSING_OWNER 前提规则；仅留 MISSING_DEADLINE 并补软期限 | **闭合**：真实产物回放中 MISSING_OWNER 被前提误伤数 19 → **0**；3 条合法软期限通过、3 条无时间语义被拒；contracts 77 项测试 OK |
| **P3**（复核项） | citations 只留 notice；claim ref≥1；pass_k status+reason；文档数字/口径 | **基本闭合**：payload 增长 46.2%→**25.5%**；claim CSV refs≥1 且 ca042 修复、可复现；pass_k 实测 `not_evaluable`/`evaluated` + `status_reason`；28 项数字抽查 26 项一致（见 §6） |

**未闭合（条件，详见 §7）**

1. **R1（P2，文档一致性）**：`evaluation/reports/2026-10-22_eval100_scale.md` 勘误 #4 与 `2026-10-22_rag_quality_gate.md` 勘误 #2 仍保留 **93.62%（44/47，剔除 11）**，**未标「更正前」/未指向 §42b**；与“旧数统一标更正前”的整改要求不符。另 LEDGER §42 正文（banner 已声明被 §42b 取代）仍留旧 floor 0.90/0.85、93.62/90.91、notice/类型/系列、MISSING_OWNER 线索等旧机制句，建议清理或整段标「初版存档」。
2. **R2（P3，设计）**：`fact_classes_sha256` 覆盖含 `generated_at` 的文件字节 → **语义完全相同的重建也会让 gate FAIL**（实测：沙箱重建仅时间戳不同 → exit 1「分类被改动」，提示语误导）。文档中的标准重建命令 `python3 evaluation/gold/build_fact_classes.py` 会产生该情形；建议改为对**去掉 generated_at 的规范化内容**取 sha，或在文档明确“重生成后必须重冻”。
3. **R3（P3，声明范围）**：`ruff check .` **未全过**：15 个错误，全部在本轮未改的 3 个文件（`evaluation/analysis/quality_gap_report.py` 9、`evaluation/fixtures/new_meeting_seed.py` 4、`scripts/moss_direct_cpcor.py` 2）；**本轮整改涉及的 11 个 Python 文件 ruff 全过**。若“ruff 全过”指变更文件则成立，指全仓则不成立。
4. **R4（P3，文档口径小误）**：“防退化线低于当前 2–3pp”与实测余量不符：p0 2.22pp、cross30 3.00pp、mixed100 **4.20pp**、new_meeting **4.96pp**（`quality_gate.py` 注释与 recall 报告 §4 的表述需改为 2–5pp）。
5. **R5（P3，残余）**：基线缺 `fact_classes_sha256` 键时 gate 仍 PASS（保护可被移除）；`_pooled()` docstring 称“二者口径在输出里注明”但 gate 输出未注明；`_artifact_paths()` 仍为死代码；基线 note 仍写“只允许 2pp 波动”（实际只拦下降）；主报告仍写“7 个产物 sha256”（未含新增的清单 sha）、`pass_k 显式 status=not_evaluable` 表述未更新为 `evaluated/not_evaluable + status_reason`。

以上 R1 为**条件项**（task-7 明确要求的“旧数统一标更正前”未完成），R2–R5 为 P3，不阻塞功能，但建议随文档收尾一并处理。
---

## 1. 复核范围与方法

- 仅复核 task-7 列出的整改点（P1-1、P2-1/2/3、P3 六项）及“gate PASS/测试/ruff”验收项；**未重复 round2 全量审查**。
- 方法：读改动 → 从 packet/gold 复算 → `/tmp` 最小副本重跑/篡改 → 实跑命令与测试 → 文档数字回源。
- 纪律：零模型调用（`make smoke` 自报 `model calls made: 0`）；无 GPU；无仓库写入（仅本报告新增）。

**关键快照 sha256**

| 文件 | sha256（前 16 位） |
|---|---|
| `evaluation/gold/fact_classes.json` | `3f5a2207337f1164` |
| `evaluation/baselines/quality_gate_baseline.json` | `85d492e57d4402d4` |
| `evaluation/quality_gate.py` | `295f20f1c2bc263f` |
| `evaluation/gold/build_fact_classes.py` | `9fc937d1d36c5e13` |
| `evaluation/lib/recall_quality.py` | `20643400e105a06a` |
| `evaluation/results/recall_quality_report.json` | `e8998ff871df8095` |
| `evaluation/gold/claim_anchor_cross30_100.csv` | `9a448bb80b560dd3` |

---

## 2. P1-1 复核：4 条误分类已改回分母

### 2.1 清单与机制

- `build_fact_classes.py` 新增 `FORCE_RETRIEVABLE = {"cross_30": {cs06#0,1,2}, "new_meeting_15": {n01#0}}`；`sources` 仍保留原始归因 `classification`（`gold_metadata_or_absence_fact` / `gold_fixture_limitation`），**provenance 未被改写**，仅在 `cases` 类别上覆盖为 `retrievable`；rule 文本与仪器报告/§42b 记录了更正来源。
- 归因回退已改 1-based：`_fact_index` 候选顺序 `(fallback-1, fallback, fallback-2)`；直接调用验证 `fallback=1..4 → 0..3` 全部正确，精确匹配仍优先（先前 P3-2 隐患闭合）。

### 2.2 重生成与可复现

- 沙箱重跑 `build_fact_classes.py`（复制 4 个 gold + 归因 JSON）：stdout 与仓库一致（cross_30 10 cases/15 facts、eval_100_mixed 11/15、new_meeting_15 2/3、qa_seed_extended 5/6）；**除 `generated_at` 外 JSON 完全一致**。
- 批注事实总数 39、`sources` 39；排除类：cross_30 metadata 8、eval_100_mixed metadata 8+conflict 1、qa_seed_extended conflict 2、new_meeting 0 → 合计 **19 剔除**（原 23 − 4）。

### 2.3 读数复算（独立重算，未采信 `recall_quality.py` 输出）

| scope | 旧口径 | recall-v2（复算） | 剔除 | 报告/LEDGER/§42b 声称 | 判定 |
|---|---:|---:|---:|---|---|
| p0 | 35/36 = 97.22% | 35/36 = **97.22%** | 0 | 97.22（不变） | ✅ |
| cross30 | 44/58 = 75.86% | **44/50 = 88.00%** | 8 | 88.00%（44/50，剔除 8） | ✅ |
| new_meeting | 20/23 = 86.96% | **20/23 = 86.96%** | 0 | 86.96%（20/23，剔除 0） | ✅ |
| mixed100 | 152/167 = 91.02% | **152/158 = 96.20%** | 9 | 96.20%（剔除 9） | ✅ |
| extended68（诊断） | 119/125 = 95.20% | 119/123 = 96.75% | 2 | 96.75% | ✅ |

- `recall_quality.py` 输出、`results/recall_quality_report.json`、`make recall` stdout 三者一致；cross30 Δpp = **+12.14**（88.00−75.86）。
- 基线 `context_recall_retrievable` = `{p0:0.9722, cross30:0.88, new_meeting:0.8696, mixed100:0.962}`，与复算一致；`instrument_version="recall-v2 (2026-10-22, corrected)"`。
- `make recall` 前后报告 sha 不变（`e8998ff8…`），可复现。

### 2.4 gate 实跑

`cd evaluation && make quality-gate` → **exit 0 / PASS**；recall-v2 四行：p0 `0.972/floor 0.950`、cross30 `0.880/floor 0.850`、new_meeting `0.870/floor 0.820`、mixed100 `0.962/floor 0.920`，全部 `yes`，CI 与合并计数一致（n=35/36、44/50、20/23、152/158）。**下限语义已与文档一致：0.85/0.82 为防退化线，0.90/0.85 为改进目标、不入门禁。**

---

## 3. P2-1 复核：manifest 入冻结 + 失败闭合

基线新增 `fact_classes_sha256 = 3f5a2207…`，与仓库 `fact_classes.json` 实物 sha256 **一致**；7 个 packet/claim 产物 sha256 重算 **7/7 一致**。

`/tmp/v3` 最小副本篡改矩阵（每次 `python evaluation/quality_gate.py`）：

| 场景 | 期望 | 实测 |
|---|---|---|
| 干净副本 | PASS | **PASS（exit 0）**，输出与仓库 gate 一致（仅 baseline 绝对路径行不同） |
| 追加 1 字节到 packet | FAIL | **FAIL（exit 1）**：`INSTRUMENT PROVENANCE FAIL: 产物已变但基线未重冻: cross30:…` |
| 改 `fact_classes.json` 一条分类（cm08#0→metadata），不重冻 | FAIL | **FAIL（exit 1）**：`fact_classes.json 已变但基线未重冻（主指标分类被改动）`（v2 变 0.898 仍过 floor，证明是 sha 拦截而非分数） |
| 删除 `fact_classes.json` | FAIL | **FAIL（exit 1）**：清单不存在 + 4 个 scope「清单缺…（不可评）」 |
| `fact_classes.json` 清空为 `{"sets":{}}` | FAIL | **FAIL（exit 1）**：sha 变更 + 各 scope 不可评 |
| 改分类 + **显式重冻** manifest sha | PASS | **PASS（exit 0）**（重冻路径可用，符合“改清单必须显式重冻”设计） |
| 缺 packet（非 p0） | 清晰报错 | **exit 2**：`缺少评测产物（先跑对应评测/组包）: [...]`（先前 traceback 问题已修） |
| `_check_artifacts` 单测 | 正确/篡改/缺失三态 | `[]` / `['产物已变但基线未重冻: …']` / `['缺少产物 …']` |

> 残余（P3-R5）：把基线 `fact_classes_sha256` 键整体删除后 gate 仍 PASS（保护可选）——建议把该键设为必备，缺失即 FAIL。
> 残余（P3-R2）：见 §0 第 2 条（时间戳 churn）。

---

## 4. P2-2 复核：CI 口径

| 行 | 显示值 | 打印 CI | 与我独立算的 CP（合并计数） |
|---|---:|---|---|
| p0 context_recall | 0.972（3 轮均值） | `[0.855,0.999]` | 35/36 → 一致 |
| p0 context_precision | 0.642（3 轮均值） | `[0.551,0.727]` | 79/123 → 一致 |
| p0 faithfulness | 0.907（3 轮均值） | `[0.853,0.946]` | 155/171 → 一致 |
| p0 answer_relevancy | 1.000 | `[0.815,1.000]` | 18/18 → 一致 |
| claim_not_in_any_context | 0.094 | `[0.058,0.141]（规则上界口径）` | 20/213 → 一致 |

- gate 25 行（4 smoke + 16 旧口径 + 4 recall-v2 + 1 claim）**全部有 CI**，无 `n/a`。
- 口径说明：recall 仪器报告 §4 已写明「p0 用三轮合并计数区间、显示值仍是 mean-of-rates」与「claim 行规则上界口径」；`quality_gate._pooled()` docstring 也说明二者不同。
- 残余（P3-R5）：output 行本身未附「CI=合并计数」标注（与 `_pooled` docstring 的“在输出里注明”不符），建议在 p0 行尾加 `(CI=3轮合并)`。

---

## 5. P2-3 复核：contracts 前提规则

- `ISSUE_PREMISE_CHECKS = {"MISSING_DEADLINE": ...}`；`OWNER_PREMISE_CUES` 已移除，**MISSING_OWNER 不再做前提语义过滤**。
- 软期限线索（`DEADLINE_CUE_PATTERN`）实测：`月底前完成灰度`、`上线前补齐文档`、`下班前给结论`、`下周五前提交`、`季度末前收口`、`发布前完成压测` → 通过；`只在产品组试点`、`团队都同意方案`、`张涛负责这块`、`这件事先推进`、`讨论一下范围`、`风险可控` → 拒绝（`missing_rule_premise_has_no_time`）。
- 构造验收：合法 MISSING_DEADLINE 3 条（月底前/上线前/下周五）全过；非法 3 条全拒；MISSING_OWNER 真实形态（`我一会跟学生跟那个家长聊`、`另外一方面，其实杨老师也组织了一些…`）**violations=[]**。
- 真实产物回放：`evaluation/**/*.json` 中 MISSING_OWNER 69 条记录（按 (case,type,span) 去重 19 条、按 (type,span) 去重 10 条）→ **前提拒绝 0 条（整改前 19/19 全被误伤）**。
- 测试更新且通过：contracts 77 项（1 个既有产物依赖 skip），含 `test_soft_deadline_premise_is_accepted`、`test_missing_deadline_premise_without_time_semantics_is_rejected`、`test_missing_owner_premise_is_not_over_filtered`。

---

## 6. P3 复核与数字抽查

### 6.1 P3 六项

| 项 | 结果 |
|---|---|
| citations：state `source_text` 只加 notice | ✅ `source_text` 含「工具说明：…」，**不含**「类型：/系列：」；type/status/owner/deadline/series 仍是独立字段；20 条 state 引用 payload 增长 **+25.5%**（6911→8671B；整改前 +46.2%） |
| claim CSV：refs≥1、100 行 12 列、无重复 | ✅ 100 行/12 列/ID 连续/无重复 (case,claim)/annotation 列留空；`ca042 cited_refs="2"`（原 `[0]` 已过滤）、cited_context 正常；沙箱重跑**字节一致** |
| pass_k status | ✅ 单份产物：`status="not_evaluable"`、`status_reason="需要同一配置重复 k 次 trial 产物；单份产物只给 single_trial_reference"`；3 trial 产物：`status="evaluated"`、`status_reason=""`、evaluated_cases=12 |
| 文档数字（28 项抽查） | 26 项一致（见下表），2 项文档级不一致 = R1（两份旧报告 93.62 未标更正） |
| “下限=防退化线、90/85 改进目标”口径 | 主报告/recall 报告/LEDGER §42b/baseline/gate 代码一致；LEDGER §42 正文旧 floor 仍留（有 banner 声明被取代，R1） |
| 其它（R2–R5） | 见 §0 |

### 6.2 数字抽查（≥15 项）

| # | 文档数字 | 回源复算 | 判定 |
|---|---|---|---|
| 1 | cross30 recall-v2 88.00%（44/50） | 44/50 | ✅ |
| 2 | cross30 旧口径 75.86%（44/58） | 44/58 | ✅ |
| 3 | cross30 剔除 8 条 | 8 metadata | ✅ |
| 4 | new_meeting 86.96%（20/23） | 20/23 | ✅ |
| 5 | mixed100 96.20%（152/158） | 152/158 | ✅ |
| 6 | p0 97.22%（35/36） | 35/36 | ✅ |
| 7 | extended68 96.75%（119/123） | 119/123 | ✅ |
| 8 | cross30 Δpp +12.14 | 88.00−75.86=12.14 | ✅ |
| 9 | cross30 CP 75.7–95.5% | CP(44,50)=[0.7569,0.9547] | ✅ |
| 10 | recall-v2 floors 0.85/0.82/0.95/0.92 | 代码 + baseline | ✅ |
| 11 | claim CI 5.83–14.13% | CP(20,213)=[0.0583,0.1413] | ✅ |
| 12 | p0 CI（合并计数） | 35/36、79/123、155/171、18/18 | ✅ |
| 13 | 清单事实 39 条/`sources` 39 | fact_classes | ✅ |
| 14 | 4 条反证更正（cs06#0-2、n01#0） | manifest cases + FORCE | ✅ |
| 15 | `fact_classes_sha256` 与实物一致 | `3f5a2207…` | ✅ |
| 16 | 7 个 packet/claim sha 一致 | 7/7 | ✅ |
| 17 | 测试 336 / 产品 643 | 实测 336 OK / 643 OK（6 skip） | ✅ |
| 18 | gate 25 行全过、17 冻结行（16 指标+claim） | 25 行统计 | ✅ |
| 19 | 契约测试 77 项 | 77 OK（1 skip） | ✅ |
| 20 | claim 锚点 100 条 | 100×12 | ✅ |
| 21 | 旧数 93.62/90.91 标“更正前” | recall 仪器报告/主报告/§42b ✅；**两份旧报告勘误未标 ❌** | ⚠️ R1 |

---

## 7. 剩余问题清单（按严重度）

**条件项（P2，建议本轮收尾前改）**

1. **R1 文档一致性未收全**：`evaluation/reports/2026-10-22_eval100_scale.md` §10 第 4 条、`evaluation/reports/2026-10-22_rag_quality_gate.md` §6 第 2 条仍写 93.62%（44/47，剔除 11）且无更正标记 → 改成「更正前 93.62%（44/47，剔除 11）；**更正后 88.00%（44/50，剔除 8）**，见 LEDGER §42b」。同时建议清理 LEDGER §42 正文的旧机制/旧数字（或整段标“初版存档，读数与机制以 §42b 为准”）。

**P3（不阻塞，建议记录）**

2. **R2** manifest sha 含 `generated_at`：语义相同重建也会 FAIL（提示语「分类被改动」误导）；建议对规范化内容取 sha 或在 build 文档写明“重建后须重冻”。
3. **R3** `ruff check .` 全仓 15 个错误（3 个未改动文件：`quality_gap_report.py` 9、`fixtures/new_meeting_seed.py` 4、`scripts/moss_direct_cpcor.py` 2）；本轮变更文件 ruff 全过。若交付声明“ruff 全过”，需注明范围或清理存量。
4. **R4** “防退化线低于当前 2–3pp”与实测不符（mixed100 4.20pp、new_meeting 4.96pp），建议统一改“2–5pp”。
5. **R5** 残余：manifest sha 键可被删除而 PASS；gate 输出未标 p0 CI=合并计数；`_artifact_paths()` 死代码；baseline note“只允许 2pp 波动”；主报告“7 个产物 sha256”/`pass_k status=not_evaluable` 措辞未更新。

---

## 8. 测试与命令（全部零模型调用）

| 命令 | 结果 |
|---|---|
| `cd evaluation && ../meetingbot/fastapi-app/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | **Ran 336 tests, OK**（3.8s） |
| `cd meetingbot/fastapi-app && .venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | **Ran 643 tests, OK (skipped=6)**；skip 为 5 个需 `PG_TEST_DSN` 的既有集成测试 + 1 个缺事故产物的既有 skip（非本轮引入） |
| `make -C evaluation smoke` | **exit 0**，`model calls made: 0`；`metrics.json`/`METRICS_REPORT.md` 重写后 sha 不变 |
| `make -C evaluation recall` | **exit 0**；五组读数 0.8800/0.8696/0.9620/0.9722/0.9675；报告 sha 前后不变 |
| `cd evaluation && make quality-gate` | **exit 0 / QUALITY GATE: PASS**；25 行全 `yes` |
| `test_contracts.py` / `test_citations.py` / `test_agent.py` / `test_trajectory.py` | 77 OK（1 skip）/ 4 OK / 30 OK / 21 OK |
| `ruff check .`（全仓） | **exit 1：Found 15 errors**（见 R3）；`ruff check <本轮变更的 11 个 py 文件>` → **All checks passed** |
| 仓库副作用 | smoke/recall/gate 前后 `git status` 与 `results/*.json` sha **无变化** |

---

## 9. 复现命令（零调用）

```bash
R=/home/song/code/Agent/media_agent/AIMeeting
PY=$R/meetingbot/fastapi-app/.venv/bin/python
cd $R/evaluation

# 1) 读数/复现
$PY -m unittest discover -s tests -p 'test_*.py'          # 336 OK
make smoke && make recall && make quality-gate            # gate PASS
md5sum results/recall_quality_report.json                  # 可先记 sha，make recall 后应不变

# 2) 清单重建（沙箱；仓库不动）
mkdir -p /tmp/v3/repro/evaluation/{gold,results}
cp gold/build_fact_classes.py /tmp/v3/repro/evaluation/gold/
cp gold/{cross_meeting_qa_30,new_meeting_15,eval_100_mixed,qa_seed_extended}.json /tmp/v3/repro/evaluation/gold/
cp results/quality_gap_report.json /tmp/v3/repro/evaluation/results/
(cd /tmp/v3/repro && $PY evaluation/gold/build_fact_classes.py)   # 除 generated_at 外应与仓库一致

# 3) 质量闸篡改（沙箱）：复制 evaluation/quality_gate.py + lib + gold/fact_classes.json
#    + baselines + 7 个 results 产物到 /tmp/v3/gate，然后：
$PY /tmp/v3/gate/evaluation/quality_gate.py                     # PASS
# 改 /tmp/v3/gate/evaluation/gold/fact_classes.json 一条分类 -> FAIL（manifest sha）
# 删除该清单 -> FAIL；改 packet 一字节 -> FAIL（artifact sha）
# 清空该清单 -> FAIL（各 scope 不可评）

# 4) 产品全量测试 + contracts 回放
cd $R/meetingbot/fastapi-app
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # 643 OK (6 skip)
# 真实产物 MISSING_OWNER 回放（0 条被前提误伤）见 round2_verification 报告 §8 脚本
```

---

## 10. 复核限制

1. 本报告只复核 task-7 范围；未重做 round2 的 MOSS/ASR、17/30 逐题差异等已登记未复核项。
2. 未做独立第二人语义复核：**P1 的 4 条改判依据仍是 round2 的“同题 packet found=true”反证**；`human_checked` 仍为 false，提升为最终金标仍需双人复核（文档已如实标注）。
3. `ruff check .` 全仓失败项均为**本轮未改文件**的存量问题；本轮变更文件单独跑通过（见 R3）。
4. 未跑 PG 集成测试（6 个 skip 为环境依赖）；未重启后端、未跑模型。
5. 沙箱 gate 输出与仓库 gate 的唯一差异是 `baseline:` 行打印的绝对路径，行为一致。

---

## 附录 A：整改文件与快照

| 文件 | mtime | 说明 |
|---|---|---|
| `evaluation/gold/build_fact_classes.py` | 18:02 | FORCE_RETRIEVABLE + 1-based 回退 |
| `evaluation/gold/fact_classes.json` | 18:02 | 重生成（`generated_at=2026-10-22T10:02:30Z`） |
| `evaluation/baselines/quality_gate_baseline.json` | 18:02 | v2 重冻 + fact_classes_sha256 |
| `evaluation/quality_gate.py` | 18:02 | manifest sha 校验、pooled CI、claim CI、失败闭合 |
| `evaluation/regression/trajectory.py` | 18:00 | pass_k status/status_reason |
| `meetingbot/fastapi-app/services/citations.py` | 18:00 | state 只留 notice |
| `meetingbot/fastapi-app/services/contracts.py` | 18:01 | 撤 OWNER 前提、DEADLINE 软期限 |
| `evaluation/gold/claim_anchor.py` / CSV | 18:00 / 18:01 | ref≥1 过滤、重生成 100 条 |
| `evaluation/tests/test_recall_quality.py`、`tests/test_contracts.py`、`tests/test_citations.py` | 18:00–18:02 | 期望值同步（metadata 8、44/50、软期限、notice） |
