# 优化策略复盘与后续路线（2026-10-20）

## 0. 先承认问题

这一阶段确实有“靠直觉推进”的成分：
- 连续用 prompt/开关试错，虽然每轮都跑了 holdout，但缺少**配对置信区间**和**多轮重复**；
- turn 级过滤的 judge 指标 +17pp 很诱人，但独立盲评显示答案质量没有系统性变化；
- e04 的全局“具体性”提示单题有效，却让 cross30 全面回归。

这份文档把“证据”和“想法”分开，后续按预注册实验推进。

## 1. 项目现状盘点

### 架构
- 检索：pgvector dense + ParadeDB BM25，RRF 融合，cross-encoder rerank，top-k=3。
- 生成：LangGraph 单 Agent + 5 个只读工具，工具结果带 ref，回答强制引用。
- 记忆：会议系列/长期记忆/待办/草稿纪要；状态查询只读 CONFIRMED + 有效待办。
- 观测：ai_call_log 记录 token/耗时/成本；有成本页与在线评测。
- 评测：RAGAS 式 context recall/precision/faithfulness/relevance，轨迹工具指标，answer review。

### 当前有效读数（默认配置，turn filter off）
| 集合 | recall | precision | faithfulness | relevancy | 备注 |
|---|---:|---:|---:|---:|---|
| cross30 | 76.79% | 68.87% | 81.86% | 100% | 30 题，AI 复核 gold |
| holdout E | 88.24% | 73.02% | 89.58% | 90% | 20 题，独立事实复核后 |
| holdout A | 96.88% | 60.66% | 89.83% | 95% | 旧配置产物 |
| holdout D | — | — | — | — | 检索级 3-way 过滤验证 |

### 主要短板
1. **context precision 60–77%**：同一场会的相邻/宽泛窗口混入；多工具重复上下文。
2. **e04 类失败**：检索同时返回“四类账号”（115s）和“管理员和员工两类”（653s），
   reranker 把宽泛条目排第一，生成采用宽泛条目。这不是“没检索到”，是**证据选择**。
3. **judge 噪声**：单 judge、单次运行、分母小；point estimate 容易误导。
4. **单会议语料**：holdout A–E 都是同一场会，题目/事实高度重叠，泛化结论弱。
5. **成本不可算**：所有调用 unpriced，只有 rerank 有价格。

## 2. 本轮新增的测量证据

新增 `evaluation/analysis/paired_bootstrap.py`：per-case 配对 bootstrap（零模型调用），
避免只看总 rate。结果：

| 对比 | precision 差值 [95% CI] | faithfulness 差值 [95% CI] |
|---|---|---|
| turn filter on-off（cross30） | +0.061 [−0.003, 0.122] | −0.058 [−0.123, 0.002] |
| turn filter on-off（holdout E） | +0.150 [0.042, 0.267] | +0.064 [−0.018, 0.182] |
| 具体性提示-baseline（cross30） | −0.053 [−0.111, −0.006] | −0.071 [−0.148, −0.003] |

解读：
- turn filter 在 cross30 的 precision 提升 CI 跨 0，不显著；holdout E precision 显著，
  但独立盲评两臂答案质量一致（19/1/0 vs 19/1/0），说明是**上下文集合/judge 效应**。
- 具体性提示的 precision/faithfulness 回归 CI 不含 0，属于真实回归，不是噪声。
- 之后任何“提升”必须先看配对 CI + 独立答案盲评，不能只看总 rate。

## 3. 联网调研结论

### 评测
- RAGAS 把检索与生成拆成 context precision/recall（检索）和 faithfulness/answer relevance（生成）；
  指标是 LLM judge，适合做相对比较，不适合单次绝对结论。
  [RAGAS evaluation](https://docs.ragas.io/en/v0.1.21/getstarted/evaluation.html)
- ARES 用少量人工标注 + PPI 校准轻量 judge，说明**judge 需要校准/置信区间**，不能裸用单 judge。
  [ARES](https://arxiv.org/abs/2311.09476)
- LLM-as-a-judge 存在 position/verbosity/self-preference 偏差，需要多 judge/盲评/校准。
- 结论：我们的答案盲评应作为主质量指标；judge 指标作为诊断，不单独作为上线依据。

### 检索
- Anthropic Contextual Retrieval 报告 top-20 失败率 -49%、+rerank -67%，但需要索引期成本；
  本单会议语料未复现收益，说明**语料规模/分布**决定方法是否值得。
  [Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
- CRAG 用检索评估器 + 纠错检索；适合“召回质量不稳定”的场景，但会增加延迟/调用。
  [CRAG](https://www.kore.ai/blog/corrective-rag-crag)
- Self-RAG 用反思 token/自我批判控制检索与生成；理念可借用为“条件化验证”，
  但完整训练式实现不适合当前工程。
  [Self-RAG](https://research.ibm.com/publications/self-rag-learning-to-retrieve-generate-and-critique-through-self-reflection)
- 工业界常见的 parent-child / small-to-big、query transformation、multi-query 都针对
  “chunk 粒度与问题粒度不匹配”；这类是结构改进，不是 prompt 补丁。

### 评测方法
- 需要 dev/holdout 分离、预注册指标、多轮重复、配对检验；单次 +17pp 不足以决策。
- 对 RAG，检索指标和答案指标要分开归因，否则会把“过滤掉上下文”误当成“回答更好”。

## 4. 后续路线（按优先级）

### P0：把测量做对（1–2 天，零产品风险）
1. 建 **unseen-meeting holdout**：至少 3–5 场新会议，问题不与现有 30/27 题重叠。
2. 每个配置跑 **3 次**（温度固定也可/开小随机），报告配对 bootstrap CI。
3. 答案质量主指标：独立子 Agent 盲评（correct/partial/wrong + 偏好）；
   judge 指标只做诊断。
4. 建立 **failure taxonomy**：检索未召回 / 排序错 / 证据冲突 / 生成选错 / 引用缺失 / 问题歧义。

### P1：e04 类“证据选择”问题（优先，条件化）
- 触发条件：同一问题检索到 ≥2 条都相关、但粒度/范围不同的证据（如“四类账号”vs“两种角色”）。
- 做法：答案生成后做一次**claim-evidence 校验**，只在该条件下触发；检查答案是否用了
  更宽泛的相似条目替代精确条目，必要时要求模型修订。
- 预期：修 e04，不全局改 prompt；成本只在冲突题 +1 次调用。
- 验证：e04 + 新 holdout F；必须同时看盲评和 cross30/holdout 回归。

### P2：检索结构改进
- **parent-child / small-to-big**：child 精确召回，返回 parent 上下文；先离线比较
  context precision/recall + 答案盲评，不直接改默认。
- **scope-aware query decomposition**：只对跨会议/多实体问题做分解；之前 planning
  无稳定收益，必须按 query 类型分层看。
- **listwise rerank/answerability rerank**：用 LLM 对候选做“能否回答问题”排序；
  成本高于 cross-encoder，只在 top-k 边界歧义时触发。

### P3：生成忠实度
- claim-level citation verification：每个事实句必须有引用且被引用上下文支持；
  不通过则删/改，而不是补一句“资料不足”。
- 只在高价值场景开启，避免每题多轮调用。

### 不做/降级
- 不再加全局 prompt 规则换单题。
- 不再默认开 turn filter/CRAG/contextual/floor/top-k 截断。
- 不在单会议语料上继续调参；先把语料与测量扩起来。

## 5. 下一步具体动作

1. 用当前 9 条 partial + e04 建 failure taxonomy（零模型调用）。
2. 做 e04 的**条件化 claim-evidence 校验**原型；在 e04 + cross30 上回归。
3. 新建 unseen-meeting holdout F；独立复核后再看是否值得上线。
4. 给 chat/embedding 配真实单价，否则成本指标永远不可用。
