# 全新（合成未见）会议评测集与结果（2026-10-22）

> **重要修正（2026-10-22）**：本文原先以 scoped 结果为主，recall 100% 是“限定单会议 +
> 题面与事实同源”的 smoke 结果，不能当泛化。主口径已改为 **unscoped**：
> recall 86.96%、precision 70.97%、faithfulness 80.00%、relevancy 100%。
> 详见 `2026-10-22_eval_design_audit.md`。

## 1. 目的

原有评测都基于同一场「产品发布评审会议」，泛化证据弱。为收尾补一个**未见会议**：

- 新会议：`客户支持知识库与智能客服灰度评审会`（meeting_id=3，2026-10-15）
- 两段录音：task 8 上午 / task 9 下午
- 已确认纪要：minutes 5
- 主题：知识库覆盖率、FAQ 补齐、智能客服灰度、账单 API 依赖、退款政策风险、预算/CSAT

语料是**合成 fixture**（`evaluation/fixtures/new_meeting_seed.py`），不是真实会议；
但题目、事实、会话范围都是新的，且经独立复核。

## 2. 评测方式

- 每题**新建 session**，并限定 `meeting_ids=[3]`，避免跨会议污染；
- 15 题 / 23 条 expected facts；
- judge 四指标 + 独立事实复核 + 独立答案盲评。

## 3. 结果

### judge（两口径）

| 口径 | recall | precision | faithfulness | relevancy | 用途 |
|---|---:|---:|---:|---:|---|
| **unscoped（主口径）** | **86.96%** | **70.97%** | **80.00%** | **100%** | 有干扰会议 |
| scoped（smoke） | 100% | 97.96% | 86.67% | 100% | 只验证链路 |

### 独立复核

| 审核 | 结果 |
|---|---|
| 23 条事实 | **23 approve / 0 revise / 0 reject** |
| 15 条答案 | **15 correct / 0 partial / 0 wrong** |

盲评确认：两段录音区分正确，纪要 decisions/pending/risks/viewpoints 正确取用，
82%/90%、120 条 FAQ、10/25、11/1 10%、15 分钟、11/15、10/30、4.5、8 万等数字与
负责人（王磊/张敏/陈雪/李娜）均与 fixture 一致。

## 4. 与旧集合的对比

| 集合 | 场景 | recall | precision | faithfulness | relevancy |
|---|---|---:|---:|---:|---:|
| P0 6×3 | 旧会议，fresh | 97.22% | 64.23% | 90.75% | 100% |
| fresh cross30 | 旧会议，跨录音 | 75.86% | 63.64% | 88.63% | 93.33% |
| **new meeting 15** | **新会议，限定范围** | **100%** | **97.96%** | **86.67%** | **100%** |

新会议 precision 明显更高，说明：
- 限定会议范围后，检索上下文更干净；
- 旧 cross30 的 precision 低，部分来自跨会议/多工具上下文混合，而不是模型不会查；
- 但新会议是合成语料，题目也较直接，不能直接说“真实泛化 98%”。

## 5. 质量闸已纳入新会议

`evaluation/quality_gate.py` 已改为使用 **unscoped new_meeting** 作为门禁：
recall≥80%、precision≥60%、faithfulness≥75%、relevancy≥90%；
scoped 只作为 smoke 打印，不参与门禁。

## 6. 独立审核发现的 fixture 问题（已在 gold 标注）

1. n01 主持人无显式标注：转写通过李娜开场/控场推断；meeting.host_id 未映射姓名；
2. n15 source_task_ids 原写 9，已修为 [8,9]；
3. task9 recap「API 检查 10/28」与其余「10/30」冲突；minutes source_task_id 混合两段录音；
4. 年份口径不统一（minutes 有 2026，转写未写年份）。

这些是**合成 fixture 的材料问题**，不是答案事实错误；报告已标注 `fixture_limitations`。

## 7. 结论

- scoped smoke 15 题 **15 correct / 0 partial / 0 wrong**，证明链路可用，但不能当泛化；
- unscoped 主口径 recall 86.96%、precision 70.97%、faithfulness 80.00%；
- 答案盲评的 CSV 带 expected facts，不是严格盲评；
- 收尾结论只能是“合成未见会议 sanity check + 跨会议混淆可用”，
  真实会议与人工金标仍缺失。

## 8. 文件

- fixture：`evaluation/fixtures/new_meeting_seed.py`
- gold：`evaluation/gold/new_meeting_15.json`
- QA：`evaluation/results/qa_new_meeting_15.json`
- answers/packet：`evaluation/results/new_meeting_scoped_*`
- 事实复核：`evaluation/reports/independent_new_meeting_fact_review_2026-10-22.json`
- 答案盲评：`evaluation/reports/independent_new_meeting_answer_review_2026-10-22.json`
