# RAG 与 Agent 通用优化调研（面向 MeetingBot 的工程决策）

> 调研时间：2026-10-22　｜　作用域：会议纪要 Agent + 会议知识库 RAG 问答（FastAPI + Vue + PostgreSQL pgvector/BM25 + rerank + LangGraph 单 Agent 工具循环）
> 交付约束：**只做调研，不改代码、不跑模型调用**；本文所有外部链接均经 `web_search`/`web_fetch`/HTTP 实测核验（2026-10-22），数据集与任务差异在证据栏注明。
> 与既有文档的关系：本文**不重复**项目已试并默认关闭的机制（query plan 提示、turn filter、CRAG 3-way、contextual retrieval、parent context、dedupe、rerank score floor、意图路由/强制首工具、证据边界 prompt）为「新方法」；对它们的复盘见 §7，只有在满足明确重开条件时才建议重试。

---

## 0. 阅读指南：本文如何对应到你的指标

### 0.1 现状基线（引用既有读数，不重新测量）

| 集合 | context_recall | context_precision | faithfulness | answer_relevancy | 口径/备注 |
|---|---:|---:|---:|---:|---|
| mixed100（组装集，100 题/167 facts） | 91.0% | 71.8% | 89.2% | 97.0% | 跨 5 个来源组装，非单次运行 |
| 其中 cross30 分层 | **81.7%** | 74.6% | **78.6%** | — | 主短板：跨录音/系列题 |
| fresh cross30（单次 fresh session） | 75.9% | 63.6% | 88.6% | 93.3% | 与上面不是同一批产物，不可混算 |
| new meeting unscoped（合成新会议） | 87.0% | 71.0% | 80.0% | 100% | 合成语料，只能作 smoke 之外的 realistic 层 |
| claim 规则审计（cross30） | — | — | 18.3% 数字不在**被引**上下文；9.4% 不在任何上下文（fresh） | — | 规则审计 25.5% 高估；深判后真 unsupported ≈ 9.6%（188 个数字断言） |
| 盲评 40 条 | — | — | 34 correct / 6 partial / 0 wrong | — | partial 集中在**引用归属**与**推断边界** |
| 工具轨迹（27 题） | required coverage 24/27 | — | — | — | macro tool F1 0.731；over-call 混合：12 只多调、3 既漏又多、0 只漏 |
| 人工金标 | **0 条** | | | | 全部 gold 为 `human_checked=false`（评测设计审计） |

### 0.2 失败模式编号（后文所有机制都映射到这里的编号）

| 编号 | 失败模式 | 主要证据 |
|---|---|---|
| **F1** | 跨来源/系列/多跳问题的**召回不完整**（不是单点检索不到，而是"该覆盖的来源没覆盖"） | cross30 recall 81.7%（分层）/75.9%（fresh）；tool coverage 24/27 |
| **F2** | **引用归属错误与推断越界**：数字/时间/引语被绑定到错的来源，或材料里没有的细节被写成确定事实 | 18.3% 不在被引上下文；盲评 6/40 partial；深判 48 缺口里 citation_mapping 18 + unsupported 18 |
| **F3** | 上下文精度被"同主题但答不了问题"的窗口稀释；精度增益多来自**分母变小**而非分子变大 | mixed100 precision 71.8%；300×3 时 84 条上下文 68 条有用，500×5 时 137 条 74 条有用 |
| **F4** | 工具调用**没有充分性/停止判据**：多调是"补证据"与"瞎探索"混合，按数量压会伤忠实度 | F1 0.731；18 次 extra call 中 7 次无引用贡献、11 次有贡献；4 种压制方案 faith 全降 |
| **F5** | 时间/元数据限定**没有检索维度**：相对时间不归一、录音/系列范围不成为过滤条件 | q26 历史 0/3；"最近七天""下午这场会"无维度可查 |
| **F6** | 评测**不独立、无人工锚点、无区间**：holdout 参与调参、组装集、合成语料、单裁判 | 1032 原始 case / 259 唯一题面 / 0 人工金标；quality gate 曾贴当前分数设阈值 |
| **F7** | **同族判分与按题过拟合**：judge 与生成器同族、27 题上调规则、单会议调参 | self-preference 研究；rerank floor 在 dev +4.29pp、holdout −3.28pp 反例 |

### 0.3 证据分级

- **A 级**：多数据集/多基准验证，或大规模人工标注基准，结论方向稳定（如 IRCoT、ALCE、τ-bench、RAGChecker）。
- **B 级**：单论文/单领域/合成数据，只能当**方向性证据**（如 RAG-Fusion、DRAGIN、Query Expansion）。
- **C 级**：官方工程文档/技术报告，无对照实验，但可用于"业界已这么做"的工程判断（如 Anthropic Contextual Retrieval、Qdrant/Azure 过滤文档、Elastic RRF）。
- **引用纪律**：凡论文结论，均标注其数据集/任务；**不把论文数字外推成本项目预期**。本文给出的"预期指标影响"是本项目口径下的**可检验假设**，不是承诺。

---

## 1. 结论先行（P0/P1/P2 清单与指标）

### 1.1 一句话判断

> 本项目当前的主要矛盾**不是"检索不到"**（recall 在多数集上 87–95%），而是三件事：
> ① **跨来源证据的"覆盖"没有变成结构化的、可执行的状态**（F1/F4）；
> ② **生成后的"claim→引用"绑定没有被校验和修复**，只被"标注"（F2/F3）；
> ③ **判分器与评测集不足以支撑工程决策**，导致"提升"不可信、容易按题过拟合（F6/F7）。
> 因此 P0 不是再写一版 prompt，而是**把已试机制的失败模式换一层解决**：从"提示词层"下移到"数据结构 / 工具契约 / 后处理流水线 / 评测协议"层。

### 1.2 P0（1–3 周，先做这三件；不做完不建议碰 P1 的参数调优）

| 编号 | 机制 | 解决的失败模式 | 预期指标（本项目口径，待验证） | 关键外部证据 | 与已试机制的关系 |
|---|---|---|---|---|---|
| **P0-1** | **Claim→span 引用校验与重归属流水线**（生成后：拆 claim → 逐条对 span 校验 → 只重绑引用/降级措辞，不改事实；规则高精度 + 小模型裁决） | F2，兼修 F3 的"引用噪声" | cross30 faithfulness 78.6%→**83–86%**；数字不在被引上下文 18.3%→**≤8%**；盲评 partial 6/40→**≤3/40**；context_precision **不降**（不动上下文集合）；延迟 +0.5–1.5s/轮 | CoVe（ACL Findings 2024）、RARR（ACL 2023）、SelfCite（2025）、CiteFix（ACL 2025 Industry）、ALCE（EMNLP 2023）、AttributionBench（2024）、RAGTruth（ACL 2024） | 已试"证据边界 prompt"是**生成前加约束**（faith +6.2 / precision −8.2，默认关）；本机制是**生成后校验+修复**，不动上下文分母，因此不必然牺牲 precision |
| **P0-2** | **工具结果的"证据回执"契约 + 基于状态的停止/补查判据**（每个工具返回 coverage/slot/provenance/empty_reason；由确定性规则判定"是否已足够"，只抑制**无贡献**重复调用） | F1、F4 | macro tool F1 0.731→**0.78–0.82**；无贡献 extra call 7→**≤3**；cross30 recall 75.9%→**79–82%**；faithfulness **≥88.6% 不降**；轮 token −3~8% | τ-bench（2024，pass^k）、Sufficient Context（Google，2024/2025）、ToolBeHonest（EMNLP 2024）、BFCL（Berkeley 榜单）、Gorilla（2023）、Toolformer（2023） | 已试 router/强制首工具/hint/budget 全部**按意图或数量**压，faith 从 88.63 掉到 73.53–83.69；本机制**按"是否有贡献/状态是否补齐"**压，不预测意图 |
| **P0-3** | **评测协议升级：人工锚点 + 配对 CI + pass^k + 冻结 holdout**（60–100 条分层盲评；双族 judge + 人工裁决；报告配对 bootstrap CI；27 题×3 trial 报 pass^3） | F6、F7 | judge–human κ **≥0.6**（先测再定）；CI 半宽 **≤8pp**；后续任何改动必须在"未参与调参"的数据上过门槛；"假提升"被拦截 | MT-Bench（>80% 与人类一致）、ARES（PPI + 少量人工标注给 CI）、G-Eval、AttributionBench（fine-tuned GPT-3.5 仅 ~80 macro-F1）、RAGBench（LLM judge 不如 fine-tuned RoBERTa）、self-preference（2024）、Length-Controlled AlpacaEval | quality gate 已改为"冻结基线 + 2pp 容忍"，但仍以 judge 分为唯一事实；本机制补的是**人工锚点与区间**，不重复"按分数调阈值" |

**P0 完成门（建议预注册）**

1. P0-1：cross30 faithfulness 相对基线 **≥+3pp**，且 context_precision **不下降 >1pp**，人工抽检引用归属正确率 **≥90%**；
2. P0-2：tool F1 **≥+0.04**，且 cross30 recall **≥+2pp**、faithfulness **≥−1pp**；无贡献 over-call 降到 ≤3；
3. P0-3：冻结一套**未参与调参**的 60–100 题（含 20 条人工签名金标）+ 双族 judge 一致性报告 + CI；不达标不进入 P1 参数调优。

### 1.3 P1（1–2 月，按顺序；每次只开一个，独立 holdout 验证）

| 编号 | 机制 | 失败模式 | 预期影响 | 外部证据 | 与已试机制的关系 |
|---|---|---|---|---|---|
| **P1-1** | **结构化查询分解**：把"拆子问题"做成检索工具的显式参数（subqueries[]）+ 返回每个子查询的命中/覆盖回执；只对多问句/多来源题触发 | F1、F5 | cross30 recall **+3–5pp**；precision −0~2pp；token +1 次调用/复杂题 | IRCoT（ACL 2023，HotpotQA 等 4 集）、Least-to-Most、Self-Ask、Query Expansion by Prompting LLMs（MS-MARCO/BEIR）、Azure RAG 指南、RAG-Fusion（B 级）、MultiHop-RAG（B 级） | 已试 query plan **提示**：检索侧 recall@k 0.846→0.897，但答案 precision −2.8、faith +4.0 未过 +4.8 门槛；本机制把分解结果**变成工具参数与回执**，只在检测到"多问句/来源缺口"时触发，不做全局提示 |
| **P1-2** | **时间/元数据硬过滤 + 相对时间归一**（recorded_at/period/source_id/meeting 系列进 filter；"最近七天/下午那场"归一为区间；元数据缺失时降级为软加权） | F5、F1 | 时间/系列题 precision **+3–6pp**，recall 不降；q26 类 3/3 保持 | FreshLLMs/FreshQA（2023）、Qdrant Filtering（C 级）、Azure AI Search filters（C 级）、Azure RAG 指南 | 已试"录音墙钟时间"规则只修了 q26；coverage 节点只给提示；本机制把维度放进**检索参数**并固定归一口径，不靠提示 |
| **P1-3** | **句级上下文裁剪**（保留引用 span 的前提下，删掉"同主题但答不了问题"的句子/窗口） | F3、F2 | context_precision **+2–4pp**；recall −≤1pp；faith 不降；+30–60ms 或 1 次小模型 | Provence（2025）、LLMLingua（EMNLP 2023）、Chain-of-Note（2023）、Lost in the Middle（TACL 2024） | 已试 dedupe 删的是"相邻重复窗口"，实测删掉的 10 条全有用（precision 81.0→78.4）；本机制删的是**问题无关句**且保留 span 身份，并用"引用可定位率"做护栏，不重试 dedupe |
| **P1-4** | **融合函数/RRF 权重校准**（从等权 RRF 改为可校准的凸组合或加权 RRF，用 dev 集学 1 个参数） | F3、F1 | nDCG@k **+1–2pp**；prep/recall 持平；改动小、可回退 | Bruch et al.《An Analysis of Fusion Functions for Hybrid Retrieval》（TOIS 2023，RRF 对参数敏感、CC 常优于 RRF）、Elastic RRF 文档（C 级）、RAG-Fusion（B 级） | 已试"候选深度 24→40"只有 +0.5pp（检不出）；本机制改的是**两路分数如何合成**，不是加候选；必须在冻结 holdout 上验证参数，防单语料过拟合 |
| **P1-5** | **充分性判定与选择性生成**（当"上下文对问题不充分"时，要求模型标注不确定/澄清，而不是硬答） | F2、F4 | faithfulness **+2–4pp**；弃答精度↑；answer rate 可能 −2~5pp（需报权衡） | Sufficient Context（selective generation 使"答对率 among answered" +2–10%）、Adaptive-RAG、FLARE、DRAGIN、Self-RAG、Chain-of-Note | 该机制与"证据边界 prompt"不同：判定依据是**检索结果的结构化充分性**而不是提示词；与 P0-2 共用回执字段 |
| **P1-6** | **引用感知生成（最后一遍 schema 化）**：主体自由生成，只在"引用格式化"最后一步要求 `{claim, evidence_ids[]}`；claim 与 span 一一对应 | F2 | 引用绑定错误 −30~50%；不改主体推理；需 P0-1 的 span 索引配合 | ALCE（EMNLP 2023：citation recall/precision 自动指标）、SelfCite（sentence-level citation）、Let Me Speak Freely（格式限制会伤推理，故只用于最后一遍） | 与 P0-1 互补：P0-1 是"事后修"，P1-6 是"生成时减少错绑"；相比全局 prompt 约束，它减少自由文本元陈述 |

### 1.4 P2（条件触发，不要现在做）

| 机制 | 现在不做的理由 | 触发条件 |
|---|---|---|
| GraphRAG / 实体图（全局/系列汇总） | 27 题里 0 次需要多跳（历史诊断）；GraphRAG 建图成本高，且本项目系列题只有 30 题规模 | 系列/跨会议题 ≥50 条且人工确认"关系型"缺口；先用离线脚本验证实体抽取质量 |
| Late chunking / 选择性 contextual embeddings | 已试 contextual retrieval：precision 77.3 vs 81.0（分子相同、分母 +4），未过主指标；语义切分论文结论是"计算成本未证明值得" | 只在"孤儿切片/跨录音块"上做稀疏实验，并用检索侧失败率（而非 precision 单指标）做门禁 |
| 本地 claim/citation 小模型（RAGTruth 式微调） | 需要标注数据与训练基础设施；当前 18k 规模的外部数据可微调，但本项目的引用体系（编号+卡片）需自定义 | P0-1 规则+小模型流水线稳定后，用其标注积累 2–3k 条再考虑 |
| ColBERT/multi-vector | 需要新索引与 GPU 侧能力；现混合+rerank 已覆盖两类召回 | 检索 recall 在真实会议集上 <85% 且人工判定为"语义召回不足" |
| Self-RAG 训练（reflection tokens） | 需要训练与推理改造；小语料收益不确定 | 有 ≥5 场真实会议 + 人工金标后 |

### 1.5 预期收益的诚实边界

- 上述数字是**假设区间**，不是论文数字的外推。论文的收益都在其数据集上成立（如 IRCoT 在 HotpotQA 类多跳集上 +15 QA，ALCE 在 ELI5 上"最好模型仍 50% 引用不完整"），与本项目的中文会议口语、短切片、系列题语料**不同分布**；
- 建议用**配对设计**（同题同配置只差一个开关）而不是跨轮次比较；所有比例带 n 与 CI；
- P0 三项的总成本估计：claim 校验 ≤1 次小模型/轮（约 1–3k token）、回执为纯代码、评测人工 60–100 条（2–4 人日）。相对"再调一轮 prompt"更贵，但**可复用、可回退、可证伪**。

---

## 2. 机制目录总表

> 读法：每行是一个"机制级"候选；"证据"列给外部 URL（详见 §9）；"通用性"列回答"为什么不是只对本项目语料有效"；"预期影响"为本项目口径假设；"关系"列对照已试机制（§7）。

### 2.1 检索召回（F1/F5）

| 机制 | 解决哪个失败模式 | 外部证据（URL） | 通用性论证 | 预期指标影响 | 代价/风险 | 优先级 | 与已试机制的关系 |
|---|---|---|---|---|---|---|---|
| 结构化查询分解（subqueries 作为工具参数 + 逐子查询回执） | F1 多问句/多来源漏检 | IRCoT https://arxiv.org/abs/2212.10509 ；Least-to-Most https://arxiv.org/abs/2205.10625 ；Self-Ask https://arxiv.org/abs/2210.03350 ；Query Expansion https://arxiv.org/abs/2305.03653 ；Azure 指南 https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-information-retrieval | 分解是多跳/多问句的通用前置；IRCoT 在 4 个多跳数据集（HotpotQA/2Wiki/MuSiQue/IIRC）上同时提升检索与 QA，且"检索什么取决于已推导出什么"是通用机制 | recall +3–5pp；precision −0~2pp；+1 调用 | 分解错误引入噪声；需门控；不能全局常开 | **P1** | 已试 query plan **提示**（检索↑/答案 precision −2.8）；本机制进工具参数与回执，不做全局提示 |
| 元数据/时间硬过滤 + 相对时间归一 | F5 时间限定无维度 | FreshLLMs https://arxiv.org/abs/2310.03214 ；Qdrant filtering https://qdrant.tech/documentation/search/filtering/ ；Azure filters https://learn.microsoft.com/en-us/azure/search/search-filters ；Azure RAG 指南（同上） | 结构化过滤是向量检索的通用能力（官方文档：把"能表达为条件的"放 filter）；时间敏感问答是通用问题类型 | 时间题 precision +3–6pp；recall 不降（需元数据覆盖率高） | 元数据缺失/错误→漏检；只能对"显式时间限定"硬过滤 | **P1** | 已试录音墙钟时间规则只修 q26；coverage 节点只是提示 |
| 自适应检索（按复杂度决定检索次数/停止） | F1、F4 | Adaptive-RAG https://arxiv.org/abs/2403.14403 ；FLARE https://arxiv.org/abs/2305.06983 ；DRAGIN https://arxiv.org/abs/2403.10081 ；Self-RAG https://arxiv.org/abs/2310.11511 | "并非所有问题都该走同一检索深度"是通用结论；Adaptive-RAG 用分类器在无检索/单步/多步间选择 | 复杂题 recall↑、简单题 token↓；faith 可能↑ | 分类器误判会放大错误；需 fallback | **P1/P2** | 意图路由（已试）伤 faith；本机制只决定"检索深度"不决定"工具"，且用回执状态而非意图标签 |
| 融合函数/权重校准（RRF→加权 RRF/CC） | F3 排序精度 | Bruch 2023 https://arxiv.org/abs/2210.11934 ；Elastic RRF https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html ；RAG-Fusion https://arxiv.org/abs/2402.03367 | 混合检索的融合是通用排序问题；论文指出 RRF 对参数敏感、凸组合在域内/域外均优于 RRF，且只需少量样本校准 | nDCG +1–2pp | 单语料过拟合；需冻结 holdout | **P1** | 当前应用层等权 RRF；未试过校准 |
| 多查询扩展（LLM 生成扩展查询） | F1 词面不匹配 | Query Expansion https://arxiv.org/abs/2305.03653 ；RAG-Fusion https://arxiv.org/abs/2402.03367 | 查询扩展是 IR 经典通用手段；LLM 生成扩展在 MS-MARCO/BEIR 上优于传统 PRF | recall +2–4pp；precision 可能 − | 额外调用；扩展跑偏（RAG-Fusion 论文自己也报告"答案跑题"） | P2 | 与 P1-1 二选一先试；不重复"multi-query 全量常开" |

### 2.2 上下文精度（F3）

| 机制 | 解决哪个失败模式 | 外部证据（URL） | 通用性论证 | 预期指标影响 | 代价/风险 | 优先级 | 与已试机制的关系 |
|---|---|---|---|---|---|---|---|
| 句级上下文裁剪/压缩（保留 span） | F3 同主题无关窗口稀释 | Provence https://arxiv.org/abs/2501.16214 ；LLMLingua https://arxiv.org/abs/2310.05736 ；Chain-of-Note https://arxiv.org/abs/2311.09210 | 长上下文/无关上下文会普遍降低生成质量（Lost in the Middle、Context Rot 均为跨模型结论）；裁剪是通用的上下文工程 | precision +2–4pp；recall −≤1pp | 裁剪误删证据→recall 掉；破坏引用 span | **P1** | 已试 dedupe（删重复窗口）失败；本机制删无关句，需 span 映射 |
| rerank 深化/类别化（按来源类型分别 rerank 或不同 top-k） | F3 | RankRAG https://arxiv.org/abs/2407.02485 ；RankGPT https://arxiv.org/abs/2304.09542 ；Anthropic 复排实验（失败率 5.7%→1.9%）https://www.anthropic.com/research/contextual-retrieval | rerank 是通用第二阶段排序；LLM/listwise rerank 在零样本下可超过监督模型（RankGPT），但成本高 | precision +1–3pp | 延迟/成本；已有 24 候选实测只 +0.5pp（k=40）→ 不靠加候选 | P2 | rerank 已默认开、候选深度已标定；不要再加候选数 |
| MMR/多样性选择 | F3 重复/冗余 | MMR 原论文 https://www.cs.cmu.edu/~jgc/publication/The_Use_MMR_Diversity_Based_LTMIR_1998.pdf | MMR 是通用"相关性−冗余"权衡；对"多个同主题窗口"的会议语料尤其相关 | precision +1–2pp；recall 持平 | 多样性可能挤掉关键第二证据；需 λ 调参 | P2 | 未试；但项目已有"相邻窗口各含答案"的反例（dedupe 失败），MMR 需按"是否含新事实"而非相似度去冗余 |
| 上下文顺序/位置优化 | F2 中间遗忘 | Lost in the Middle https://aclanthology.org/2024.tacl-1.9/ ；Context Rot https://www.trychroma.com/research/context-rot | 位置效应与长度退化是跨 18 个模型的通用现象 | 引用已置于末尾的答案格式已部分缓解；faith 可能 +1–2pp | 改动小；收益不确定 | P2 | 当前答案格式已把引用放行内；可先量"证据放在开头 vs 中间"的对照 |
| chunk 粒度/边界（语义切分、晚切分） | F3 | 语义切分成本研究 https://aclanthology.org/2025.findings-naacl.114/ ；Late Chunking https://arxiv.org/abs/2409.04701 ；Anthropic chunk 边界建议（同上） | 切分是通用参数；但语义切分"计算成本是否值得"在论文中未获肯定结论 | 可能 +1–2pp precision | 索引重建/重嵌成本；当前 300×3 已标定 | P2 | 已做 500×5→300×3 标定；不要重复调 chunk |

### 2.3 忠实度与引用归属（F2）

| 机制 | 解决哪个失败模式 | 外部证据（URL） | 通用性论证 | 预期指标影响 | 代价/风险 | 优先级 | 与已试机制的关系 |
|---|---|---|---|---|---|---|---|
| Claim-level 事后校验 + 重归属（CoVe/RARR 式） | F2 引用绑定/推断越界 | CoVe https://arxiv.org/abs/2309.11495 ；RARR https://arxiv.org/abs/2210.08726 ；SelfCite https://arxiv.org/abs/2502.09604 ；CiteFix https://arxiv.org/abs/2504.15629 ；ALCE https://arxiv.org/abs/2305.14627 ；AttributionBench https://arxiv.org/abs/2402.15089 ；RAGTruth https://arxiv.org/abs/2401.00396 | 生成模型的引用归属是**通用未解问题**：ALCE 上最好模型 50% 引用不完整；AttributionBench 上 fine-tuned GPT-3.5 仅 ~80 macro-F1；CiteFix 引行业报告"生成搜索引用准确率约 74%" | faith +3–6pp；引用类 partial ↓；precision 不降 | 修复误伤内容；judge 成本；需保留原句做灰度 | **P0** | 已试规则只"标注不拦截"（`rag_quality`）、证据边界 prompt（faith+6.2/prec−8.2）；本机制是**生成后处理**，不占上下文预算 |
| 引用感知生成（生成时带 evidence span id） | F2 | ALCE（同上）；SelfCite（同上）；格式限制伤推理 https://arxiv.org/abs/2408.02442 | 让模型在生成时绑定证据是通用范式；但**格式约束会伤推理**，故只对最后一遍引用格式化 | 绑定错误 −30~50% | 格式 rigidity；需 claim 拆分器 | P1 | 与 P0-1 互补 |
| 幻觉检测模型（HHEM/RAGTruth 式小模型） | F2 判分/护栏 | HHEM https://huggingface.co/vectara/hallucination_evaluation_model ；RAGTruth（同上） | RAGTruth 显示微调小模型可接近 GPT-4 prompt 的检测水平；HHEM 是公开可用的通用幻觉分类器 | 检测 F1 提升、成本下降 | 领域漂移；中文会议语料需校准 | P2 | 可作为 P0-1 的裁决器候选，但先用人工 100 条校准 |
| 忠实度 judge 的 claim 级拆解（FActScore/SAFE/RAGChecker） | F6 评测口径 | FActScore https://arxiv.org/abs/2305.14251 ；SAFE https://arxiv.org/abs/2403.18802 ；RAGChecker https://arxiv.org/abs/2408.08067 | "把长答案拆成原子事实再核验"是通用评测范式；RAGChecker 声称与人类判断相关性显著更好 | 评测区分度↑、能定位到 claim | 成本高；拆解器本身有误差 | **P0（评测侧）** | 当前 faithfulness 是 judge 内容级；claim 级更细但更贵 |

### 2.4 工具调用效率（F4）

| 机制 | 解决哪个失败模式 | 外部证据（URL） | 通用性论证 | 预期指标影响 | 代价/风险 | 优先级 | 与已试机制的关系 |
|---|---|---|---|---|---|---|---|
| 工具结果结构化"证据回执"（coverage/slot/provenance/empty_reason） | F4 无停止判据 | τ-bench https://arxiv.org/abs/2406.12045 ；Sufficient Context https://arxiv.org/abs/2411.06037 ；ToolBeHonest https://arxiv.org/abs/2406.20015 ；BFCL https://gorilla.cs.berkeley.edu/leaderboard.html | τ-bench 用数据库终态而非对话文本判分，说明"状态"才是可验证的对象；Sufficient Context 说明模型普遍不会在证据不足时弃答；ToolBeHonest 把"任务可解性判断"列为最大错误源 | F1 +0.04~0.09；无贡献 over-call ↓；recall +2–3pp | 状态计算错误→错误停止；schema 兼容 | **P0** | 已试 coverage 节点是**提示**；router/force first 是**意图层**；本机制是**数据契约层** |
| 基于贡献的 over-call 抑制（只压无 citation 贡献的重复调用） | F4 | τ-bench（pass^k 可靠性）；BFCL（irrelevance 类别）https://gorilla.cs.berkeley.edu/leaderboard.html | "多调一次是否有信息增益"是通用 agent 效率问题；τ-bench 显示一致性比单次成功率低得多 | F1 改善且 faith 不降 | 误判"无贡献"会漏证据 | **P0（并入 P0-2）** | 已试"硬压"4 方案 faith 全降；本机制按贡献，不按数量 |
| 工具选择/参数鲁棒性（宽解析、schema 校验、错误可行动） | F4 | Gorilla https://arxiv.org/abs/2305.15334 ；Toolformer https://arxiv.org/abs/2302.04761 ；ReAct https://arxiv.org/abs/2210.03629 | API/参数错误是工具调用通用失败模式（Gorilla 明确"参数错误与 API 误用"）；ReAct 把"行动-观察"作为通用循环 | 轮级失败↓；参数错误重试↓ | 宽解析掩盖真错误 | P1（部分已修） | 项目已修字符串化参数（历史 q26/q27）；保持 |

### 2.5 评测方法（F6/F7）

| 机制 | 解决哪个失败模式 | 外部证据（URL） | 通用性论证 | 预期指标影响 | 代价/风险 | 优先级 | 与已试机制的关系 |
|---|---|---|---|---|---|---|---|
| 人工锚点 + 双族 judge + 校准 | F6/F7 | MT-Bench >80% 人类一致 https://arxiv.org/abs/2306.05685 ；ARES（PPI+少量人工标注）https://arxiv.org/abs/2311.09476 ；G-Eval https://arxiv.org/abs/2303.16634 ；Prometheus 2 https://arxiv.org/abs/2405.01535 | LLM judge 可以接近人类但**有系统偏差**（位置/长度/自我偏好），需要人工锚点与去偏；PPI 是通用统计工具 | 决策误差↓；κ 可报告 | 人工 60–100 条；成本 | **P0** | 现有 judge 单族、无人工锚点 |
| 配对 CI/bootstrap + pass^k | F6 | τ-bench（pass^k 定义）https://arxiv.org/abs/2406.12045 ；ARES PPI（CI）（同上） | 小样本比例的 CI 与多 trial 可靠性是通用统计要求 | 报告从点估计升级为区间/可靠性 | 需要重跑多次（成本） | **P0** | 当前报告多为点估计 |
| Claim 级 RAG 指标（RAGChecker/TRACe） | F6 | RAGChecker（同上）；RAGBench/TRACe https://arxiv.org/abs/2407.11005 | RAGBench 100k 例 5 领域，TRACe 给出可解释指标；LLM judge 在该任务上不如 fine-tuned RoBERTa | 区分度↑；可定位 | 实现成本 | P1（评测侧） | 当前用 RAGAS 四指标 |
| 防污染/防按题过拟合 | F7 | 数据污染调查 https://arxiv.org/abs/2311.09783 ；RAG 最佳实践 https://arxiv.org/abs/2407.01219 | 测试集污染与调参泄漏是通用问题；论文给出"检索式重叠检测/TS-Guessing"方法 | 结论可信度↑ | 需隔离 holdout | **P0（协议）** | quality gate 已相对化，但仍需真 holdout |

> 表中 URL 均已核验可访问；完整题录见 §9。

---

## 3. P0 机制详解

### 3.1 P0-1 Claim→span 引用校验与重归属流水线（post-hoc verification & re-citation）

**解决：F2（引用归属错误、推断越界），兼修 F3 的"引用噪声"。**

#### 原理

外部证据表明"引用归属"是一个**独立的、尚未解决的问题**，不是本项目的语料问题：

- **ALCE**（EMNLP 2023）把"引用质量"拆成 citation recall / citation precision 自动指标，并报告在 ELI5 上**最好的模型仍有 50% 的答案缺少完整引用支撑**；
- **RARR**（ACL 2023）证明"事后找证据 + 修订输出"可以在保留原答案绝大部分内容的同时显著提升归因；
- **CoVe**（ACL Findings 2024）用"先起草 → 设计验证问题 → 独立回答 → 重写"的流程降低幻觉，关键点是**验证问题必须独立回答，避免被原答案带偏**；
- **SelfCite**（2025，LongBench-Cite）用"上下文消融"作自监督奖励（删掉被引片段答案应不成立、只留被引片段答案应仍成立），说明"引用是否必要/充分"是可操作定义；
- **CiteFix**（ACL 2025 Industry）在生产 RAG 中做**后处理引用校正**，报告整体准确率相对提升 15.46%，并引行业数据"生成式搜索引用准确率约 74%"；
- **AttributionBench**（2024）表明即使是 fine-tuned GPT-3.5，二分类归因也只有 ~80 macro-F1——**归因判定器本身不完美，必须与规则/人工配合**；
- **RAGTruth**（ACL 2024）~18k 条人工词级标注显示，微调小模型可接近大模型 prompt 的幻觉检测水平——这是本项目可以低成本引入裁决器的依据。

对本项目的直接含义：现在只有"规则标注"（`rag_quality`：数字/日期/引语不在被引上下文 → warning，不拦截），以及一次失败过的"证据边界 prompt"。前者**发现了问题但不修**，后者**在生成前加约束**（cross30 faith 78.6→84.8、precision 74.6→66.4）。P0-1 走第三条路：**在生成后修引用，不碰上下文集合**。

#### 最小可行实现（代码级思路，不写代码）

1. **构建轮内证据表（已有基础设施可复用）**
   - 每个工具结果归一化为 `evidence_items[]`：`{eid, source_kind(chunk/fact/segment/minutes/state), source_id, meeting_id, char_start, char_end, text, recorded_at, authority}`；
   - 统一做文本归一化：全角/半角、中文数字（"十七点"↔"17:00"）、日期格式（"9月10日"↔"2026-09-10"）、时长/区间（"58-77 秒"）、专名（人名/系统名）；
   - 这个表**只在本轮内有效**，不跨轮拼历史（避免历史引用口径混淆——LEDGER 已记录多轮 claim audit 22.9% → fresh 9.4% 的假阴性）。

2. **确定性 claim 拆分（规则，零调用）**
   - 按句号/分号/换行切句，再去掉纯元陈述（"以上信息来自纪要""该条为人工确认"）——这类约占历史答案的 20–30%，是 faithfulness 噪声源（`问答链路分析` 2.5）；
   - 句子分类（规则/词表即可）：`number/date`、`entity/role`、`quote`、`causal/inference`、`cross_source_aggregation`；
   - 每条 claim 记录它当前引用的 `[n]` 列表与对应 `eid`。

3. **第一层校验：高精度规则（零模型调用）**
   - 数字/日期/引语：归一化后必须在**被引 span 的原文**中出现；不在被引 span、但在本轮其他 span 中出现 → 动作 `rebind`（把引用从错误 eid 改到正确 eid）；
   - 任何 span 都找不到 → 动作 `downgrade`（把"确定"改为"材料未明确/据上下文推断"，或删除该细节）；
   - **只改引用与措辞，不改事实内容**；每个动作记录 before/after，落库可审计；
   - 这正是现有规则审计（18.3% 不在被引上下文 / 9.4% 不在任何上下文）的"可执行化"。

4. **第二层校验：小模型裁决（仅规则不确定的 claim）**
   - 不确定集合 = 规则无法判定（推断类、跨来源聚合、数字为派生值/换算值）；目标 ≤20% claims；
   - 裁决器输出**受约束枚举**：`supported_by_cited / supported_by_other / citation_mapping / derived / unsupported`——与 LEDGER 25 号记录的深判分类完全一致；
   - 判定提示必须要求"引用原文 vs claim"逐字对照，并禁止引入外部知识；
   - 裁决器选择：先用当前 judge 族之外的模型（降低同族偏置），或先做 100 条人工校准后使用 HHEM/RAGTruth 式本地小模型。

5. **修复动作与护栏**
   - 动作空间只有三种：`rebind`（改引用编号）、`hedge`（降级措辞，如"约/据…推断"）、`drop`（删除无依据子句）；**禁止新增事实**；
   - 若一条答案被判定需要修复的 claim 比例 >30%，则**不自动改写**，退回"只标注 + 原答案"，并把 `evidence_warnings` 暴露给前端（现在的 warning 机制）；
   - 用户可见文案用"证据标注"而非"纠错"，避免误导（引用重绑不保证答案正确，只保证"引用指向的片段确实支撑该句"）。

#### 验证设计

| 项 | 设计 |
|---|---|
| A/B | 同一批已有答案（40 条盲评集 + fresh cross30）做**配对**离线重跑：A=现状（只标注），B=标注+修复。同一模型、同一天、同 prompt 其余不变 |
| 主指标 | claim 级引用绑定正确率（人工 40 条逐 claim 判）；faithfulness（judge）；"数字不在被引上下文"比例 |
| 次指标 | context_precision（必须**不降**，因为上下文集合未变）、answer_relevancy、答案改动率、延迟、token、`unknown/missing` 引用数（必须保持 0） |
| 护栏（否决条件） | ① 负样 12/12 拒答、0 编造必须保持；② 引用可定位率 100%（55/55）必须保持；③ 盲评 correct 数不得下降超过 1 条；④ 答案改动率 >30% 视为不可接受（说明规则误伤） |
| 样本量 | 人工逐 claim 判 40 条（约 400–800 claims）→ 绑定正确率的 95% CI 半宽约 ±3–4pp（以 claim 为单位）；答案级 faithfulness 需 100+ 题才有 ±8pp 内的 CI，因此**先看方向 + 护栏，再扩到 100** |
| 判分器 | 至少两族 judge；与人工判断报一致率；不一致由人工裁决，不取平均 |

#### 与已试机制的关系（为什么这次不同）

- **不是**"证据边界 prompt"：不往 system prompt 加长文，不在生成前改变模型行为，因此**不改变上下文预算与答案先验**；
- **不是**"只标注不拦截"：标注已经证明信息是有的（18.3% 规则命中、深判 9.6% 真 unsupported），缺的是修复动作；
- **不是**"全量拦截"：LEDGER 25 号明确"支持做条件化 claim 校验，不做全量拦截"——本机制的 30% 阈值与"只修引用/措辞"就是对这句结论的工程化；
- 与 **RARR/CoVe** 的差异：本项目的引用是**结构化编号+卡片**，证据表是现成的，因此不需要 RARR 的 web 检索；与 **CiteFix** 的差异：CiteFix 用关键词+语义匹配，本项目可先用规则（数字/日期/引语）拿到高精度部分，再对小集合用小模型，成本更低。

---

### 3.2 P0-2 工具结果"证据回执"契约 + 基于状态的停止/补查判据

**解决：F4（无停止判据、over-call 混合），并直接改善 F1（跨来源覆盖）。**

#### 原理

- **τ-bench**（2024/2025）评测的是"用户与 Agent 多轮交互后**数据库终态**是否正确"，并提出 `pass^k` 衡量多次试验的一致性：即使 gpt-4o，任务成功率 <50%，零售域 `pass^8` <25%。这说明"能不能按规则把状态改对"是**通用 agent 能力瓶颈**，而不是本项目特有；
- **Sufficient Context**（Google，ICLR 2025）证明：大模型在上下文**充分**时很擅长回答，但在上下文**不充分**时通常不会弃答而是硬答；用"充分性"做选择性生成，可使"回答中的正确率"提升 2–10%；
- **ToolBeHonest**（EMNLP 2024）把工具增强 LLM 的幻觉拆成"可解性检测 / 方案规划 / 缺工具分析"，发现**最主要的错误来源是"评估任务是否可解"**（GPT-4o 总分仅 37/100）；
- **BFCL**（Berkeley Function Calling Leaderboard）用 AST 比对而非字符串匹配评测函数调用，并包含"irrelevance detection"（不该调用时是否忍住）——与 over-call 直接相关；
- **Gorilla**（2023）指出工具调用失败的主因是"参数错 + API 误用"，**Toolformer**（2023）则说明"何时调用、调什么、怎么用结果"需要被显式建模。

对本项目：现有 `coverage` 节点只做"检测来源缺口 → 注入一次定向提示"（提示层）；`tool_utility.py` 已经能审计"extra call 是否贡献 citation"。P0-2 把这套离线分析**搬进运行时数据契约**：让"是否已覆盖/是否还缺槽位/这次调用有没有新证据"成为工具返回值的一部分，停止由**确定性规则**判定，而不是让模型猜或让意图路由猜。

#### 最小可行实现（代码级思路，不写代码）

1. **统一工具回执 schema（所有 5 个工具）**

   ```text
   {
     ok: bool,
     items: [{eid, source_kind, meeting_id, source_id, char_start, char_end,
              text, recorded_at, authority, score}],
     coverage: {
       meetings_seen: [...], recordings_seen: [...],
       segments_seen: [{recording_id, range}],
       slots: {time_point: bool, person: bool, quantity: bool,
               cross_source: bool, series_scope: bool},
       missing: ["source:meeting2/recordingB", "slot:quantity"]
     },
     provenance: {tool, params_echo, retrieval_round},
     empty_reason: "no_confirmed_minutes|draft_only|filter_mismatch|no_hit|null",
     next_actions_hint: ["search_transcript_scope:recordingB"]
   }
   ```

   - `empty_reason` 尤其关键：历史上 `list_facts` 因"下午纪要还是 DRAFT"返回空、模型不知道可以退回转写检索，导致两轮失败（`问答链路分析` 2.2 现象二）。**空结果的原因必须可行动**；
   - `next_actions_hint` 是**建议**不是命令，模型可不采纳；但 UI/日志可观测。

2. **确定性状态机（在 graph 的工具节点之后，不调模型）**
   - 维护 `turn_evidence_state`：已覆盖来源集合、已填槽位、每次调用的 `new_evidence_count`；
   - **重复调用抑制**：同一 `(tool, normalized_params)` 第二次调用且第一次 `new_evidence_count=0` → 不再真正执行，直接返回 `empty_reason="duplicate_no_gain"` 并附 `stop_hint`；不同参数/不同工具**不拦**（避免误伤"探索性但有贡献"的调用）；
   - **补查判据**：若"问题显式要求跨来源/系列"（规则检测：出现"分别/两个/几段/整个系列"等 + 题目声明的 scope）且 `coverage.missing` 非空 → 允许一次定向补查（沿用现有条件补搜机制，但要按回执字段判断，而不是按"强标记 + 转写覆盖<2 段"的硬编码）；
   - **停止判据**：`coverage.missing` 为空 且 最近一次调用 `new_evidence_count=0` → 注入一次"证据已覆盖，可作答"的短状态（≤1 行），**不禁止**再调不同工具。

3. **把 `new_evidence_count` / `citation_kind` 落库**
   - 每轮记录：每次调用的贡献（新增 eid 数、被最终引用数）→ 直接复用 `tool_utility.py` 的口径；
   - 这让"over-call utility"从离线脚本变成**在线可报表**：无贡献 extra call 数、覆盖率、重复调用数。

#### 验证设计

| 项 | 设计 |
|---|---|
| A/B | 27 条轨迹题（配对，同 prompt/同模型）+ fresh cross30（答案指标）+ mixed100 中 cross30 分层 |
| 主指标 | macro tool F1、required coverage、**无贡献 extra call 数**（目标 7→≤3）、cross30 recall |
| 次指标 | cross30 precision/faithfulness、轮 token、延迟、`stop_reason=stalled` 数（必须为 0） |
| 护栏 | ① cross30 faithfulness ≥88.6%（不得用 faith 换 F1，这是已试 router 的失败教训）；② recall 不得低于 baseline−1pp；③ 不得出现"该调却没调"（只漏题数 0→>0 即否决）；④ 首轮工具分布不能异常偏移（可接受变化） |
| 样本量 | 27 条轨迹对 F1 的 CI 很宽（±10pp 量级），结论必须用"配对方向 + 100 题答案指标"支撑；如需宣称 F1 +0.05，需扩到 ≥60 条轨迹 |
| 上线策略 | 先 **shadow 模式**：只记录 `coverage/new_evidence_count` 与建议，不改变行为跑 1–2 天；用真实轮次校准"状态计算是否准"，再开启抑制 |

#### 与已试机制的关系（为什么这次不同）

- **不是** router/selective/hint/budget/force-first：这五种都从"意图/数量"出发，实测 F1 提升但 cross30 faith 从 88.63 掉到 73.53–83.69；
- **按贡献而非数量**：`tool_utility` 已经证明 18 次 extra call 里 11 次有引用贡献——本机制只拦"同参重复且零新增证据"的调用，理论上不会碰有贡献的探索；
- **不是** coverage 提示词：现有 coverage 节点只是"注入一次定向提示"；本机制把 coverage 变成**工具返回值 + 确定性状态**，模型看到的是数据而不是指令；
- 与 **Self-RAG/Sufficient Context** 的差异：不训练模型、不改推理；充分性由检索侧状态近似，先拿工程收益。

---

### 3.3 P0-3 评测协议升级：人工锚点 + 配对 CI + pass^k + 冻结 holdout

**解决：F6（评测不独立、无区间）、F7（同族判分、按题过拟合）。**

#### 原理

- **MT-Bench / LLM-as-a-judge**（NeurIPS 2023 Datasets & Benchmarks）证明强 judge 与人类偏好可 >80% 一致，但**同时点名**位置、长度、自我增强等偏差；
- **G-Eval**（EMNLP 2023）用 CoT + 表单式打分提升与人类相关性（summarization 上 Spearman 0.514），但也提示 judge 对 LLM 生成文本有偏好；
- **ARES**（NAACL 2024）用**少量人工标注 + prediction-powered inference（PPI）**给 RAG 三指标提供统计校正——这是"怎么用 200 条人工标注换 CI"的通用工具；
- **AttributionBench**（2024）：fine-tuned GPT-3.5 在归因二分类只有 ~80 macro-F1；**RAGBench**（2024）进一步发现 LLM-based RAG 评测方法**打不过 fine-tuned RoBERTa**——两者共同说明"不能把通用大模型 judge 当金标"；
- **self-preference**（2024）：LLM 评分者对自己生成的文本有可测量的偏好，且自我识别能力与偏好强度线性相关——**判分器与生成器同族是结构性偏差**；
- **Length-Controlled AlpacaEval**（2024）：用回归控制长度混杂后与 Chatbot Arena 的 Spearman 从 0.94 升到 0.98——**偏差可以被建模消除**；
- **τ-bench**：`pass^k` 定义（同一任务 k 次独立尝试全部成功的比例）衡量可靠性，而不是单次成功率；
- **数据污染调查**（2023）：提出检索式重叠与 TS-Guessing 检测测试集污染，说明"刷分"有可检测手段。

本项目的具体风险已在内部审计中列出：1032 原始 case / 259 唯一题面 / 0 人工金标；holdout A/B/D 参与过调参；new meeting 是合成语料；mixed100 是组装集；quality gate 曾贴当前分数设阈值（已改为冻结基线 + 2pp，但仍以 judge 分为唯一事实）。

#### 最小可行实现（协议，不是代码）

1. **冻结评测集（一次性，加 hash）**
   - 分层：`real+human`（60–100 题：3–5 场真实会议；每题人工签名金标 + 答案人工裁决）→ 最终对外只认这一层；`hard`（时间/系列/多跳/不可回答，现有 cross30 改造）；`realistic`（unscoped 多会议 + 干扰会议）；`smoke`（现有 6×3，只验链路）；
   - **holdout 只跑一次**：文件 hash 冻结、评测脚本只读、任何人不得据其调参；调参用独立 dev 集（现有 27/68/100 中划出）；
   - 污染检查：用题目/答案文本对 prompt、docs、代码做 n-gram 重叠检索（TS-Guessing 思路的简化版）；同源审计（题目作者 vs 事实作者分离）。

2. **判分协议**
   - **双族 judge + 人工锚点**：至少两个不同模型族做 judge；报告每指标的两族一致率；**不一致时用人工裁决**，不取平均、不取 max；
   - 每个 judge 只做其被校准过的任务（faithfulness 用 claim 拆解；context precision 用 rank-aware 口径；answer relevancy 单独）；
   - **judge 提示固定、版本化**，任何提示改动都算新判分器，历史读数不可混算；
   - 抽样 40–60 条做人工标注，计算 judge–human 的 Cohen's κ 与逐指标偏差（κ ≥0.6 才可作决策依据；<0.6 只作方向）；
   - 结论表述规则：**judge 分只能报"相对变化 + CI"**，不能报"绝对质量"。

3. **统计**
   - 比例为点估计 + Wilson 95% CI；组间比较用**配对 bootstrap**（按题重采样 10k 次，固定随机种子）或 McNemar；
   - 样本量直觉（独立二项，p≈0.8）：n=30 的 95% 半宽 ≈14pp、n=100 ≈8pp、n=400 ≈4pp；配对设计会缩小但**不会消失**；要稳定检出 5pp 绝对提升，建议 ≥150–200 配对题（或先只报方向）；
   - 多重比较：一次实验只预注册 1 个主指标 + ≤3 个次指标；否则报告 Bonferroni/Holm 校正后的区间。

4. **可靠性（pass^k）**
   - 12–27 条轨迹题 ×3 次独立运行 → `pass^3`（全部成功的比例）；与单次成功率一起报；
   - τ-bench 的教训：单次成功率会高估可用性（gpt-4o 单次 <50%，`pass^8` <25%）；对本项目而言，`pass^3` 比单次 tool F1 更接近"用户连续三次都满意"的体验。

5. **"避免判分器与生成器同族 / 按题过拟合"的可执行规则**
   - judge 模型族 ≠ 被评生成模型族；若无法避免（成本），用**规则/检索侧零调用指标**（recall@k、MRR、nDCG、引用可定位率、数字命中率）作为"不受 judge 漂移影响"的锚；
   - 任何新机制**默认关闭**，必须在新 holdout 上过门槛才默认开（沿用 `generalization_audit_v2` 的原则）；
   - 报告模板强制字段：n、CI、所用 judge 族、是否人工锚定、数据是否参与过调参、**失败样例清单**（至少 5 条）。

#### 验证设计（对"评测本身"的验证）

| 项 | 设计 |
|---|---|
| judge 一致性 | 40–60 条同一答案双族 judge 评分 + 人工裁决；报 κ、一致率、偏差方向 |
| CI 校准 | 用 bootstrap 重采样估计"同一配置重复评测"的方差；与理论 Wilson CI 对照 |
| 过拟合检测 | 在"未参与调参"的新 holdout 上复测历史 best 配置；记录 dev→test 的 delta 分布（如 rerank floor 的 +4.29→−3.28 反例） |
| 成本 | 人工 60–100 条 × 2 人日量级；双族 judge 的 token 成本可接受（离线评测） |

#### 与已试机制的关系

- quality gate 已改为"冻结基线 + 2pp 容忍 + 绝对下限"，这是对的方向；P0-3 补的是**人工锚点、区间、可靠性**三件它没有的东西；
- 不重复"按分数调阈值"：阈值必须来自预注册与 holdout，不来自当前读数；
- 不把"AI 复核"写成 human：沿用 `intent_source` 的三值口径（human/ai_review/agent_draft）。

---

## 4. P1 机制详解

### 4.1 P1-1 结构化查询分解（subqueries 作为工具参数 + 逐子查询回执）

**解决：F1（多问句/多来源漏检）、F5（限定条件无处安放）。**

**原理。** IRCoT（ACL 2023）的关键不是"分解"本身，而是**检索与推理交替**："检索什么取决于已经推导出什么"；它在 HotpotQA/2WikiMultihopQA/MuSiQue/IIRC 四个多跳集上把检索提升最多 21 点、QA 最多 15 点，并在 OOD 与小模型上同样有效。Least-to-Most 与 Self-Ask 说明"先拆子问题再逐个解决"能缩小组合性差距；Query Expansion by Prompting LLMs 进一步表明 **CoT 式提示生成的扩展查询**在 MS-MARCO/BEIR 上强于传统 PRF；Azure RAG 指南则给了工程原则：**先判断是否需要多次检索，再决定检索几次；子问题必须自包含；多路结果融合后 rerank 再截断**。

**与已试 query plan 的差别。** 已试版本把"意图+改写+子问题+建议工具"一次性生成并**注入提示**，检索侧变好（recall@k 0.846→0.897、MRR 0.627→0.673、nDCG 0.668→0.709），但答案 A/B 里 precision −2.8pp、faith +4.0pp 未过 +4.8pp 门槛。分析原因：①提示让模型"先规划"的行为挤占了答案预算；②预取/分解结果作为**上下文**进入生成，扩大了上下文分母；③没有"哪条子查询没覆盖"的回执，模型无法只用分解做补查。P1-1 的改法：**分解结果作为工具参数，结果带子查询覆盖表；不把规划文本塞进 system prompt**。

**最小可行实现。**
- `search_knowledge` 增加可选参数 `subqueries: [{qid, text, filters?}]`（模型可填 1–4 条）；不填时行为不变（零回归）；
- 服务端对每条子查询分别走 dense+BM25+rerank（复用现有 24→3 流程），合并去重后返回：`items[]`、`per_subquery: [{qid, hit_eids[], coverage_score}]`、`merged_missing: []`；
- 触发策略（**不做意图路由**）：由模型自主决定是否填 `subqueries`；系统提示中只保留一句"多问句/多来源题可传 subqueries"，并在工具描述里说明参数语义；
- 生成端只看合并后的证据 + 一句"子查询覆盖：q1 已覆盖 / q2 无命中"（≤1 行，状态而非计划）。

**验证设计。**
- A/B：cross30 30 题配对（同模型/同 prompt），观察 recall/precision/faith/引用可定位率；次指标：多检索轮次、每子查询命中率、token；
- 防线：precision 下降 >2pp 即停；token 增加 >15% 需重新设计触发；用 100 题（eval100 中 cross30 层 + 20 多问句新题）补足统计；
- 增量实验：先只验证"检索侧"（零调用 recall@k/MRR/nDCG 在 30 题上），检索侧不过关不进入答案侧。

**风险与关系。** 分解错误会引入噪声（RAG-Fusion 论文自己也报告"生成查询相关性不足时答案跑题"）；因此必须允许"子查询空结果"如实返回，而不是强行凑证据。与已试"预检索 k=1/2/3"不同：不预先注入上下文（那是 cross8 precision 51–60% 的原因），只在模型要求时按子查询检索。

---

### 4.2 P1-2 时间/元数据硬过滤 + 相对时间归一

**解决：F5（q06"最近七天"、q26"下午这场"、系列范围）。**

**原理。** 检索系统的通用能力是"把不能表达为语义相似度的条件放进 filter"（Qdrant 官方文档：对要过滤的字段建 payload index、支持 must/should/嵌套；Azure AI Search 用 OData 表达式过滤可过滤字段）。时间敏感问答有独立的方法论（FreshLLMs/FreshQA 用搜索增强 + 新鲜度标注，说明"时间"需要被显式建模）。对本项目，`recorded_at` 是录音墙钟时间（已有），但只用于一条规则，没有成为检索参数。

**最小可行实现。**
- `search_knowledge` / `list_facts` 增加可选 `meeting_ids / source_ids / date_from / date_to / period(afternoon/morning) / series_id`；
- **相对时间归一（确定性规则，零调用）**：在当前时间基准（会议日期）下把"最近七天/上周/下午"映射为绝对区间；映射不确定时**不猜**：返回 `clarify_needed` 字段（现有"如实说明"的延伸），或退回不限定检索并在答案里注明；
- 元数据覆盖率检查：若目标会议缺 `recorded_at`，硬过滤降级为**软加权**（boost 而非 filter），避免漏检；
- 系列范围：`series_id` 或 `meeting_ids[]` 显式传入，与现有"资料覆盖"规则配合（coverage 回执里 `meetings_seen` 直接可见）。

**验证设计。**
- 回归：q26 3/3 必须保持；新增 20 条时间/系列题（含"最近 N 天""上午/下午场""系列里最早一次"）；指标 precision/recall + 引用可定位率；
- 否决条件：任何"应命中却因过滤漏掉"的人工判定案例 >2/20 即回退；
- 记录降级率（硬过滤→软加权）与澄清率，避免"总在澄清"的体验问题。

**与已试机制关系。** 已试"录音墙钟时间"规则让 q26 从 0/3→3/3、precision 75.3→78.4，但只是提示层规则；本机制把它变成参数与归一，可复用、可测、可回退。

---

### 4.3 P1-3 句级上下文裁剪（保留引用 span）

**解决：F3（同主题无关窗口稀释精度）。**

**原理。** Provence（2025）把上下文裁剪建模为**序列标注**，并与 reranking 统一训练，报告"可动态决定裁剪量、在多个领域几乎无性能损失、成本可忽略"；LLMLingua（EMNLP 2023）证明粗到细的提示压缩可 20× 压缩而性能小幅损失；Chain-of-Note 说明"对检索文档生成阅读笔记、评估相关性"能提升对噪声的鲁棒性（+7.9 EM 在完全噪声文档下、+10.5 拒答率提升）。Lost in the Middle 与 Context Rot 则证明**无关上下文本身会伤害性能**——这是"少而准"的理论依据。

本项目的特殊性：**引用卡片/编号是产品核心资产**，任何裁剪都必须保留"引用指向的原文片段"与位置关系。因此不能走"父块拼接"（已验证失败：recall 97.22→33.33），也不能只做去重（dedupe 删掉的 10 条全有用）。

**最小可行实现。**
- 在 rerank top-3 之后、生成之前：把每个 chunk 切成句子（保留 char offset）；用**轻量打分**选句：查询-句向量相似度 + 关键词重叠 + 是否含问题中的实体/数字/时间；保留连续区间，每 chunk 至少保留 1 段（chunk 本身高分时保留头尾各一段）；
- 返回给生成器的证据仍是 `eid` + `char_start/char_end`（引用身份不变），只是 `text` 变成裁剪后的 span；
- 护栏：问题中出现的数字/人名/时间所在句**不得删除**；被引 span 序列化时附"已裁剪"标记，前端卡片仍显示原段落（阅读层与引用层分离——这正是 parent-context 两版失败的反面教训：parent 只能做阅读层，不能替换 citation 身份）。

**验证设计。** 27 + cross30 配对；主指标 context_precision（+2–4pp 预期）、次指标 recall（−≤1pp）、faith、"引用可定位率 100%"保持；成本 +30–60ms 或 1 次小模型调用；如果 recall 掉 >2pp 直接回退。

---

### 4.4 P1-4 融合函数/RRF 权重校准

**解决：F3 的排序精度（dense/BM25 互补没有被校准利用）。**

**原理。** 当前应用层等权 RRF（只看排名、免调权重），这是稳健的选择但**不最优**：Bruch 等（TOIS 2023）在混合检索上系统比较凸组合（CC）与 RRF，发现 **RRF 对参数敏感**、CC 在域内与域外均更优、且 CC 只需少量样本即可校准唯一参数；Elastic 的 RRF 实现也把 `rank_constant`（默认 60）与 `rank_window_size` 暴露为可调参数。RAG-Fusion 用 RRF 融合多查询也说明"排名融合"是通用范式，但它的收益依赖查询质量。

**最小可行实现。**
- 保留 RRF 作为默认（fallback），dev 集上学习一个**凸组合权重 w**：`score = w * norm(dense) + (1-w) * norm(bm25)`（norm 用 min-max 或 z-score，需固定口径）；
- 或加权 RRF：`w_d/(k+rank_d) + w_b/(k+rank_b)`，只学 2 个参数；
- 只用**零调用检索指标**（recall@k/MRR/nDCG）在 100 题 + 冻结 holdout 上评估，不先跑 judge（省成本、不受 judge 漂移影响）；
- 预注册门槛：nDCG@k ≥ +2pp 且 recall@k 不降；否则保持等权 RRF。

**风险。** 单语料调参过拟合（rerank floor 已有 dev +4.29pp → holdout −3.28pp 的反例）；因此参数只能在"未参与调参"的 20–30 题上一次性验证，且记录为"若 holdout 反转则回退"。

---

### 4.5 P1-5 充分性判定与选择性生成

**解决：F2（上下文不足时硬答）、F4（何时停止检索）。**

**原理。** Sufficient Context 的核心实验：大模型在上下文充分时很准，但**不充分时倾向硬答而非弃答**；用"充分性"引导的选择性生成，把"回答中的正确率"提升 2–10%。Self-RAG 用 reflection token 让模型自适应检索与自评；Chain-of-Note 用阅读笔记在噪声文档下提升 7.9 EM、并把"应拒答"的拒答率提高 10.5；Adaptive-RAG/FLARE/DRAGIN 则从"何时检索/检索深度"侧给出机制（分类器、低置信触发、信息需求触发）。对本项目，"拒答"已有专门口径（12 条负样 100% 拒答、0 编造），可作为现成护栏。

**最小可行实现。**
- 在工具循环结束后，用 P0-2 的 `coverage/missing` 与"关键槽位是否被至少一条被引证据支撑"计算 `evidence_status ∈ {sufficient, partial, insufficient}`（确定性）；
- 把状态作为**一行结构化上下文**交给生成器（不是长 prompt）：`partial` 时要求"明确列出缺失部分"，`insufficient` 时要求"说明资料不足 + 可澄清的问题"，**不强制拒答**；
- 若选择"回答"，允许 hedging（"材料只提到…，未给出…"）；若模型仍硬答，P0-1 的 claim 校验会兜底。

**验证设计。** cross30 + 12 条负样 + 20 条"部分可答"新题；主指标 faithfulness 与"answered 中的正确率"；次指标 answer rate、拒答精度、relevancy；否决条件：负样拒答率跌破 100%、或 answer rate 下降 >5pp 且正确率未显著提升。样本：30 题只作方向，需扩到 100 题或 50 条人工盲评。

---

### 4.6 P1-6 引用感知生成（最后一遍 schema 化）

**解决：F2 的生成时错绑。**

**原理。** ALCE 把 citation 质量拆成"引用召回/精度"并给出自动指标；SelfCite 证明**句级引用**可以被自监督对齐（上下文消融奖励），并在 LongBench-Cite 上把 citation F1 提升最多 5.3 点。但另一面：Let Me Speak Freely 的研究发现**格式限制（JSON/XML）会显著降低推理能力，且限制越严格退化越大**——所以不能全程用结构化输出。

**最小可行实现。**
- 两次生成：第一次自由文本（现有风格，含 `[n]`）；第二次只做**引用格式化**：把答案拆成 `{claim, evidence_ids[]}`，要求每个 evidence_id 必须来自本轮证据表；
- 校验：`evidence_ids` 不存在 → 丢弃该 claim（或标 `unsupported`）；claim 与 evidence 的文本重叠低于阈值 → 标 `weak`；
- 输出 schema 只用于**落库与前端引用卡片**，用户看到的仍是自然语言（卡片可点回片段）。

**验证设计。** 与 P0-1 共用 40 条盲评；主指标 claim 绑定正确率；次指标答案可读性（人工 5 分制）、延迟、token；护栏：主体内容改动率 ≤10%。若 P0-1 已把绑定错误降到可接受水平，P1-6 可作为"降低在线校验成本"的第二步，而不是必须项。

---

### 4.7 P1 机制之间的顺序与依赖

```text
P0-3 评测协议（先建尺子）
   └─> P0-2 证据回执/停止判据（让状态可观测）
          ├─> P1-5 充分性/选择性生成（复用回执）
          ├─> P1-1 结构化分解（复用子查询回执）
          └─> P1-2 时间/元数据过滤（状态里加维度）
P0-1 claim 校验/重归属（不动上下文，可与上并行）
   └─> P1-6 引用感知生成（降低校验成本）
P1-3 句级裁剪 / P1-4 融合函数（独立、低耦合，最后做参数类）
```

**原则**：P0-1 与 P0-2 可以并行（互不改同一文件语义）；P1 每次只开一个，用 P0-3 的冻结 holdout 判定；P1-3/P1-4 属"参数类"，必须在机制类稳定后再做，否则读数不可归因。

---

## 5. 评测方法：把"分数"升级为"可决策的证据"

> P0-3 已给出协议骨架；本节补**指标定义、判分器校准流程、统计口径与报告模板**，目标是任何一次改动都能回答三个问题：提升了多少？区间多宽？是不是过拟合？

### 5.1 指标分层（建议同时报，不合成总分）

| 层 | 指标 | 定义/口径 | 当前来源 | 用途 |
|---|---|---|---|---|
| 零调用检索 | recall@k / MRR / nDCG@k | 用金标片段号算，不依赖 judge | `make smoke` | 不受 judge 漂移影响的锚；改检索时主看 |
| RAG 四指标 | context_recall / context_precision / faithfulness / answer_relevancy | RAGAS 口径；contexts=检索到的全部切片 | 现有 judge | 与历史可比，但只报相对变化 + CI |
| Claim 级 | claim support rate、citation binding accuracy、number/date/quote 命中率 | 规则审计 + 人工/claim 级 judge | 现有 `claim_citation_audit` + P0-1 | 定位到"哪类断言不可信" |
| 引用可核验 | 引用可定位率、未越权率、unknown/missing 数 | 已有零调用不变量 | 现有 | **硬护栏**，任何改动不得破 |
| 拒答/编造 | 拒答率（12 条负样）、fabrication 率、负样上下文 useful 率 | 现有负样专用 `refused/fabricated` | 现有 | 硬护栏 |
| 工具轨迹 | macro/micro tool F1、required coverage、first-tool strict/acceptable、over-call rate、**extra-call utility** | 金标为"最小 required 集合"；utility=是否贡献 citation | 现有 `tool_utility.py` | F1 不能单独当目标（已证） |
| 可靠性 | pass^k（k=3） | k 次独立运行全部成功的比例 | 待建 | 比单次成功率更接近体验 |
| 效率 | 轮 token、p50/p95 延迟、单轮成本、rerank 延迟 | `ai_call_log` | 现有 | 与质量同表报告 |
| 评测元指标 | judge–human κ、双族一致率、CI 半宽 | 见 §5.3 | 待建 | 决定"这次读数能不能信" |

### 5.2 RAGAS / ARES / TRACe / RAGChecker 的用法与边界

- **RAGAS**（EACL 2024 Demo）：reference-free 的 RAG 评估套件，包含 context precision（rank-aware）、context recall、faithfulness、response relevancy 等。官方文档仍把它定位为"无金标也可用"的评估框架。**边界**：它本身用 judge 实现，精度受 judge 影响；"reference-free"不等于"人工对齐"。
- **ARES**（NAACL 2024）：用合成数据微调轻量 judge，并用**少量人工标注做 PPI（prediction-powered inference）**，在 8 个知识密集任务上跨域有效。**对本项目最有价值的是 PPI 思路**：不需要 100% 人工标注，只需 ~200 条人工点即可给 judge 分加置信区间。
- **RAGBench / TRACe**（2024）：10 万例、5 个行业域的 benchmark，形式化"可解释、可行动"的 TRACe 指标族（utilization/relevance/adherence/completeness 等）。论文的关键发现是：**LLM-based RAG 评测方法在该任务上打不过 fine-tuned RoBERTa**——所以不要把"大模型当裁判"当成默认最优。
- **RAGChecker**（2024）：claim-level 的 retrieval/generation 诊断指标，声称与人类判断的相关性显著优于既有指标。**用法**：作为 P0-1 的离线 claim 级评测；**边界**：其 entailment 判定依赖另一个模型，仍需人工抽检。

**建议的组合**：RAGAS 四指标（与历史可比）+ claim 级子集（P0-1）+ 零调用检索指标（不受 judge 影响）+ 人工锚点（60–120 条）。四者不一致时，以**人工锚点 + 零调用指标**为准，judge 分只作相对方向。

### 5.3 Judge 校准流程（可执行步骤）

1. **选样**：从 100 题中分层抽 60 条答案（cross30 20、realistic 20、hard/不可答 10、new meeting 10），覆盖四个指标各自的"典型 + 边界"样例；
2. **人工标注**：每指标独立打分（不要先看 judge 分），两人独立标注 20% 计算标注者间 κ；
3. **双族 judge 运行**：两个不同模型族、固定提示、固定温度；同一答案各跑一次；
4. **对齐统计**：每指标算 judge–human 的 Cohen's κ（二分类）或 Spearman（连续）；双族之间算一致率；
5. **验收**：κ ≥0.6 且双族一致率 ≥80% → judge 可用于决策；否则只用于方向，**不用于门槛**；
6. **偏差检查**：按答案长度分桶看 judge 分（长度偏差）、按生成模型族看差异（同族偏好）、按位置打乱看稳定性（位置偏差）；
7. **冻结**：校准后的 judge 提示版本化；更换 judge 模型即视为换尺子，历史读数不可混算。

### 5.4 样本量与统计（把"方向信号"写清楚）

| 场景 | 建议最小 n | 95% CI 半宽（p≈0.8，独立近似） | 说明 |
|---|---:|---:|---|
| 27 题轨迹（tool F1） | 27 | ~±15pp | 只能测大幅变化；F1 ±0.05 需要 ≥60 题 |
| cross30 答案指标 | 30 | ~±14pp | 只能作方向；扩到 100 才有 ±8pp |
| mixed100 / 单次 100 题 | 100 | ~±8pp | 需与"组装集"区分，单次运行才算 |
| 人工盲评 | 40–60 | 逐条定性 + 比例 ~±12pp | 重点看"wrong 是否出现"，而非精确比例 |
| 目标检出 5pp 绝对提升 | ≥150–200 配对 | — | 配对设计（同题 A/B）可降方差，但样本量需求仍高；不足时只报"方向 + 护栏" |

- **配对 bootstrap**：对题目索引有放回重采样 10k 次，每次重算两臂指标差，取 2.5/97.5 分位；固定随机种子，脚本进仓；
- **多重比较**：一次实验预注册 1 个主指标；其余为次指标，报告时标注"未校正"；若同时宣称多个提升，用 Holm 校正；
- **独立 vs 配对**：同一批题做 A/B 是配对，比"两批不同题"更可信；但需防"同题记忆/缓存"，故每题 fresh session（项目已有此纪律）。

### 5.5 pass^k 与 pass@k 的区别（不要混用）

- **pass@k**（Codex 等）：k 次采样中**至少一次**成功——衡量"能力上限"，随 k 单调上升；
- **pass^k**（τ-bench）：k 次独立试验**全部**成功——衡量"可靠性"，随 k 单调下降；
- 对本项目：用户连续三次问同类问题都得到可用答案，才是体验；因此报 **pass^3**（12–27 题 ×3 trial），并同时报单次成功率。τ-bench 的实验（gpt-4o 单次 <50%、零售 `pass^8` <25%）说明"单次看起来能用"与"稳定可用"差距巨大。

### 5.6 报告模板（每次实验强制字段）

```text
机制名 / 开关名 / 默认值
对照臂：A（关）vs B（开），配对题数 n=__
数据：是否参与过调参？holdout hash=__
主指标：__，A=__，B=__，差值=__（配对 bootstrap 95% CI: [__, __]）
次指标：__（列全，含 token/延迟/成本）
护栏检查：引用可定位率__ / 负样拒答__ / stalled__ / 无贡献 over-call__
人工锚点：__ 条，correct/partial/wrong = __/__/__，judge–human κ=__
失败样例：≥5 条（题号 + 原因）
结论：采纳 / 回退 / 仅标注（不许写"预期提升"）
```

### 5.7 如何避免"判分器与生成器同族"

- 结构性做法：**生成模型族 ≠ judge 族**；若只能同族，至少用不同提示 + 规则交叉验证；
- 用**零调用指标**（recall@k/MRR/nDCG/引用可定位/数字命中）作为"不受 judge 偏好影响"的旁证；
- 对 judge 做"盲化"：不告诉 judge 答案来自哪个模型；打乱 A/B 顺序；长度分桶后比较；
- 参考 self-preference 研究的结论：同族偏好与"自我识别"相关，不是随机噪声，**不能靠多跑几次平均掉**。

### 5.8 如何避免"按题过拟合 / 单会议调参"

- 所有规则写成**机制级参数**（阈值、字段、判据），不写"q26 如果…"；
- 调参集与验证集分离，且验证集**只跑一次**（文件 hash 冻结）；
- 记录 dev→holdout 的历史 delta（已有 rerank floor 的 +4.29→−3.28 反例）；
- 每季度做一次"污染自检"：用题目/答案文本对 prompt/docs 做 n-gram 重叠检索（TS-Guessing 的简化工程版）；
- 结论层写作纪律：单会议/单裁判/组装集读数只能写"方向信号"，不能写"普遍水平"。

---

## 6. 不建议做清单

| # | 不做 | 理由 | 证据 |
|---|---|---|---|
| 1 | **继续加长全局 prompt / 全局证据约束** | 已试证据边界 prompt：faith +6.2pp 但 precision −8.2pp；长上下文本身会非均匀退化；格式/指令越长越挤占答案预算 | 内部 `2026-10-22_evidence_prompt.md`；Lost in the Middle（TACL 2024）；Context Rot（Chroma 2025，18 模型） |
| 2 | **硬压 over-call（按数量/意图/预算）** | 已试 4 种方案全部 F1↑、faith 88.63→73.53~83.69；11/18 次 extra call 有引用贡献 | 内部 `2026-10-22_tool_f1_deep_dive.md`；τ-bench（pass^k 可靠性）；ToolBeHonest（可解性判断才是主错） |
| 3 | **靠加 k / 加候选深度 / 塞更多上下文提精度** | 40 候选 +0.5pp（检不出）、延迟 +36.5%、成本 +65.7%；top_k=2 虽 +2.14pp precision 但 recall −14.84、faith −5.74 | 内部标定；Lost in the Middle；Anthropic "更多 chunk 有上限" |
| 4 | **把 tool F1 当唯一目标 / 强制首工具** | F1 是"最小 required 集合"口径，不奖励有贡献的多查；强制首工具让 cross30 recall 68.97、precision 57.14、faith 82.05 | 内部 tool_f1 报告；τ-bench（规则遵循与一致性） |
| 5 | **单会议/单裁判调参，把 judge 分当人工金标** | 0 人工金标；holdout 已参与调参；LLM judge 有同族/长度/位置偏差；fine-tuned GPT-3.5 归因仅 ~80 macro-F1；LLM judge 在 RAGBench 上不如 fine-tuned RoBERTa | 内部评测审计；self-preference；AttributionBench；RAGBench；MT-Bench（偏差清单） |
| 6 | **把已试失败机制原样重开为默认** | query plan 提示、contextual retrieval、parent context、dedupe、score floor、CRAG、turn filter、路由/强制首工具均已有否决读数 | §7；`generalization_audit_v2` |
| 7 | **直接上 GraphRAG / ColBERT / 语义切分 / Self-RAG 训练** | 需求规模不匹配（系列题 30 条、工具 5 个、27 题 0 多跳）；语义切分"计算成本是否值得"论文未给肯定结论；训练需基础设施 | GraphRAG 论文；语义切分成本研究；`问答链路分析` 5 节；`RAG链路参数` 4 节 |
| 8 | **无外部反馈的"自我纠错"当修复手段** | CRITIC 明确指出**外部反馈**才是自我改进关键；CoVe 要求验证问题**独立回答**；Self-RAG 需训练 | CRITIC；CoVe；Self-RAG |
| 9 | **用 pass@k 冒充可靠性** | pass@k 衡量上限（至少一次成功），会系统性高估可用性 | τ-bench（pass^k 定义） |
| 10 | **把 AI 复核写成 human / 用 LLM 生成 gold 当金标** | 现有 gold 全部 `human_checked=false`；AI 复核与人工不可混用 | 内部 `intent_metrics` 口径；评测审计 |
| 11 | **按题/按会议写规则** | 规则在 27 题上调过即不可外推；项目已有"不按 case 硬编码"的通用性审计原则 | `generalization_audit_v2` |
| 12 | **多 Agent 编排解决单 Agent 问题** | 27 轮里 0 次并行子任务/信任边界；Anthropic 的工程原则是"从最简方案开始" | `问答链路分析` 5 节；Building Effective Agents |
| 13 | **直接拿 HHEM/小模型 judge 替换人工而不校准** | 领域漂移（英文/通用 vs 中文会议）；RAGTruth 证明可行但必须在本域校准 | HHEM 模型卡；RAGTruth |
| 14 | **只做"工具输出字段裁剪"就当解决 precision** | 已做 get_minutes.fields / list_facts.keywords / 摘录预算，方向正确但 68 precision 67.2% <68、fact 57.1% <65，未达主指标 | 内部读数（RAG链路参数 §2） |

---

## 7. 已试机制复盘（为什么失败，什么条件下才值得重开）

| 机制 | 实测结果 | 失败原因（本项目归因） | 重开条件 / 归属 |
|---|---|---|---|
| Query plan 提示 | 检索 recall@k 0.846→0.897、MRR 0.627→0.673、nDCG 0.668→0.709；答案 A/B（27 题）precision 78.9→76.1、faith 94.2→98.2（+4.0 < +4.8 门槛） | 提示层规划挤占答案预算；分解/改写结果没有"覆盖回执"，模型无法据此补查；预取会抬大上下文分母 | 只以 **P1-1 结构化分解**（工具参数+回执，触发式）重开，且须过独立 holdout |
| Turn filter | 无稳定增益 | 过滤按相似度/轮次删证据，可能删掉有用窗口（与 dedupe 同类风险） | 并入 **P1-3 句级裁剪**，按"是否含问题实体/数字"而非相似度 |
| CRAG 3-way | holdout 无稳定收益 | 检索评估器与 rerank 功能重叠，且多一次判定调用；阈值在单语料上不可迁移 | 并入 **P1-5 充分性状态**（只在 insufficient 时触发，不做 3-way 常数判定） |
| Contextual retrieval | 300×3 上 precision 77.3% vs 81.0%（有用条数相同、分母 +4），MRR 0.870→0.889、nDCG 0.886→0.893、AR 96.3→100 | 主指标是"precision"，分母变大即被判负；且单会议语料，索引期 LLM 收益不可迁移 | 只用**检索侧失败率**（如 top-20 未命中率下降 ≥20%）作门禁，且只对孤儿/跨录音块做（P2） |
| Parent context | 第一版 recall 97.22→33.33、precision 64.23→13.80、faith 90.75→78.33；第二版（+parent_text）50.00/15.30/77.60/66.67 | 父块替换/污染了 child 的 citation 身份，引用语义崩了 | 只作**阅读层**（前端展开），绝不进 citation/tool payload；P1-3 已按此设计 |
| Dedupe（相邻窗口） | precision 81.0→78.4、recall 不变；删掉的 10 条**全部被判有用** | 相邻窗口不是重复噪声，而是各含答案的上下文 | 不重开；如需去冗余，改用"按新事实覆盖"的 MMR 变体（P2） |
| Rerank score floor | dev（cross30）floor=0.5：precision 67.27→71.57（+4.29）、recall 80.36→82.14、faith 79.10→80.75；**holdout B：precision 68.85→65.57（−3.28）** | 单会议/单分布下调参，holdout 反转；分数分布随语料漂移 | 多会议 + 预注册 + 分来源类型阈值，或不做 |
| 候选深度 24→40 | precision +0.5pp（检不出）；rerank 延迟 278.5→380ms（+36.5%）、token +65.7%、成本 +65.7% | 收益低于测量噪声，成本显著 | 保持 24 |
| top_k 3→2 | precision +2.14pp，但 recall −14.84pp、faith −5.74pp | 精度不能靠截断来源换；会放大"该覆盖没覆盖"（F1） | 不重开 |
| 意图路由/选择性路由/hint/budget/强制首工具 | F1 0.7309→0.7654~0.8519、strict 14→17~19，但 cross30 faith 88.63→73.53~83.69；强制首工具 recall 68.97/precision 57.14/faith 82.05 | 按意图或数量压制，没有按"是否贡献证据"；路由误判被放大 | 由 **P0-2 回执/贡献判据**替代；路由不恢复 |
| 证据边界 prompt | cross30 faith 78.6→84.8、relevancy 96.7→100，但 precision 74.6→66.4 | 生成前全局约束让模型更保守/更爱补查，上下文分母变大 | 由 **P0-1 事后校验**替代 |
| 入口预检索 k=1/2/3 | cross8 precision 51–60%，recall 87.5–93.8 | "覆盖优先"把无关窗口也灌进来，分母爆炸 | 保留"条件补搜"（72.1/93.8）路径，预检索不恢复 |
| 录音墙钟时间规则 | q26 0/3→3/3；precision 75.3→78.4；检索三项 0.944/0.830/0.855→0.981/0.883/0.905 | 成功，但只覆盖单题/单场景 | **P1-2 泛化**为检索参数与相对时间归一 |
| 结构化工具收窄 | 68 集 precision +2.9pp、recall +1.6pp、fact +15.6pp（41.5→57.1%），但 fact 57.1%<65、68 precision 67.2%<68 | 方向正确、未达主指标；工具输出形状仍不携带覆盖状态 | 由 **P0-2 回执**继续推进 |

**复盘总结论**：已试机制的失败有共同结构——**都在"提示/意图/数量"层做约束，没有把"证据覆盖与引用绑定"变成数据与流程**。这正好是 P0-1/P0-2 的位置。

---

## 8. 落地路线（2–3 周 + 2 个月）

### 8.1 周计划

| 周 | 动作 | 产出 | 门禁 |
|---|---|---|---|
| W0（3 天） | 冻结评测集（hash）；60 条人工锚点抽样；双族 judge 校准跑一轮；P0-2 回执字段在 shadow 模式记录（不改行为） | 校准报告（κ/一致率）；shadow 日志 | κ≥0.6 才继续用 judge 作门槛；shadow 里"状态计算错误率"人工抽检 ≤10% |
| W1 | P0-1 规则层（数字/日期/引语 → rebind/hedge/drop）+ 日志；P0-2 schema + 重复调用抑制（flag 默认关） | 离线 A/B 脚本；40 条逐 claim 人工判 | 护栏：引用可定位 100%、负样 12/12、答案改动率 ≤30% |
| W2 | P0-1 小模型裁决层（仅不确定 claim）；P0-2 开启 shadow→flag A/B；P0-3 报告模板进 CI | A/B 报告（配对 + CI） | P0-1：faith +3pp 且 precision −≤1pp；P0-2：F1 +0.04、recall +2pp、faith 不降 |
| W3 | 决策：P0-1/P0-2 是否默认开；失败则回退并记录反例 | 决策记录 | 任一门禁不过即回退，不带病上线 |
| W4–5 | P1-1 或 P1-2（按失败归因二选一） | 检索侧零调用指标 + 答案 A/B | 先零调用后 judge；precision −≤2pp |
| W6–8 | P1-3 / P1-4（低耦合参数类） | A/B 报告 | 预注册阈值；holdout 一次性 |
| 季度 | 污染自检、报告复核、公开失败样例 | 季度评测审计 | 与 `generalization_audit_v2` 原则一致 |

### 8.2 验证矩阵（每个机制的最小验证）

| 机制 | 样本 | 主指标 | 护栏 | 回退条件 |
|---|---|---|---|---|
| P0-1 | 40 盲评 + cross30 | 引用绑定正确率 / faith | precision 不降；引用可定位 100%；负样 12/12 | faith −、precision −>1pp、改动率 >30% |
| P0-2 | 27 轨迹 + cross30 + 100 题 | tool F1、无贡献 over-call、recall | faith ≥88.6；无"只漏"；stalled=0 | F1 不升或 faith 掉 >1pp |
| P0-3 | 60–100 人工 | κ、CI 半宽 | judge 双族一致 ≥80% | κ<0.6 时禁止作门槛 |
| P1-1 | 30 cross30 + 20 多问句 | recall/precision | token +≤15%；precision −≤2pp | 检索侧零调用不过先停 |
| P1-2 | q26 回归 + 20 时间题 | precision/recall | 漏检案例 ≤2/20 | 任何"应命中未命中"人工确认即回退 |
| P1-3 | 27 + cross30 | precision | recall −≤1pp；引用可定位 100% | recall −>2pp |
| P1-4 | 100 题 + holdout | nDCG/MRR | recall 不降 | holdout 反转 |
| P1-5 | cross30 + 12 负样 + 20 部分可答 | answered 正确率、faith | 拒答率 100% 保持 | answer rate −>5pp 且正确率无显著提升 |
| P1-6 | 40 盲评 | 绑定正确率 | 主体改动率 ≤10% | 可读性人工下降 |

### 8.3 成本估算（量级）

| 项 | 估算 |
|---|---|
| P0-1 规则层 | 纯代码，0 元/轮；开发 2–3 人日 |
| P0-1 小模型裁决（≤20% claims） | 约 1–3k token/轮，按现有单价（未定价项需先补价）→ 单轮增量成本可忽略，但**必须进 `ai_call_log` 计价** |
| P0-2 回执/状态 | 纯代码；延迟 +<50ms（本地计算） |
| P0-3 人工锚点 | 60–100 条 × 2 次复核 ≈ 2–4 人日 |
| P1-2 时间过滤 | 1–2 人日 + 20 条题；无模型成本 |
| P1-4 融合校准 | 离线网格搜索（零模型调用），1 人日 |
| 总量 | 2–3 周内可完成 P0；P1 各 3–5 人日 |

### 8.4 回滚与默认值纪律

- 所有新机制 **flag 默认 0**，与 `generalization_audit_v2` 的"未过新 holdout 不得默认开"一致；
- 一次只开一个机制；开关粒度到"机制"不到"题"；
- 每个 flag 必须有：关闭路径、旧行为快照、回滚命令/配置说明；
- 若上线后出现 `stalled`、引用未定位、负样编造任一，**当天回滚**，不"观察一天再说"。

---

## 9. 参考文献（全部为 2026-10-22 实测可访问；标题为核验时页面标题）

**检索与多跳**
1. Trivedi et al. *Interleaving Retrieval with Chain-of-Thought Reasoning for Knowledge-Intensive Multi-Step Questions* (ACL 2023). https://aclanthology.org/2023.acl-long.557/ — HotpotQA/2Wiki/MuSiQue/IIRC 上检索 +21、QA +15；OOD 有效。
2. Zhou et al. *Least-to-Most Prompting Enables Complex Reasoning in Large Language Models* (ICLR 2023). https://arxiv.org/abs/2205.10625 — 拆子问题；在 SCAN/组合泛化等合成任务上验证，非开放域 RAG。
3. Press et al. *Measuring and Narrowing the Compositionality Gap in Language Models*（Self-Ask，EMNLP Findings 2023）. https://arxiv.org/abs/2210.03350 — 自问自答式分解。
4. Jagerman et al. *Query Expansion by Prompting Large Language Models* (2023). https://arxiv.org/abs/2305.03653 — CoT 提示生成扩展查询，在 MS-MARCO/BEIR 上强于 PRF。
5. Rackauckas. *RAG-Fusion: a New Take on Retrieval-Augmented Generation* (2024). https://arxiv.org/abs/2402.03367 — 多查询 + RRF；人工小样本评估，作者自述"查询跑偏会答非所问"（B 级证据）。
6. *MultiHop-RAG: Benchmarking Retrieval-Augmented Generation for Multi-Hop Queries* (2024). https://arxiv.org/abs/2401.15391 — 多跳查询 benchmark，说明多跳检索是独立于单跳的评测维度。
7. *From Local to Global: A Graph RAG Approach to Query-Focused Summarization* (2024). https://arxiv.org/abs/2404.16130 — 全局/摘要类问题的图索引方法；面向 1M token 级语料。
8. *FreshLLMs: Refreshing Large Language Models with Search Engine Augmentation* (2023). https://arxiv.org/abs/2310.03214 — 时间敏感/新鲜度问题。
9. Azure Architecture Center — *Develop a RAG Solution: Information-Retrieval Phase*. https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-information-retrieval — 工程指南：先判断是否需要多次检索、子问题自包含、RRF→rerank→截断。
10. Qdrant — *Filtering*. https://qdrant.tech/documentation/search/filtering/ — payload 索引、must/should/嵌套、先建索引再写入。
11. Microsoft Learn — *Text Query Filters (Azure AI Search)*. https://learn.microsoft.com/en-us/azure/search/search-filters — OData 过滤可过滤字段。

**上下文精度与排序**
12. Carbonell & Goldstein. *The Use of MMR, Diversity-Based Reranking for Reordering Documents and Producing Summaries* (SIGIR 1998). https://www.cs.cmu.edu/~jgc/publication/The_Use_MMR_Diversity_Based_LTMIR_1998.pdf — 多样性重排的原始定义（相关性−冗余），用于"同主题窗口"的第二证据选择。
13. Bruch et al. *An Analysis of Fusion Functions for Hybrid Retrieval* (TOIS 2023). https://arxiv.org/abs/2210.11934 — RRF 对参数敏感；凸组合在域内/域外优于 RRF。
14. Elastic — *Reciprocal rank fusion*. https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html — rank_constant / rank_window_size 的工程语义。
15. *Provence: efficient and robust context pruning for retrieval-augmented generation* (2025). https://arxiv.org/abs/2501.16214 — 序列标注式裁剪 + 与 rerank 统一；跨域性能几乎无损。
16. Jiang et al. *LLMLingua: Compressing Prompts for Accelerated Inference of Large Language Models* (EMNLP 2023). https://arxiv.org/abs/2310.05736 — 最高 20× 压缩。
17. Yu et al. *Chain-of-Note: Enhancing Robustness in Retrieval-Augmented Language Models* (2023). https://arxiv.org/abs/2311.09210 — 噪声文档下 +7.9 EM、拒答率 +10.5。
18. Liu et al. *Lost in the Middle: How Language Models Use Long Contexts* (TACL 2024). https://aclanthology.org/2024.tacl-1.9/ — 相关信息在中间显著退化。
19. Hong et al. *Context Rot: How Increasing Input Tokens Impacts LLM Performance* (Chroma 技术报告, 2025). https://www.trychroma.com/research/context-rot — 18 个模型；输入变长性能非均匀退化；干扰项放大退化。
20. Günther et al. *Late Chunking: Contextual Chunk Embeddings Using Long-Context Embedding Models* (2024). https://arxiv.org/abs/2409.04701 — 先长上下文编码、后切分再池化。
21. *Is Semantic Chunking Worth the Computational Cost?* (NAACL 2025 Findings). https://aclanthology.org/2025.findings-naacl.114/ — 语义切分收益与成本之比的实证质疑。
22. Anthropic — *Contextual Retrieval*. https://www.anthropic.com/research/contextual-retrieval — top-20 检索失败率 −49%（+rerank −67%）；contextual chunk 生成成本 $1.02/M tokens。
23. Yu et al. *RankRAG: Unifying Context Ranking with Retrieval-Augmented Generation in LLMs* (2024). https://arxiv.org/abs/2407.02485 — 单模型统一 rerank+生成。
24. Sun et al. *Is ChatGPT Good at Search? Investigating Large Language Models as Re-Ranking Agents* (EMNLP 2023). https://arxiv.org/abs/2304.09542 — LLM listwise rerank 零样本可比监督方法。

**忠实度、引用与幻觉**
25. Dhuliawala et al. *Chain-of-Verification Reduces Hallucination in Large Language Models* (ACL Findings 2024). https://arxiv.org/abs/2309.11495 — 起草→验证问题→独立回答→重写。
26. Glass et al. *RARR: Researching and Revising What Language Models Say, Using Language Models* (ACL 2023). https://arxiv.org/abs/2210.08726 — 事后归因 + 最小改动修订。
27. Gou et al. *CRITIC: Large Language Models Can Self-Correct with Tool-Interactive Critiquing* (ICLR 2024). https://arxiv.org/abs/2305.11738 — 强调外部反馈是自我改进关键。
28. Min et al. *FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation* (EMNLP 2023). https://arxiv.org/abs/2305.14251 — 原子事实支持率；自动化估计误差 <2%。
29. Wei et al. *Long-form factuality in large language models*（SAFE，2024）. https://arxiv.org/abs/2403.18802 — 拆原子事实 + 搜索核验；与人工一致 72%，分歧中 76% 判为 SAFE 更优，成本低 20×。
30. Niu et al. *RAGTruth: A Hallucination Corpus for Developing Trustworthy Retrieval-Augmented Language Models* (ACL 2024). https://arxiv.org/abs/2401.00396 — ~18k 响应、词级标注；微调小模型可接近 GPT-4 prompt 检测。
31. Li et al. *AttributionBench: How Hard is Automatic Attribution Evaluation?* (2024). https://arxiv.org/abs/2402.15089 — fine-tuned GPT-3.5 仅 ~80 macro-F1。
32. *SelfCite: Self-Supervised Alignment for Context Attribution in Large Language Models* (2025). https://arxiv.org/abs/2502.09604 — 上下文消融奖励；LongBench-Cite citation F1 +5.3。
33. *CiteFix: Enhancing RAG Accuracy Through Post-Processing Citation Correction* (ACL 2025 Industry). https://aclanthology.org/2025.acl-industry.23/ — 后处理引用校正；整体指标相对 +15.46%；行业引用准确率约 74%。
34. Gao et al. *Enabling Large Language Models to Generate Text with Citations*（ALCE，EMNLP 2023）. https://arxiv.org/abs/2305.14627 — citation recall/precision 自动指标；ELI5 上最好模型 50% 引用不完整。
35. Vectara — *hallucination_evaluation_model* (HHEM). https://huggingface.co/vectara/hallucination_evaluation_model — 公开幻觉评估模型。

**工具与 Agent**
36. Yao et al. *τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains* (2024). https://arxiv.org/abs/2406.12045 — 数据库终态判分；`pass^k`；gpt-4o 单次 <50%、零售 `pass^8` <25%。
37. Joren et al. *Sufficient Context: A New Lens on Retrieval Augmented Generation Systems* (2024/2025). https://arxiv.org/abs/2411.06037 — 充分性分层；选择性生成使 answered 正确率 +2–10%。
38. Zhang et al. *ToolBeHonest: A Multi-level Hallucination Diagnostic Benchmark for Tool-Augmented LLMs* (EMNLP 2024). https://aclanthology.org/2024.emnlp-main.637/ — 可解性检测为主要错误源；GPT-4o 37/100。
39. Berkeley — *Berkeley Function-Calling Leaderboard*. https://gorilla.cs.berkeley.edu/leaderboard.html （博客：https://gorilla.cs.berkeley.edu/blogs/8_berkeley_function_calling_leaderboard.html ） — AST 比对、含 irrelevance detection。
40. Patil et al. *Gorilla: Large Language Model Connected with Massive APIs* (2023). https://arxiv.org/abs/2305.15334 — API 参数错误/误用是主因；检索增强 API 文档可缓解。
41. Schick et al. *Toolformer: Language Models Can Teach Themselves to Use Tools* (2023). https://arxiv.org/abs/2302.04761 — 何时/如何调用工具的自我监督。
42. Yao et al. *ReAct: Synergizing Reasoning and Acting in Language Models* (ICLR 2023). https://arxiv.org/abs/2210.03629 — 行动-观察循环。
43. Jeong et al. *Adaptive-RAG: Learning to Adapt Retrieval-Augmented LLMs through Question Complexity* (NAACL 2024). https://arxiv.org/abs/2403.14403 — 复杂度分类器选择检索深度。
44. Jiang et al. *Active Retrieval Augmented Generation*（FLARE，EMNLP 2023）. https://arxiv.org/abs/2305.06983 — 生成中按置信度触发检索。
45. Su et al. *DRAGIN: Dynamic Retrieval Augmented Generation based on the Real-time Information Needs of LLMs* (ACL 2024). https://arxiv.org/abs/2403.10081 — 何时/检索什么由实时信息需求决定。
46. Asai et al. *Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection* (ICLR 2024). https://arxiv.org/abs/2310.11511 — reflection tokens；自适应检索；长文事实性与引用提升。
47. Anthropic — *Building effective AI agents*. https://www.anthropic.com/engineering/building-effective-agents — 从最简方案开始；路由需可靠分类。

**评测与统计**
48. Es et al. *Ragas: Automated Evaluation of Retrieval Augmented Generation* (EACL 2024 Demo). https://arxiv.org/abs/2309.15217 ｜文档：https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/
49. Saad-Falcon et al. *ARES: An Automated Evaluation Framework for Retrieval-Augmented Generation Systems* (NAACL 2024). https://arxiv.org/abs/2311.09476 — 轻量 judge + PPI（少量人工标注）提供统计校正。
50. Friar et al. *RAGBench: Explainable Benchmark for Retrieval-Augmented Generation Systems* (2024). https://arxiv.org/abs/2407.11005 — 100k 例、5 行业域、TRACe 指标；LLM-based 评测不如 fine-tuned RoBERTa。
51. Ru et al. *RAGChecker: A Fine-grained Framework for Diagnosing Retrieval-Augmented Generation* (2024). https://arxiv.org/abs/2408.08067 — claim-level 检索/生成诊断指标。
52. Zheng et al. *Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena* (NeurIPS 2023 D&B). https://arxiv.org/abs/2306.05685 — >80% 人类一致，但点名位置/长度/自我增强偏差。
53. Liu et al. *G-Eval: NLG Evaluation using GPT-4 with Better Human Alignment* (EMNLP 2023). https://arxiv.org/abs/2303.16634 — CoT + 表单式；summarization Spearman 0.514。
54. Kim et al. *Prometheus 2: An Open Source Language Model Specialized in Evaluating Other Language Models* (2024). https://arxiv.org/abs/2405.01535 — 开源 judge 可与人类/GPT-4 高度一致。
55. Panickssery et al. *LLM Evaluators Recognize and Favor Their Own Generations* (2024). https://arxiv.org/abs/2404.13076 — 自我偏好与自我识别线性相关。
56. Dubois et al. *Length-Controlled AlpacaEval: A Simple Way to Debias Automatic Evaluators* (2024). https://arxiv.org/abs/2404.04475 — 长度去偏后与 Arena Spearman 0.94→0.98。
57. *Investigating Data Contamination in Modern Benchmarks for Large Language Models* (2023). https://arxiv.org/abs/2311.09783 — 检索重叠 + TS-Guessing 检测污染。
58. *Let Me Speak Freely? A Study on the Impact of Format Restrictions on Performance of Large Language Models* (2024). https://arxiv.org/abs/2408.02442 — 格式限制显著降低推理，限制越严退化越大。
59. Barnett et al. *Seven Failure Points When Engineering a Retrieval Augmented Generation System* (2024). https://arxiv.org/abs/2401.05856 — 经验报告：验证只能在运行中完成。
60. *Searching for Best Practices in Retrieval-Augmented Generation* (2024). https://arxiv.org/abs/2407.01219 — 系统比较 RAG 各阶段组合。
61. *Retrieval Augmented Generation or Long-Context LLMs? A Comprehensive Study and Hybrid Approach* (2024). https://arxiv.org/abs/2407.16833 — 充分资源下长上下文更强但成本高；Self-Route 折中。

---

## 附录 A：论文证据与本项目的分布差异（外推前的检查表）

| 外部证据 | 任务/数据 | 与本项目的差异 | 外推强度 |
|---|---|---|---|
| IRCoT / Least-to-Most / Self-Ask | 英文多跳 QA（HotpotQA、MuSiQue 等）、合成组合任务 | 中文会议口语、短切片、系列题；多跳是"跨录音找事实"而非实体链推理 | 中：机制（交替检索/分解）可迁移，数字不可迁移 |
| ALCE / SelfCite / CiteFix | 英文长文 QA（ELI5、LongBench-Cite）或英文生产 RAG | 中文、编号+卡片引用体系、会议纪要文体 | 中高：citation 质量是同一类问题 |
| RARR / CoVe / CRITIC | 英文知识密集生成/事实核查 | 本项目有轮内证据表，可省去 web 检索环节 | 高（机制级） |
| FActScore / SAFE / RAGChecker | 英文传记/长文事实性 | claim 拆分器需中文适配；本项目 claim 密度高（20–30 条/答案） | 高（方法），需本地校准 |
| τ-bench / ToolBeHonest / BFCL | 英文 API 域（零售/航空/多类工具） | 本项目 5 个工具、中文口语任务、单轮为主 | 中：可靠性/可解性结论方向可迁移 |
| RAGBench / RAGAS / ARES | 英文行业文档/知识密集任务 | 中文会议纪要，judge 需本地化 | 中：评测方法可迁移，阈值需重标定 |
| Anthropic Contextual Retrieval | 多域英文语料 | 单会议中文语料，已实测收益不迁移 | 低（仅机制解释用） |
| Lost in the Middle / Context Rot | 英文长上下文合成任务 | 中文、上下文通常 <10k token，但"无关上下文伤害"方向一致 | 中高 |

## 附录 B：术语与口径速查

- **Claim**：可独立核验的最小断言（含数字/日期/实体/因果）。本项目答案平均 20–30 条。
- **Span（证据片段）**：工具返回的原文片段 + char offset；引用卡片的最小单位。
- **Rebind / Hedge / Drop**：引用重绑、措辞降级、删除无依据子句——P0-1 允许的三种修复动作。
- **Coverage / Slot**：本轮已覆盖的来源集合 / 问题要求的条件槽位（时间点、人物、数量、跨来源、系列范围）。
- **No-contribution over-call**：多调工具但未贡献任何被引用证据、也未补齐槽位。
- **pass^k vs pass@k**：见 §5.5。
- **PPI**：prediction-powered inference，用少量人工标注校正大量模型预测并给 CI（ARES）。
- **RRF / CC**：Reciprocal Rank Fusion（只看排名）/ 凸组合（分数加权，可校准）。
- **RAGAS 四指标口径**：contexts = 检索到的全部切片（不是答案实际引用的切片）——本项目沿用此口径，注意与"被引上下文"的 claim 审计不是同一分母。

## 附录 C：本文的核验方式与限制

- **核验方式**：每条外部链接在 2026-10-22 通过 `web_search` 检索 + `web_fetch`/HTTP 请求核验可访问，并抓取标题/摘要；引用结论以**论文摘要与官方文档正文**为准，未逐篇精读实验细节；
- **未做的事**：没有改任何代码、没有跑模型调用、没有重跑评测；本文所有"预期影响"均为**待验证假设**，不替代实测；
- **已知局限**：①部分论文数字来自英文数据集，不能外推到中文会议语料；②工具调用/充分性的论文多为单轮或特定 API 域；③本文对"本项目已试机制"的复盘依据为仓库内文档与报告（`docs/Agent指标.md`、`docs/RAG链路参数.md`、`evaluation/LEDGER.md`、`evaluation/reports/2026-10-2x_*.md`），未重新核对其原始日志；
- **更新方式**：若 P0 实验结果与本文预期方向相反，应更新 §7 的复盘表与 §1 的优先级，而不是修改预期数字去贴合结果。
