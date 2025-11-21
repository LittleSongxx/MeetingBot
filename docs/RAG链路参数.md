# RAG 全链路参数（标准化配置，2026-10-17）

这份表回答一个问题：**这条 RAG 链路的每个可调参数现在取什么、为什么取这个、改了要付什么代价。**
取值的依据是公开做法（Qdrant 官方索引/过滤文档、Azure RAG 检索阶段指南、DashScope 模型文档）
与本语料上的实测（`evaluation/LEDGER.md` 的「标准化链路」一节），**不做学术式消融**。

## 一、链路与参数

```
转写/纪要 ──切块──▶ 切片（500 字窗口，2 行重叠）
    ├─ 稠密：qwen3.7-text-embedding（1024 维）
    ├─ 稀疏：字符 bigram 次线性 TF + Qdrant 服务端 IDF（= BM25 词法口径）
    ▼
Qdrant 混合检索（prefetch 24 × 两路，服务端 RRF 融合）
    ▼
qwen3.7-text-rerank（专用 cross-encoder 类，24 候选打分）
    ▼
截断 top-5 ──▶ 生成模型
```

| 参数 | 当前值 | 位置 | 改它要付什么 |
|---|---|---|---|
| 窗口字数 | **500**（`KB_CHUNK_TARGET_CHARS`） | `services/kb_chunking.py` | 换值 = 切片正文变 → 内容指纹变 → 下次索引自动重建（要重嵌） |
| 重叠 | 2 行（约 15%） | 同上（常量） | 同上；重叠太少会切断跨句事实，太多会重复召回 |
| 切分方式 | 句读边界（`。！？；`）+ 单句上限 800 字硬切 | 同上 | 会议转写是连续口语，没有章节结构；按句读切比固定窗口稳定 |
| 稠密模型 / 维度 | `qwen3.7-text-embedding` / **1024**（`EMBEDDING_MODEL`、`EMBEDDING_DIM`） | `.env` → bootstrap 同步到 `ai_model_config` | 换模型/维度 = 换向量空间，必须重建（指纹含模型名与维度，自动触发） |
| 稀疏臂 | 字符 bigram 次线性 TF + **IDF 由 Qdrant 按语料统计** | `services/kb_sparse.py`、`common/qdrant.py` | `modifier=idf` 是**建库时**的声明；换它要换 collection 名重建（代码会 fail-fast 而不是静默退化） |
| 混合与融合 | 两路各 prefetch 24，服务端 **RRF**（`query.fusion=rrf`） | `common/qdrant.py` | 候选深度影响召回；RRF 免调权重 |
| 重排 | `qwen3.7-text-rerank`（`RERANK_MODEL`；`RERANK_BASE_URL` 可指专属端点） | `services/model_factory.rerank_documents` | 每次检索一次 HTTP；实测 **平均 476ms、最大 2.35s**、约 5.4k token/次（24 候选） |
| 重排开关 | 默认**开**（`KB_RERANK_ENABLED`） | `services/kb_service.py` | 关掉省延迟，但 context_precision 会掉 |
| 截断 | **top-5**（`RERANK_TOP_K`） | 同上 | k 越大召回越宽、单条精度越低（见第二节实测） |
| 工具默认条数 | **5**（模型可自己要到 20，重排后仍截到 top-5） | `services/kb_tools.py` | 模型要更多也只会拿到 5 条；要更大就调 `RERANK_TOP_K` |

## 二、本语料实测（27 条人工标注题；判定 = qwen + deepseek 两族，关思考）

标准口径（RAGAS：contexts = **检索到的全部切片**，不是"答案实际引用的那些"）：

| 配置 | context_precision | context_recall | faithfulness 内容级 | answer relevancy | recall@k / MRR / nDCG |
|---|---|---|---|---|---|
| k=8（未截断） | 33.5% | 93.5% | 100% | 100% | 0.919 / 0.827 / 0.854 |
| **k=5（当前）** | **51.5%** | **95.2%** | 97.0% | 96.3% | **0.956 / 0.846 / 0.879** |
| k=8 + 300 字窗口 | 37.3% | 91.9% | 99.3% | 100% | 0.889 / 0.796 / 0.799 |

跨族一致性（当前配置）：precision 51.5%（qwen）/ 53.0%（deepseek）、recall 95.2% / 95.2%、
内容级支撑 97.0% / 94.5%、relevancy 96.3% / 96.3%——**两族在 precision/recall/relevancy 上一致**，
faithfulness 有 2.5pp 差异（切分粒度不同，历史已知）。

**读法**：`context_precision` 是这条链路的短板，原因不是检索不到（recall 95%），
而是"一次问一两个事实，却要交 5 段各约 2 分钟的窗口"——每段都沾主题，只有一两段真的含答案。
这是 k 与窗口大小的函数，不是某个模型的错。

## 三、改参数的正确顺序

1. 改 `.env`（块大小/模型/k）→ 重启 backend（bootstrap 会把模型配置同步进库）；
2. 触发重建索引（三个来源各 POST 一次 `/kb/index`）——**内容指纹覆盖了正文、payload 结构、
   嵌入模型与 collection**，所以改这些都会自动重建；**稀疏 IDF 与 collection 名绑死**，
   换它要改 `QDRANT_COLLECTION`；
3. 重建后用 `make smoke` 看零调用检索指标（recall@k/MRR/nDCG），再决定要不要跑判定。

## 四、明确**没有**做的（避免被当成遗漏）

| 没做 | 为什么 |
|---|---|
| 语义切分 / 父子块 / Contextual Retrieval | 都要额外机制（父块表、索引时逐块 LLM 调用）；本语料 128 个片段、40 个切片，收益与成本不成比例。**块大小与重叠已留成可配参数**，要做先量收益 |
| 多向量 / ColBERT | 需要新索引与 GPU 侧能力；当前混合+重排已覆盖"精确串 + 语义"两类召回 |
| k 的自适应（按问题类型给不同 k） | 需要先有可靠的意图路由；该路由的逐类 F1 目前还有两类不可用（见 LEDGER） |
| 修改 BM25 的 k1/b | Qdrant 的稀疏 IDF 只暴露 modifier；完整 BM25 参数要自建检索服务，超出本项目范围 |
