# 2026-10-19 前沿 Agent 能力实测报告（预注册后首次）

- 授权上限：300 次模型调用；**实耗 299 次**（产品 `ai_call_log` 293 次 + 评测侧跨族 judge 6 次，后者不写产品库）。
- 产品记账成本：109,033 微元 ≈ **¥0.1090**（按调用时单价；6 次评测侧 judge 走 DeepSeek，不在产品账本）。
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
| 产品调用数 | 293（CHAT 157 / KB_QUERY_EMBED 62 / KB_RERANK 61 / CHAT_SUMMARY 5 / ONLINE_EVAL_JUDGE 5 / KB_QUERY_PLAN 3） |
| 产品记账成本 | ¥0.1090（另有 6 次评测侧 judge 不在此账本） |
| 实验窗口缓存命中（token 级） | 600,704 / 972,170 = **61.79%** |
| 全历史缓存命中（API 口径） | 10,508,544 / 23,952,638 = **43.87%** |
| 问答单轮平均调用 | 2.22（全历史） |
| 未定价调用（全历史） | 8,649 条（成本记 0，不能当免费） |

说明：实验窗口命中率包含 27+ 次重复请求与规划/检索调用，不能把 61.79% 当作通用命中率；全历史口径 43.87% 也只是历史观测。成本未做 CHEAP 路由 A/B，路由仍默认关闭。

## 6. 跨会议 draft-gold 补全（零调用）

- `evaluation/gold/cross_meeting_qa_30.json`：30 题全部补 `expected_facts` 与
  `evidence_segments`/`evidence_refs`，依据种子转写、纪要、系列记忆与待办；
  `label_source=agent_draft_enriched`、`human_checked=false`、`todo=["human_review"]`。
- 这是 **AI 草稿金标**，不是人工金标；在人工复核前，任何 context recall 只能标
  `draft-gold`，不能写成正式质量结论。

## 7. 查询规划 A/B 试点（3 题，臂间同题）

- 臂 A：`KB_QUERY_PLAN_ENABLED=0`，复用 27 题轨迹中的 3 条 cross_meeting 回答。
- 臂 B：`KB_QUERY_PLAN_ENABLED=1`，同 3 题重新抓取（规划调用 3 次）。
- 结果：
  - 词法 context 覆盖率：两臂均为 **0.911**（无差异）
  - 词法 answer 覆盖率：A **0.917** vs B **0.844**（规划臂反而略低，样本 3）
  - LLM context judge：两臂均 0/6，但词法证据显示事实大多在上下文里，判定器返回
    与词法证据矛盾，**该 judge 结果判为仪器失败，不采纳**；不做“0 提升”结论。
- 结论：没有证据支持查询规划在这 3 条跨会议题上带来收益；A/B 需要更大样本和
  可用 judge，且 6 次 judge 已消耗剩余预算。该试点标记为 inconclusive。

## 未运行 / 缺口

- 跨会议 A/B 30 题：金标已补为 AI draft-gold，但完整 30×2 未运行；只做了 3 题
  查询规划试点（词法口径无差异、LLM judge 仪器失败，inconclusive）。
- 成本路由 A/B：未建 CHEAP 配置、未做配对样本；只完成路由实现与看板。
- pass^3 12 题：预算只够 4 题 ×3；其余 8 题未跑。
- 人工 kappa：`results/judge_spotcheck_20.csv` 已生成 20 行，但未回填人工标签，`--score` 不报数。
- 所有读数只适用于当前种子语料与当前提示词版本；不能写成通用提升。
