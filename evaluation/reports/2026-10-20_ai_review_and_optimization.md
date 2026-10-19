# 2026-10-20 AI 复核 + 人工审核包 + 优化复测

- 授权：不限调用次数。产品侧累计 **2,237 次**调用；外部 judge/审核调用约 **450 次**
  （DeepSeek/Qwen 直连，不写产品 `ai_call_log`）。
- 产品记账成本：863,369 微元 ≈ **¥0.8634**。
- 说明：本轮先做 **AI 逐条复核 gold**，再在复核后的 draft-gold 上复测；
  `human_checked=false`，人工审核包已生成。

## 1. AI 复核 gold（人工审核前）

- 轨迹 gold：4 条启发式标签（t11/t12/t16/t25）按证据修正为 `search_knowledge`
  （原 heuristic 把 aggregation/state 硬绑到 list_facts/state，与实际数据源不符）；
  标记 `label_source=agent_ai_reviewed`、`review.human_checked=false`。
- 跨会议 gold：30 题 58 条 expected_fact，由 DeepSeek 逐条对照转写/纪要/种子数据：
  **55 supported、3 partial（已按材料改写）、0 unsupported**；
  标记 `label_source=agent_ai_reviewed`、`human_checked=false`。
- 人工审核包：
  - `evaluation/reports/cross_gold_human_review_30.csv`（58 行，含 AI 判定/证据/建议表述/人工批准列）
  - `evaluation/reports/cross_gold_ai_review_summary.md`
  - `evaluation/reports/answer_human_review_30.csv`（AI 答题 + 引用 + AI judge 分数，供人工复核答案正确性）

## 2. 轨迹 27 题（同 27 题）

| 指标 | pre-review gold | AI-reviewed gold |
|---|---:|---:|
| tool_call_f1（macro） | 0.7469 | **0.8951** |
| 首轮 strict | 18/27 = 66.7% | 22/27 = 81.5% |
| 首轮 acceptable | 25/27 = 92.6% | 25/27 = 92.6% |
| required 覆盖 | 23/27 = 85.2% | 27/27 = 100% |
| must_cite | 25/25 | 25/25 |
| 禁止工具命中 | 0 | 0 |

**不能把 0.7469→0.8951 写成产品提升**：其中一部分来自修正 4 条错标 gold。
产品侧真实改动只有状态/聚合/寒暄提示，pre-review 的 0.7469 才是产品读数。

## 3. pass^3（12 题 ×3）

| gold | pass^3 | strict | acceptable | 禁止工具 |
|---|---:|---:|---:|---:|
| pre-review | 10/12 = 83.3% | 10/12 | 12/12 | 0/36 |
| AI-reviewed | **12/12 = 100%**（CP 95% 73.5–100） | 12/12 | 12/12 | 0/36 |

12/12 的直接原因是 2 条 aggregation 题的 gold 标签被修正；**不把它当独立泛化证据**。
人工复核后若维持修正，才能引用。

## 4. 跨会议 A/B（30 题 ×2，AI-reviewed draft-gold）

| 指标 | 臂 A（planning 关） | 臂 B（planning 开） |
|---|---:|---:|
| context_recall | 40/58 = 68.97% | 44/58 = 75.86% |
| context_precision | 74/112 = 66.07% | 82/138 = 59.42% |
| faithfulness | 165/214 = 77.10% | 220/278 = 79.14% |
| answer_relevancy | 26/30 = 86.67% | 28/30 = 93.33% |

recall +6.90pp，但 precision −6.65pp；预注册要求“recall +5pp 且 precision 不降
>3pp”，**仍未达标**。且两轮方向不完全一致，保持 `KB_QUERY_PLAN_ENABLED=0`。

## 5. 注入 / 在线评测 / 成本路由

- 注入线上 30 题：注入标记 10/10、良性误报 0/20、系统提示泄露 0/30、
  跨会议外发 0/10。
- 在线评测 30 条：faithfulness 0.8967、relevance 0.9217、pass_rate 0.9667。
- 成本路由试点（5 题）：qwen vs DeepSeek，输入 42,417 vs 47,688 token、
  输出 1,177 vs 1,898；precision 86.7% vs 77.8%、faithfulness 100% vs 94.9%。
  两模型均未定价，不能算金额；路由已删除，默认关闭。

## 6. 累计

| 指标 | 读数 |
|---|---:|
| 产品调用 | 2,237 次（CHAT 1,096 / KB_QUERY_EMBED 462 / KB_RERANK 461 / KB_QUERY_PLAN 93 / ONLINE_EVAL_JUDGE 65 / CHAT_SUMMARY 60） |
| 产品记账成本 | ¥0.8634 |
| 实验窗口 token 级缓存命中 | 4,690,688 / 7,773,855 = **60.34%** |
| 外部 judge/审核 | 约 450 次 |

## 7. 人工审核后要做什么

1. 逐行审核 `cross_gold_human_review_30.csv` 的 58 条事实，填 `human_approve`；
2. 对 4 条轨迹标签修正确认（t11/t12/t16/t25）；
3. 确认后把两个 gold 的 `human_checked` 置 true，再引用 AI-reviewed 指标；
4. 未确认前：轨迹 0.8951 / pass^3 100% 都只能作为**待人工复核的草稿读数**。
