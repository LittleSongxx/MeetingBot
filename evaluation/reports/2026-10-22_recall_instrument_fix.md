# 仪器修复：可检索事实召回（recall-v2）与质量闸加固（2026-10-22）

> 变更类型：**换仪器**（context_recall 主口径 + quality_gate 输出）与**质量闸加固**。零模型调用。
> 依据：`docs/质量指标全景与缺口分析.md §4/§9`、`evaluation/results/quality_gap_report.json`（metric-auditor 逐条归因）、
> `evaluation/reports/2026-10-22_round2_verification.md`（独立验证，含 4 条误分类更正）、
> `evaluation/LEDGER.md §42/§42b`。
> **旧读数不与新读数相减**：旧 `context_recall` 保留为 `context_recall_legacy`（历史可比/回归护栏），
> 新主指标为 `context_recall_retrievable`（recall-v2）。
>
> **重要更正（round2 独立验证后）**：本报告初稿的 cross30 93.62%（44/47）与 new_meeting 90.91%（20/22）
> 建立在 4 条单审计误分类上（cross30 `cs06#0-2`、new_meeting `n01#0`）。同题 packet（m016/m071）
> judge 判 found=true，证明这 4 条可检索。更正后 cross30 **88.00%（44/50）**、new_meeting
> **86.96%（20/23）**；质量闸下限同步按"防退化线"调整为 0.85/0.82（0.90/0.85 是改进目标，不入门禁）。

## 1. 问题

`cross30_fresh` 的 `context_recall` 75.86%（44/58）被当成"跨录音检索差"引用，但 14 条缺失事实里
初判 **11 条是检索材料按定义不可能包含的**：`supersedes_id` 为空、CONFIRMED vs DRAFT、tool notice 原文、
"系列只含会议 2"等系统元数据/否定性结论；另有 fixture 限制与题目冲突。
按题型拆：cm 跨录音 **94.74%**、ct 单点 **90.00%**、cs 系列 **42.11%**——缺口集中在系列状态题。
剔除可检索口径后（更正 4 条误分类后）cross30 可检索事实召回 **44/50 = 88.00%**。

## 2. 机制

| 件 | 内容 |
|---|---|
| 分类清单 | `evaluation/gold/fact_classes.json`：由 `evaluation/gold/build_fact_classes.py` 从既有逐条归因产物生成（零调用，`human_checked=false`）。只给**逐条分类过的缺失事实**打非 retrievable 标签，其余默认 `retrievable`；`FORCE_RETRIEVABLE` 记录 round2 反证的单审计误分类。 |
| 类别 | `metadata`（系统状态/元数据/否定性结论/notice）、`fixture`（合成材料限制）、`conflict`（题目/金标冲突）→ **不计入召回分母**；`judge_uncertain`（疑似 judge 假阴性）→ **仍计入分母**（保守）；其余 `retrievable` |
| 计算 | `evaluation/lib/recall_quality.py`：逐 case 对齐 `judgement.context.facts[].found` 与 gold 事实序号；未判定的 case 整题跳过；兼容 `judgements.f` 跨族产物 |
| 报告 | `evaluation/analysis/recall_report.py` → `evaluation/results/recall_quality_report.json` |
| 门禁 | `evaluation/quality_gate.py`：**旧口径 + recall-v2 双行**；recall-v2 有独立绝对下限与冻结基线；分类清单缺失/变更 → FAIL |

## 3. 结果（当前产物，n 为事实数；**已按 round2 更正**）

| scope | 旧 context_recall | recall-v2 | 剔除（非可检索） | Δpp（仅描述，不可相减） | 主口径下限（防退化） |
|---|---:|---:|---:|---:|---:|
| p0（6 题×3 轮） | 97.22%（35/36） | 97.22%（35/36） | 0 | +0.00 | ≥0.95 |
| cross30_fresh | **75.86%（44/58）** | **88.00%（44/50）** | 8 | +12.14 | ≥0.85 |
| new_meeting（unscoped） | 86.96%（20/23） | 86.96%（20/23） | 0 | +0.00 | ≥0.82 |
| mixed100（组装集） | 91.02%（152/167） | 96.20%（152/158） | 9 | +5.18 | ≥0.92 |
| extended68（诊断，不入门禁） | 95.20%（119/125） | 96.75%（119/123） | 2 | +1.55 | — |

> recall-v2 的分子不变（仍是 judge 判"在上下文里找到"），只收窄分母；不是"分数变好看"，而是
> 同一个分子除以正确的分母。真缺口（retrieval_miss/partial）仍然计入分母、仍然扣分。
> **更正记录**：初版 cross30 93.62%（44/47）/ new_meeting 90.91%（20/22）依赖 4 条误分类
> （cs06×3、n01），独立验证反证后已改回分母；当前数字为 88.00%/86.96%。

## 4. 质量闸加固（同一轮）

1. **双口径 + 独立下限**：旧 `context_recall` 继续按冻结基线做回归护栏；recall-v2 是主指标，
   下限按"防退化线"定位（低于当前 3–5pp：cross30 88.0→0.85、mixed100 96.2→0.92、new_meeting 87.0→0.82），**0.90/0.85 的改进目标不入门禁**。
2. **每个可计算计数的比例打印 Clopper-Pearson 95% 区间**；p0 旧口径行用三轮**合并计数**的区间
   （显示值仍是 mean-of-rates，二者口径不同）；claim 行打规则上界口径的区间。
3. **冻结基线记录产物 sha256**（7 个 packet/claim 文件）**+ `fact_classes.json` 的 sha256**；
   产物或分类清单变了但基线没重冻 → 闸门 **FAIL** 并提示重新冻结；清单缺失 → FAIL（不再静默跳过）。
4. **基线元数据**：`instrument_version=recall-v2 (2026-10-22, corrected)`、`context_recall_retrievable`
   冻结值与 `fact_classes_sha256`；旧键保持不变。

## 5. 限制与纪律

- `fact_classes.json` 是 `human_checked=false`：类别来自 metric-auditor 的逐条归因（39 条缺失事实）
  **加 round2 的 4 条反证更正**，不是双人独立金标；提升为最终金标前需按 `gold/ANNOTATION_GUIDE.md` 双人复核。
- 只标了**缺失**事实；已 found 的事实一律 `retrievable`。
- `judge_uncertain` 计入分母（保守）：extended68 的 q93 疑似 judge 假阴性仍扣分，待跨族重判后修正。
- recall-v2 只修"分母口径"，**不改变检索与生成**；cross30 88.0% 距 90% 改进目标仍有 2pp，产品侧
  真缺口仍按 P1（系列覆盖/查询分解/证据选择）处理。
- 改类别/改清单 = 换仪器，必须新预注册并重述历史数（评测宪法第 2/6 条）。
