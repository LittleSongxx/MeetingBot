# Agent 全链路质量深研（2026-10-21）

## 1. 结论先行

当前瓶颈不是单一 RAG 检索，而是**意图识别/工具策略、引用时间绑定、检索 precision**三层叠加：

- 意图识别：产品没有显式 intent router；用 qwen 直接分类 27 条轨迹题，准确率 **81.5%**，
  cross_meeting/state/aggregation 之间仍有混淆。
- 工具策略：cross_meeting 首轮 strict 仅 **20%**（acceptable 80%），工具过度调用 **1.8×**；
  state 首轮 strict 50%、工具 F1 **0.583**、过度调用 **1.75×**。
- RAG：fresh 6×3 mean recall 97.22%、precision 64.23%、faithfulness 90.75%。
- 引用/时间绑定：真 unsupported 集中在 ct06/ct08 的“9月1日/9:00/18:00”归属，不是数字编造。
- 评测口径：多轮历史引用会造成假 unsupported；fresh session 与多轮必须分开。

## 2. 各层指标

### 2.1 意图/工具选择（轨迹 27 题，gold）
| intent | n | 首轮 strict | 首轮 acceptable | tool F1 | tool precision | 覆盖 | 过度调用 |
|---|---:|---:|---:|---:|---:|---:|---:|
| single_fact | 10 | 90% | 90% | 0.967 | 0.95 | 100% | 1.10× |
| aggregation | 6 | 83.3% | 100% | 0.778 | 0.75 | 83.3% | 1.17× |
| cross_meeting | 5 | **20%** | 80% | 0.733 | 0.60 | 100% | **1.80×** |
| state | 4 | **50%** | 100% | **0.583** | **0.50** | 75% | **1.75×** |
| smalltalk | 2 | 100% | 100% | 1.000 | 1.00 | 100% | — |

失败点：
- cross_meeting 题（t17–t20）先调 `list_facts`/`get_transcript_excerpt`，而不是 `search_knowledge`；
  但 acceptable 里允许 list_facts，所以 strict 很低、acceptable 尚可——真正问题是**过度调用与上下文噪声**。
- aggregation t13（“哪两组验收数据”）应该先 `list_facts`，用了 search。
- state t26（“行动项和负责人”）应该 `query_meeting_state`，用了 list_facts+search。
- state/cross 的工具集 precision 只有 0.5/0.6，说明模型“多查了不该查的”。

### 2.2 意图分类实验
用产品同族 qwen 对 27 条轨迹题分类：

- accuracy **81.5%**
- 混淆：
  - single_fact → cross_meeting 1、state 1
  - aggregation → state 1
  - cross_meeting → state 1
  - state → aggregation 1
- 结论：单独让 LLM 做 intent router 不够；需要**规则 + 分类器 + 置信度回退**。

### 2.3 RAG
| 口径 | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| P0 6×3 fresh mean | 97.22% | 64.23% | 90.75% | 100% |
| fresh cross30 单轮 | 75.86% | 63.64% | 88.63% | 93.33% |
| 多轮 cross30（旧口径，混淆） | 76.79% | 68.87% | 81.86% | 100% |

RAG 的 recall 已高；precision 是主瓶颈。

### 2.4 引用/时间
- 规则审计 cross30 25.5% 数字不在被引上下文；LLM 深判后真 unsupported 18/188=9.6%。
- fresh session 审计“不在任何上下文” 9.4%（多轮为 22.9%，约一半是历史引用假阴性）。
- 真 unsupported 集中在 ct06/ct08/ct03 的时间/引用归属。

### 2.5 可靠性/成本
- pass^3（12×3）12/12，但只覆盖简单 12 题；cross/state 的 pass^k 需要补齐。
- 检索链路 ~0.5–0.65s；CHAT ~2.4s；CHAT_SUMMARY ~20.7s；所有调用仍大量 unpriced。

## 3. 联网调研

- **BFCL V4**：工具调用评测已从单轮 AST 扩展到多轮/agentic；工具选择不只看“调没调”，
  还看参数/AST/多轮一致性。
  https://gorilla.cs.berkeley.edu/leaderboard.html
- **τ-bench**：用数据库终态对比 + **pass^k** 衡量 agent 可靠性；即使强模型也常低于 50%、
  pass^8 <25%，说明“单次成功”远不够。
  https://arxiv.org/abs/2406.12045
- **Adaptive-RAG**：用 query complexity 分类器在 no-retrieval/单步/多步之间路由；
  这正是我们缺少的“按意图/复杂度选策略”。
  https://aclanthology.org/2024.naacl-long.389/
- **RAGAS / ARES / ALCE**：检索、生成、引用质量要分开；judge 要校准；引用质量独立评估。
  https://docs.ragas.io/en/v0.1.21/getstarted/evaluation.html
  https://arxiv.org/abs/2311.09476
  https://arxiv.org/abs/2305.14627
- **Temporal-aware RAG / Verify-RAG**：时间表达归一化 + 引用/groundedness 独立校验。
  https://ieeexplore.ieee.org/document/11402566
  https://zenodo.org/records/21120261

## 4. 优化路线（按 ROI）

### P0 指标补齐（先做）
- 显式 **intent accuracy / 混淆矩阵**（5 类），不只靠 tool F1。
- **工具过度调用率**、首轮 strict/acceptable、tool precision/recall、required coverage。
- **citation quality**（ALCE 式）：每个引用是否支持所在句子；时间/数字是否绑定正确。
- **pass^k** 覆盖 cross_meeting/state，不只简单 12 题。
- fresh / multi-turn 分开；所有指标 6×3 + 配对 CI。

### P1 意图路由（最可能提升 Agent 层）
- 混合路由：规则强信号（“两段录音/上午下午/行动项/负责人/哪几组”）+ LLM 分类器 + 置信度。
- 低置信度不硬路由，回退到通用策略；避免误分类。
- 路由输出：intent + first_tool_policy + max_tool_calls + required_coverage_hint。
- 对 cross_meeting：先 search，禁止先 list_facts；工具预算 3–4。
- 对 state：先 query_meeting_state，工具预算 2–3。
- 对 aggregation：先 list_facts，工具预算 2。
- 评估：intent accuracy、首轮 strict、过度调用率、覆盖率、答案盲评；新 holdout。

### P2 工具协议（不改模型也能做）
- 在 tool schema/description 中明确“先选哪个工具”的 decision tree；
- 对 `list_facts`/`query_meeting_state` 的 triggers 做意图级约束；
- 设置每 intent 的工具预算，避免 cross 1.8×、state 1.75× 的过度调用。

### P3 引用/时间绑定
- 归一化时间表达（9:00/9点/下午六点/发布前一日/9月1日）；
- 每条数字/日期必须绑定到被引 ref 的来源和时间范围；
- 只对“数字不在被引证据”的句子触发校验/修引用，不重写整段。

### P4 检索 precision
- 在意图/工具稳定后再做；当前 precision 噪声 10pp，先别调 top-k/rerank。
- 若继续：结构化切片、answerability rerank、parent 作为生成后阅读层。

## 5. 当前默认

所有实验增强默认关闭：turn filter / evidence hint / CRAG / contextual /
parent-context / floor / planning；top_k=3，rerank on。

## 6. 不做

- 不用全局 prompt 换单题；
- 不在单会议语料上继续调 top-k；
- 不把 judge 单次读数当答案质量；
- 不在多轮会话上直接比较 RAG 指标（历史引用混淆）。

## 7. 意图路由原型结果（2026-10-21）

### 规则分类器
- 27 条轨迹题：**26/27 = 96.3%**（t09 单事实被误判 state）
- 15 条新探针：15/15（规则在这些探针上做过调整，不能当独立泛化）
- 对照：LLM 直接分类 27 条 = 81.5%

### 接入产品（`INTENT_ROUTER_ENABLED`，默认 0）
路由输出 intent + first_tool + max_tool_calls + system hint。

**fresh trajectory 27（每题新 session）**
| 配置 | tool F1 | strict | acceptable | required 覆盖 |
|---|---:|---:|---:|---:|
| router off | 0.7309 | 14/27 | 22/27 | 24/27 |
| router on | **0.8519** | **18/27** | **23/27** | **26/27** |

- cross_meeting strict 0→80%，F1 .667→.933
- state F1 .433→.750，strict 25%→50%
- aggregation F1 .611→.722，coverage 66.7%→100%

**fresh cross30（答案质量）**
| 配置 | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| router off | 75.86% | 63.64% | **88.63%** | 93.33% |
| router on | 75.86% | 63.57% | **83.06%** | 100% |

结论：
- 路由显著改善工具选择/覆盖，但 **faithfulness 下降 5.6pp**；
- 规则在 27 题上调过，泛化不足；
- 因此 **默认保持 0**，代码保留为可选实验开关。
- 下一步应做“选择性路由”（只对 cross_meeting/state 触发）并上新 holdout，而不是全量注入。

## 8. 选择性路由（只 cross_meeting/state）结果

| 配置 | tool F1 | strict | acceptable | coverage |
|---|---:|---:|---:|---:|
| router off | 0.7309 | 14/27 | 22/27 | 24/27 |
| 全量路由 | **0.8519** | **18/27** | **23/27** | **26/27** |
| 选择性路由 | 0.8025 | 17/27 | 21/27 | 24/27 |

fresh cross30：
| 配置 | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| off | 75.86% | 63.64% | **88.63%** | 93.33% |
| 全量 | 75.86% | 63.57% | 83.06% | 100% |
| 选择性 | 77.59% | **73.33%** | **73.53%** | 93.33% |

结论：
- 选择性路由把 precision 提到 73.33%，但 faithfulness 掉到 73.53%；
- 工具指标也不如全量路由；
- 仍是“换指标”而非稳定双赢，默认保持 0。
- 需要先做答案盲评 + 新 holdout；不能仅凭 precision 上线。
