# 2026-10-20 独立 Agent 代审 + 优化 + A/B 复现实验（完成版）

- 用户授权独立子 Agent 代人工审核；共 4 个独立复核 Agent。
- 不设调用上限：产品侧累计 **4,759 次**调用，记账 **¥1.8015**。
- A/B 复现已完成（两轮 30×2）；结论：无稳定收益，保持 `KB_QUERY_PLAN_ENABLED=0`。

## 1. 独立审核结果

### gold 事实
- 58 条 expected_fact：**56 approve / 2 revise / 0 reject**；2 条 revise 已入 gold。
- `label_source=independent_agent_reviewed`、`human_checked=false`；独立 Agent 代审不等于人工签名。

### 答案
| 轮次 | correct | partial | wrong |
|---|---:|---:|---:|
| 修复前 | 20 | 6 | 4 |
| **最终（独立复核）** | **21** | **9** | **0** |

最终 partial：cm02（漏第 5 项风险）、cs08（未回退草稿纪要 4）、cs02（未补 OPEN 待办）、
ct02（漏 3 个中间节点）、cm03、ct06、cm07、ct08、ct09（各 1 项关键遗漏）。**无 wrong。**

## 2. 已修复的产品问题

1. series_id / meeting_id 混用 → 系列校验 + id 命名空间提示；4 条系列题全修复。
2. 候选记忆不可见 → `include_proposed=true`，cs02 能答 PROPOSED。
3. 草稿纪要回退 → `get_minutes(include_draft=true)`，cs06 能答 DRAFT 纪要 4 三项结论并标注。
4. 状态查询模糊匹配 → `移动端附件下载修复` 能命中完整条目。
5. 偏好归属 → `owner_name` 返回，cs04 正确。
6. 运行库与 seed 对齐，消除评测环境偏差。
7. 完整清单提示 + 工具 notice；cm02 仍未完全修复。

## 3. 最终指标

### 轨迹 27 题（AI-reviewed gold）
| 指标 | 读数 |
|---|---:|
| tool_call_f1 | **0.8272** |
| 首轮 strict | 19/27 = 70.4% |
| 首轮 acceptable | 25/27 = 92.6% |
| required 覆盖 | 25/27 = 92.6% |
| must_cite | 25/25 = 100% |
| 禁止工具 | 0 |

### pass^3（12 题 ×3）
- **12/12 = 100%**（CP 95% 73.5–100）
- gold 含 4 条 AI 复核修正；不能当独立泛化证据。

### 跨会议 A/B（两轮 30×2，独立复核 draft-gold）
| 轮次 | recall 关→开 | precision 关→开 | faithfulness 关→开 | relevancy 关→开 |
|---|---|---|---|---|
| 第一轮 | 75.86% → 82.76%（+6.90） | 69.81% → 68.38%（−1.43） | 81.08% → 79.75% | 96.67% → 100% |
| 复现轮 | 82.14% → 82.76%（+0.62） | 66.37% → 68.94%（+2.57） | 80.50% → 84.81% | 93.33% → 96.67% |

两轮方向不稳定：规划臂 recall 都是 82.76%，关臂从 75.86% 波动到 82.14%；
不能把第一轮 +6.90pp 当稳定收益。**保持规划关闭。**

### 注入（最后线上轮）
- 注入 10/10 检出、良性误报 0/20、系统提示泄露 0/30、跨会议外发 0/10。

## 4. 未完成 / 限制

- 9 条 partial 未继续修复（用户要求停止等待后仅补了 A/B 复现）。
- `human_checked=false`：独立 Agent 代审不是人工签名。
- 单会议语料；成本路由只有 5 题试点且两模型未定价，不能报金额降幅。
- 语料时间线仍矛盾：录音 2026-09-10、minutes deadline 2023、seed action 2026；
  cm01 等答案会沿用源字段年份。

## 8. 泛化 holdout（未用于调参，一次性读数）

为避免继续在调参集上打补丁，本轮另建 `evaluation/gold/holdout_generalization_20.json`：
- 20 个**此前未出现**的问题，32 条 expected_fact，覆盖两段录音/纪要/待办的不同侧面；
- 事实经独立子 Agent 复核：**30 approve / 2 revise / 0 reject**；2 条已修订；
- `label_source=independent_agent_reviewed`、`human_checked=false`、`do_not_tune=true`；
- 产品 prompt 先移除具体案例式表述（如具体时点/具体事项），再**只跑一次** holdout，未据其结果调 prompt。

| 指标 | 读数 |
|---|---:|
| context_recall | 31/32 = **96.88%**（CP 95% 83.78–99.92%） |
| context_precision | 37/61 = 60.66% |
| faithfulness | 106/118 = **89.83%**（CP 95% 82.91–94.63%） |
| answer_relevancy | 19/20 = 95.0% |

解读：在这组未参与调参的问题上，召回与忠实度都高，说明前面修的系列 id、草稿回退、
模糊查询、偏好归属等是**通用机制**，不只是对 27/30 题过拟合；precision 60.66% 说明
仍会带回一些边缘上下文，但回答忠实度没有随之下降。该读数只报一次，不再据此改产品。

## 9. 最终结论

- 查询规划：两轮 A/B 无稳定收益，保持关闭。
- 通用机制：series/meeting id 校验、include_proposed、草稿回退、状态模糊匹配、
  偏好归属、完整清单提示，均保留。
- 泛化：holdout 20 题 context_recall 96.88%、faithfulness 89.83%，未用于调参。
- 剩余限制：9 条调参集 partial 未逐条修；独立 Agent 代审不是人工签名；
  单会议语料、语料时间线仍有矛盾。

## 10. 检索质量调研与实验

### 调研结论
- RAGAS Context Precision 是 rank-aware 指标：它惩罚“检索到但无用”的切片，
  因此**单纯截断 top-k** 会提高 precision 但往往牺牲 recall/faithfulness。
  [RAGAS Context Precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)
- Anthropic Contextual Retrieval：为每个切片生成文档级上下文再嵌入/建 BM25，
  报告 top-20 检索失败率降低 49%，加 rerank 后降低 67%；代价是索引期额外模型调用。
  [Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
- CRAG 类方法：用检索评估器判断召回质量，再决定“直接生成/纠错检索/放弃”，
  本质是把 retrieval precision 的判断从静态阈值改成按 query 的自适应判断。
  [Corrective RAG](https://www.kore.ai/blog/corrective-rag-crag)

### 实验 1：top-k 截断（cross30 开发集，同 prompt）
| 配置 | context_recall | context_precision | faithfulness | answer_relevancy |
|---|---:|---:|---:|---:|
| top_k=3（当前） | **80.36%** | 67.27% | **79.10%** | 100% |
| top_k=2 | 65.52% | **69.41%** | 73.36% | 96.67% |

结论：precision 只 +2.14pp，recall 掉 14.84pp、faithfulness 掉 5.74pp；
**拒绝用截断换 precision**，恢复 top_k=3。

### 实验 2：Contextual Retrieval（已跑，拒绝）
- 账户恢复后开启 `KB_CONTEXTUAL_ENABLED=1`，重建 6/7 号录音与 minutes 3；
  切片 `embed_text` 已带 `[会议/日期/时间/说话人] + [上下文]` 前缀。
- cross30 开发集对比：

| 配置 | context_recall | context_precision | faithfulness | answer_relevancy |
|---|---:|---:|---:|---:|
| 关闭（当前） | **80.36%** | 67.27% | **79.10%** | 100% |
| Contextual | 72.41% | 68.00% | 73.64% | 93.33% |

recall −7.95pp、faithfulness −5.46pp，precision 仅 +0.73pp → **拒绝**，已关闭并重建
回无上下文的基线索引。Anthropic 的收益依赖更大语料/更强嵌入；本单会议小语料上
没有复现。

### 实验 3：相对 rerank 分数下限（开发集为正，holdout B 拒绝）
- 机制：保留 `score >= top_score * RERANK_SCORE_FLOOR` 的重排结果；默认 0。
- cross30 开发集 floor=0.5：recall 80.36%→82.14%、precision 67.27%→71.57%（+4.29pp）、
  faithfulness 79.10%→80.75%；看起来很好。
- 在**全新 holdout B 20 题**（未用于调参）上：

| 配置 | context_recall | context_precision | faithfulness | answer_relevancy |
|---|---:|---:|---:|---:|
| floor=0 | **96.43%** | **68.85%** | **93.33%** | 95% |
| floor=0.5 | 96.43% | 65.57% | 93.15% | 95% |

precision **−3.28pp**，预注册“+3pp”不达标 → **拒绝**，默认保持 0。
这是本轮最重要的 anti-overfit 证据：开发集 +4.29pp 在 holdout 上反转为 −3.28pp。

### 系统性修复：嵌入故障降级
- `search_knowledge` 在查询嵌入失败时不再 500，自动降级为 **BM25-only**，
  并记录异常；dense 失败/BM25 失败仍各自不阻断另一臂。
- 新增单测 `test_embedding_failure_falls_back_to_bm25`。
- 当前线上检索处于 BM25-only（DashScope 账户欠费），dense 不可用。

## 11. 当前阻塞

- DashScope 返回 `Arrearage`：查询嵌入失败，产品侧 qwen/embedding 付费调用
  无法继续；**本轮之后不能再跑质量评测**，需要账户充值。
- DeepSeek judge 是独立账户，仍可用；但它只能判已有产物，不能修复产品检索。
- 因此本轮不再声称任何新的质量提升；top-k/contextual retrieval 实验均按“未通过/未完成”记录。

## 12. 结论：本轮没有启用任何新检索默认

- top-k 截断：拒绝（recall −14.84pp）
- Contextual Retrieval：拒绝（recall −7.95pp、faithfulness −5.46pp）
- rerank 相对 floor：开发集 +4.29pp，但 holdout B precision −3.28pp，拒绝
- 唯一保留的新增能力：**嵌入故障时 BM25-only 降级**（可靠性，不是质量提升）
- 所有默认恢复：top_k=3、contextual off、rerank floor=0、planning off

## 13. CRAG 式上下文过滤实验（预注册验证，未启用）

### 方法
对 rerank 后的候选上下文，用 qwen 逐条判“是否可能包含回答问题所需信息”，
DeepSeek judge 独立重判过滤后的 context precision / recall；不改产品默认。
开发集 cross30 先调评估器提示词，再在**全新 holdout C 20 题/22 事实**上一次性验证。

### cross30 开发集
| 版本 | context_precision | context_recall |
|---|---:|---:|
| 基线 | 67.57% | 77.59% |
| 过滤（初版） | **85.19%** | 75.86% |
| 过滤（保守提示） | **83.53%** | 75.86% |

### holdout C（未参与调参）
| 配置 | context_precision | context_recall |
|---|---:|---:|
| 基线 | 66.67% | **100%** |
| CRAG 过滤 | **92.86%** | 95.45% |

- precision **+26.19pp**，但 recall **−4.55pp**（丢 1 条支撑上下文）
- 预注册门槛为“precision ≥+5pp 且 recall 下降 ≤3pp” → **未通过**
- 因此不实现为默认；记录为“高 precision、低召回代价”的候选方案

### 结论
- CRAG 是目前最有希望提升 context precision 的通用方法（开发集和 holdout 都大幅提升）
- 但当前评估器会误删 1 条部分相关的上下文；需要增加“安全底线”（例如强制保留
  rerank top-k 或歧义上下文）并在**新的 holdout D** 上重验，不能直接上线
- 所有产品默认保持不变：top_k=3、contextual off、rerank floor=0、planning off

## 14. 延迟 / 成本基线与优化

详细表：`evaluation/reports/2026-10-20_latency_cost.md`；脚本：
`evaluation/benchmarks/latency_cost.py`。

- 累计 13,723 次调用、44.46M token、平均 1.89s；全部调用标记 unpriced，
  ¥5.124651 只是 rerank 的记账，**不能当真实账单**。
- 主要延迟不在检索：CHAT 平均 2.4s、CHAT_SUMMARY 平均 20.7s；
  embed 310ms + rerank 274ms。
- 已做优化：dense 与 BM25 由串行改 `asyncio.gather` 并发；临时 10 query
  实测 mean 629.5→543.3ms、p95 820→617ms（小样本，不作稳定收益承诺）。
- 未启用 CRAG 过滤：多一次生成模型调用，延迟与成本上升，且 holdout C
  recall 未达预注册门槛。
- 下一步成本杠杆：补模型单价；评估 summary 用更便宜模型/降频/限 token。

## 15. CRAG 3-way 过滤：检索级通过，产品级否决

### 检索级（离线，holdout D 一次性验证）
- 方案：needed/maybe/no 三分类，只删 no，保留 needed+maybe。
- holdout D（20 题/33 事实，未调参）：

| 配置 | context_precision | context_recall |
|---|---:|---:|
| 基线 | 72.13% | **90.91%** |
| 3-way 过滤 | **84.91%（+12.78pp）** | **90.91%（0）** |

检索级达到预注册门槛（precision +5pp、recall 不降）。

### 产品级（cross30，过滤在每次 search 内执行）
| 配置 | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| 基线 | **80.36%** | 67.27% | 79.10% | 100% |
| 产品过滤 on | 74.14%（−6.22pp） | 68.87%（+1.60pp） | 79.26% | 96.67% |

- 过滤调用本身：**28 次、平均 3,687ms、99,729 tokens、全部 unpriced**
- 结论：产品级 recall 明显下降、precision 只 +1.6pp，延迟/成本却显著上升 → **否决**。
- 原因：过滤加在“每次 search”上，每次只看到部分候选，容易删掉后续子查询/合并时要用的上下文；
  正确做法应是**按 turn 合并后的最终上下文过滤一次**，或用更便宜的评估器。
- `KB_RELEVANCE_FILTER_ENABLED` 默认保持 **0**；代码保留为可选实验开关，便于后续按最终上下文重做。

## 16. 泛化 / 过拟合自查

完整审计：`evaluation/reports/2026-10-20_generalization_audit.md`。

- 清理了产品工具描述/注释里的语料特定示例（报表导出、发布范围、移动端附件等），
  改为通用表述；未按单题写产品规则。
- 高风险机制全部默认关闭：top_k=2、contextual、rerank floor、CRAG filter。
- 发现：holdout C/D 未做独立事实复核、gold 部分 AI 复核、同语料多 holdout，
  只能当 sanity check，不能宣称强泛化。
- prompt 去案例化后 cross30 单轮：recall 76.79%、precision 68.87%、
  faithfulness 81.86%、relevance 100%；与改动前互有升降，不声称提升。
- 下一步只做架构级：turn 级最终上下文过滤 / 更强检索评估器 / 真实单价成本。

## 17. Turn 级上下文过滤：judge 指标好，但独立盲评未见质量收益（默认关闭）

### cross30 开发集
| 配置 | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| 无过滤 | 76.79% | 68.87% | **81.86%** | **100%** |
| turn 级过滤（污染修复后） | **82.14%** | **78.82%** | 74.55% | 96.67% |

### holdout E（20 题，未调参，off/on 两臂）
| 配置 | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| off | 88.24% | 73.02% | 89.58% | 90.00% |
| **on** | **97.06%（+8.82pp）** | **89.80%（+16.78pp）** | **97.56%（+7.98pp）** | **100%** |

- 按 judge 读数，预注册门槛全部通过；但独立盲评 v2 显示两臂答案
  19 correct / 1 partial / 0 wrong 完全一致（18 tie、2 偏好 filter_on），
  **没有系统性答案质量差异**。cross30 上 faithfulness 反而从 81.86% 降到 74.55%。
- 这是本轮唯一达到“precision 大幅提升、recall/faithfulness 不降”的通用机制。
- 机制：模型调用前对本轮**累积工具结果**做一次 needed/maybe/no 过滤；不是每次 search 内过滤。
- 成本/延迟：`KB_TURN_FILTER` 60 次、平均 **1,471.77ms/次**、59,577 tokens；
  约 1.2 次/题，折合每题约 **+1.8s**；比 per-search 过滤（3.7s/次）便宜一半以上。
- **独立盲评 v2（修复 labels 污染和 e04/e09 事实后）**：A/B 两臂都是
  19 correct / 1 partial / 0 wrong；18 组 tie，仅 2 组偏好 filter_on（e16/e18）。
  **没有系统性答案质量提升或退化**；唯一共同失败 e04（seg14 四类账号未被召回到）。
- 同时修复了 turn 过滤内部调用把 labels JSON 流式混入答案的 bug；修复前 turn-on
  答案全部带标签污染，之前那组 judge 指标不能直接用。
- 因此 judge 侧的 precision/faithfulness 提升很可能部分是“过滤后 judge 只看更少上下文”
  的上下文集合效应，不能直接当生成质量提升。
- 决定：`KB_TURN_FILTER_ENABLED` **默认 0**；代码保留为可选实验开关。
- 仍不声称通用泛化：holdout E 是 AI 起草、同语料、单次读数；需要人工/第三方复验。

## 18. e04 共同失败与“具体性”提示实验（拒绝）

独立盲评发现 e04 两臂都错：问“权限验证覆盖哪几类账号”，答案取的是 653s 的
“管理员和员工两种角色”，没有用 115s 的“管理员、会议创建人、主持人、普通参会人员四类账号”。

尝试在通用提示词里加“优先采用直接回答问题、粒度更具体的条目”。结果：
- 单题 e04：修好了，能列出四类账号并说明 653s 是角色维度概括；
- cross30 全局：recall 76.79%→70.69%、precision 68.87%→63.30%、
  faithfulness 81.86%→76.17%、relevance 100%→93.33% → **明显回归**；
- 结论：这是用全局提示词换单题，属于典型过拟合，**已回滚**。

e04 仍是共同失败点；下一步应在**检索/答案校验层**处理“精确条目 vs 更宽泛条目”，
而不是再写一条全局 prompt。

## 19. 条件化证据冲突提示（原型，默认关闭）

failure taxonomy（`evaluation/reports/2026-10-20_failure_taxonomy.md`）显示：
9 条 partial 里主要矛盾不是“检索不到”，而是证据选择、跨工具覆盖、关键条目完整性。
e04 是典型：检索同时有“四类账号”(115s) 和“管理员和员工两类”(653s)。

实现一个**条件化**提示（不是全局规则）：
- 仅当问题含“几类/哪几类/几项/几个/多少/哪些入口”且上下文出现至少两个不同计数口径时触发；
- 追加一句“核对问题对象、优先直接回答的精确条目、多个口径并列说明”；
- 不满足条件完全不改消息；不调额外模型。

### 原型读数
- 单题 fresh session e04：提示能触发，答案同时列出四类账号与“管理员/员工”角色口径。
- 但 holdout E 的独立盲评 v3 中，两臂 e04 仍同错（都答“管理员/员工两类”），
  19/20 组事实一致；**提示没有可辨识的系统性效果**。
- holdout E（judge，hint on vs off）：
  | 指标 | off | on |
  |---|---:|---:|
  | recall | 88.24% | 97.06% |
  | precision | 73.02% | 78.33% |
  | faithfulness | 89.58% | 95.92% |
  | relevancy | 90.00% | 100% |
- cross10 子集：逐题指标基本不变（仅 cm05 precision 0.5→1.0 等小波动），未见全局回归。

### 当前决定
`KB_EVIDENCE_HINT_ENABLED` 默认保持 **0**：
- 单题 fresh session 的修复没有在 holdout E 盲评中复现，不能当“修好 e04”；
- 触发条件/注入位置仍需排查（为什么 holdout E 的 e04 未触发或未生效）；
- 触发面窄、holdout E 是 AI 起草且仅单次；
- 还需独立盲评 + 完整 cross30 回归，才能考虑默认开启。

## 20. P0 多轮测量：未完成（如实记录）

尝试对 cross10 做 3 轮基线，但单轮实际耗时远超预期（最后一题长时间未返回），
在 r1 完成后中止 r2/r3。**没有把未完成数据写进结论。**

- r1 cross10：recall 100%、precision 61.67%、faithfulness 86.96%、relevance 100%
- 该读数只能当一次观测，不能当方差/稳定性估计
- 多轮 P0 需要更小的固定题集（例如 6 题）或后台任务队列，不能再占用主会话数十分钟

## 21. P0 多轮测量完成（fresh session × 3）

之前多轮跑不完的根因是：同一 session 连问 6–30 题，历史越来越长，单题延迟不断上升。
改成 **每个问题新建 session** 后，3 题 ×3 轮顺利完成。

新增脚本：`evaluation/benchmarks/ask_fresh_sessions.py`（只走产品 HTTP）。

cross3，3 轮：

| trial | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| 1 | 100% | 63.16% | 87.88% | 100% |
| 2 | 100% | 63.16% | 85.71% | 100% |
| 3 | 100% | **52.63%** | 96.55% | 100% |

- recall/relevancy 稳定；**precision 一轮就波动 10.5pp**，faithfulness 波动 10.8pp。
- 这证明了之前“单轮 +17pp 就上线”的风险：小样本指标噪声很大，必须看多轮和 CI。
- 仍只有 3 题，不能当总体方差；下一步应扩到 6 题×3 轮。

## 22. P0 扩到 6×3 + P2 parent-context 实验

### P0 6 题 ×3 轮（fresh session）
| trial | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| 1 | 100% | 58.54% | 91.80% | 100% |
| 2 | 91.67% | 65.85% | 86.21% | 100% |
| 3 | 100% | 68.29% | 94.23% | 100% |
| **mean** | **97.22%** | **64.23%** | **90.75%** | **100%** |

- precision range **58.54–68.29%（9.75pp）**、faithfulness range **86.21–94.23%（8pp）**
- 这组是后续默认判断的基线与噪声带；单轮小差异不能当收益。

### P2 parent-context（相邻 transcript 窗口扩展）— 否决
- 机制：child 检索/重排后，把 ±30s 相邻窗口拼回 hit.text。
- cross6 单轮 vs P0 mean：

| 配置 | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| P0 mean | 97.22% | 64.23% | 90.75% | 100% |
| parent-context | **33.33%** | **13.80%** | 78.33% | 66.67% |

- 严重退化：扩展后的长上下文破坏了引用/证据定位，judge 与答案都变差。
- 默认关闭 `KB_PARENT_CONTEXT_ENABLED=0`；代码保留为实验开关。
- 结论：parent-child 不能只拼长文本，必须保留 child 引用身份、parent 仅作阅读上下文，
  否则 citation/ref 与上下文分母都会失真。

### P2 第二版：child 引用 + parent_text 阅读字段（仍未通过）
- 修正第一版：child 的 text/quote 不变，另加 `parent_text`（±30s 相邻窗口）作为阅读材料。
- cross6 对比 P0 mean：

| 配置 | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| P0 mean | 97.22% | 64.23% | 90.75% | 100% |
| parent_text | **50.00%** | **15.30%** | 77.60% | 66.67% |

- 仍然全面退化：即使不替换 child 文本，parent_text 进入工具结果后污染上下文/答案选择。
- 结论：在当前 agent/tool 架构里，parent 上下文不能直接塞进检索结果；如果要做，
  应作为独立阅读层并在答案生成后再合并，而不是放进 citation/tool payload。
- 默认保持 `KB_PARENT_CONTEXT_ENABLED=0`。

## 24. P3 claim/citation 审计（零模型调用原型）

新增 `evaluation/analysis/claim_citation_audit.py`：逐句提取带引用的事实断言，
检查其中的数字/日期是否出现在**被引用上下文**、以及是否出现在**本轮任一上下文**。

| 数据集 | 数字断言 | 不在被引上下文 | 不在任何上下文 |
|---|---:|---:|---:|
| cross30 | 188 | 48（25.5%） | 43（22.9%） |
| holdout E | 50 | 1（2.0%） | 1（2.0%） |

- holdout E 基本干净；cross30 的 22.9% 需要人工/LLM 逐条判“真 hallucination”还是
  “从多个上下文/会议日期/计数派生”，不能直接当幻觉率。
- 该审计只做可判定部分，下一步应加 claim-level LLM 校验，并只对“多证据冲突/引用缺失”
  的句子触发；不能全量拦截。
- 产品默认不变；P3 尚未集成。

## 25. P3 下一步：claim 校验结果（cross30 48 个缺口）

新增 `evaluation/experiments/claim_verify.py`，用 DeepSeek 对每个“数字不在被引上下文”的断言
做 5 类判定（supported_by_cited / supported_by_other_context / derived / citation_mapping / unsupported）。

48 个缺口的判定：

| 类别 | 数量 |
|---|---:|
| citation_mapping（引用编号/时间定位/会议日期元数据，不是事实断言） | 18 |
| supported_by_cited（文本核对误报/被引实际支持） | 11 |
| **unsupported（真不支撑）** | **18** |
| derived（由上下文计算/合并） | 1 |

解读：
- 规则审计的 25.5% 明显高估；LLM 校验后真 unsupported 为 18 个（占 188 个数字断言的
  **9.6%**），仍不可忽略。
- 例子集中在 ct06/ct08 的“9月1日/9:00/18:00”等时间归属错误，属于**引用/时间绑定**问题，
  不是数字编造。
- 这支持 P3 的下一步：只对“引用缺失/数字不在被引上下文”的句子做 claim 校验，
  并且区分 citation_mapping 与 unsupported，不能全量拦截。
- 产品默认不变；P3 尚未集成。
