# 2026-10-19 前沿 Agent 能力实测报告（预注册后首次）

- 授权上限：300 次模型调用；**实耗 269 次**（以 `ai_call_log` 统计，窗口 19:15–19:40）。
- 记账成本：98,453 微元 ≈ **¥0.0985**（按调用时单价；历史未定价调用不计入本窗口）。
- 语料：本地种子会议（1 场会、2 段录音、1 份 CONFIRMED 纪要）+ 本次补的系列/记忆/待办演示数据。
- 口径：所有比例带样本量；未跑项不写结论。

## 1. 轨迹评测（27 题，1 trial）

产物：`evaluation/results/trajectory_answers.json` → `trajectory_report_27.json`
金标：`evaluation/gold/trajectory_27.json`（10 single_fact / 6 aggregation / 5 cross_meeting / 2 smalltalk / 4 state）

| 指标 | 读数 |
|---|---:|
| **tool_call_f1（主指标，macro）** | **0.5432** |
| 首轮 strict | 13/27 = 48.1%（CP 95%: 28.7–68.1%） |
| 首轮 acceptable | 24/27 = 88.9%（CP 95%: 70.8–97.6%） |
| required 覆盖 | 16/27 = 59.3%（CP 95%: 38.8–77.6%） |
| must_cite 命中 | 25/25 = 100% |
| 禁止工具命中 | 2 次（2 条 smalltalk 各 1 次） |
| 工具集合 micro P/R/F1 | 0.485 / 0.640 / 0.552 |
| 按 intent 的 F1 | single_fact 1.00；cross_meeting 0.80；aggregation 0.11；state 0.00；smalltalk 0.00 |

说明：这是**加固状态工具提示前**的 27 题读数；state 4 题当时全部没调 `query_meeting_state`，aggregation 题大量用 `search_knowledge` 起步。smalltalk 2 题仍调了工具，属于提示词未覆盖的已知缺口。

## 2. 状态工具加固后的 4 题 × 3 trials（pass^3）

产物：`results/state4_t3.json` / `t4` / `t5` → `trajectory_state4_pass3.json`

| 指标 | 读数 |
|---|---:|
| pass^3（3 次全满足 required+must_cite） | 3/4 = 75%（CP 95%: 19.4–99.4%） |
| required 覆盖（首次 trial） | 3/4 = 75% |
| 首轮 strict | 2/4 = 50% |
| 首轮 acceptable | 4/4 = 100% |
| must_cite | 4/4 = 100% |
| 工具集合 F1 | 0.458 |

说明：加固内容为「状态类问题第一步必须调用 query_meeting_state」+ `meeting_ids` 过滤。q12/q23 已稳定走 state 工具；q26 会先 list_facts 再补 state；q71 仍从 list_facts 起步。4 题样本小，区间很宽，不能外推。

## 3. 注入线上红队（10 题，1 trial）

产物：`results/injection_live.json`；脚本：`evaluation/security/injection_live.py`

| 指标 | 读数 |
|---|---:|
| 产品 security 标记率 | 10/10 = 100% |
| 系统提示/协议原文泄露 | 0/10 |
| 跨会议外发成功 | 0/10（2 条回答出现“其他会议”措辞，实际均为明确拒绝） |
| 规则侧（零调用） | 30/30 检出、0/20 良性误报 |

说明：产品侧安全标记覆盖全部 10 条；回答没有泄露系统提示或跨会议数据。规则侧和线上臂是两套仪器，不能合并成一个“拦截率”。

## 4. 在线评测（管理员手动触发，5 条）

产物：`online_eval_run` id=1（MySQL）+ API 详情

| 指标 | 读数 |
|---|---:|
| 样本 | 5 |
| avg_faithfulness | 0.80 |
| avg_relevance | 0.92 |
| pass_rate（阈值 0.6） | 1.0 |
| 失败样例 | 0 |

说明：只做趋势输入，不替代离线评测；样本 5 条，不能当质量结论。

## 5. 成本与缓存（同一窗口）

| 指标 | 读数 |
|---|---:|
| 调用数 | 269（CHAT 148 / KB_QUERY_EMBED 56 / KB_RERANK 55 / CHAT_SUMMARY 5 / ONLINE_EVAL_JUDGE 5） |
| 记账成本 | ¥0.0985 |
| 实验窗口缓存命中（token 级） | 567,552 / 707,412 = **80.23%** |
| 全历史缓存命中（API 口径） | 10,508,544 / 23,952,638 = **43.87%** |
| 问答单轮平均调用 | 2.22 |
| 未定价调用（全历史） | 8,649 条（成本记 0，不能当免费） |

说明：实验窗口命中率高是因为同一系统前缀被 27+ 次重复请求；全历史口径包含大量旧调用与不同模型，不能把 80% 当作通用命中率。成本未做 CHEAP 路由 A/B，路由仍默认关闭。

## 未运行 / 缺口

- 跨会议 A/B 30 题：金标 `expected_facts` 仍为空，且预算已用 269/300，未运行。
- 成本路由 A/B：未建 CHEAP 配置、未做配对样本；只完成路由实现与看板。
- pass^3 12 题：预算只够 4 题 ×3；其余 8 题未跑。
- 人工 kappa：`results/judge_spotcheck_20.csv` 已生成 20 行，但未回填人工标签，`--score` 不报数。
- 所有读数只适用于当前种子语料与当前提示词版本；不能写成通用提升。
