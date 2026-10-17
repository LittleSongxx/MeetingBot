# RAG 全链路参数（标准化配置，2026-10-17）

这份表回答一个问题：**这条 RAG 链路的每个可调参数现在取什么、为什么取这个、改了要付什么代价。**
取值的依据是公开做法（Qdrant 官方索引/过滤文档、Azure RAG 检索阶段指南、DashScope 模型文档）
与本语料上的实测（`evaluation/LEDGER.md` 的「标准化链路」一节），**不做学术式消融**。

## 一、链路与参数

```
转写/纪要 ──切块──▶ 切片（300 字窗口，2 行重叠）
    ├─ 稠密：qwen3.7-text-embedding（1024 维）
    ├─ 稀疏：字符 bigram 次线性 TF + Qdrant 服务端 IDF（= BM25 词法口径）
    ▼
Qdrant 混合检索（prefetch 24 × 两路，服务端 RRF 融合）
    ▼
qwen3.7-text-rerank（专用 cross-encoder 类，24 候选打分）
    ▼
截断 top-3 ──▶ 生成模型
```

| 参数 | 当前值 | 位置 | 改它要付什么 |
|---|---|---|---|
| 窗口字数 | **300**（`KB_CHUNK_TARGET_CHARS`） | `services/kb_chunking.py` | 换值 = 切片正文变 → 内容指纹变 → 下次索引自动重建（要重嵌） |
| 重叠 | 2 行（约 15%） | 同上（常量） | 同上；重叠太少会切断跨句事实，太多会重复召回 |
| 切分方式 | 句读边界（`。！？；`）+ 单句上限 800 字硬切 | 同上 | 会议转写是连续口语，没有章节结构；按句读切比固定窗口稳定 |
| 稠密模型 / 维度 | `qwen3.7-text-embedding` / **1024**（`EMBEDDING_MODEL`、`EMBEDDING_DIM`） | `.env` → bootstrap 同步到 `ai_model_config` | 换模型/维度 = 换向量空间，必须重建（指纹含模型名与维度，自动触发） |
| 稀疏臂 | 字符 bigram 次线性 TF + **IDF 由 Qdrant 按语料统计** | `services/kb_sparse.py`、`common/qdrant.py` | `modifier=idf` 是**建库时**的声明；换它要换 collection 名重建（代码会 fail-fast 而不是静默退化） |
| 混合与融合 | 两路各 prefetch 24，服务端 **RRF**（`query.fusion=rrf`） | `common/qdrant.py` | 候选深度影响召回；RRF 免调权重 |
| 重排 | `qwen3.7-text-rerank`（`RERANK_MODEL`；`RERANK_BASE_URL` 可指专属端点） | `services/model_factory.rerank_documents` | 每次检索一次 HTTP；实测 **平均 476ms、最大 2.35s**、约 5.4k token/次（24 候选） |
| 重排开关 | 默认**开**（`KB_RERANK_ENABLED`） | `services/kb_service.py` | 关掉省延迟，但 context_precision 会掉 |
| 截断 | **top-3**（`RERANK_TOP_K`） | 同上 | k 越大召回越宽、单条精度越低（见第二节四臂标定） |
| 工具默认条数 | **5**（模型可自己要到 20，重排后仍截到 top-3） | `services/kb_tools.py` | 模型要更多也只会拿到 3 条；要更大就调 `RERANK_TOP_K` |

## 二、本语料实测（27 条人工标注题）

**判定只用一个族**：DeepSeek（`deepseek-flash`、thinking=off、label `f` 的标定家族）。
qwen 不再当裁判；旧读数只作历史对照，不与新读数相减。

标准口径（RAGAS：contexts = **检索到的全部切片**，不是"答案实际引用的那些"）：

| 配置 | context_precision | context_recall | faithfulness 内容级 | answer relevancy | recall@k / MRR / nDCG | 上下文数 |
|---|---|---|---|---|---|---|
| 500×8（历史） | 33.5% | 93.5% | 100% | 100% | 0.919 / 0.827 / 0.854 | — |
| 500×5（历史基线） | 53.0%（71/134） | 95.2% | 94.5% | 96.3% | 0.956 / 0.846 / 0.879 | 134 |
| 500×5（标定复跑） | 54.0%（74/137） | 95.2% | 96.4% | 100% | 0.956 / 0.821 / 0.867 | 137 |
| 500×3 | 65.1%（56/86） | 95.2% | 96.0% | 100% | 0.956 / 0.846 / 0.880 | 86 |
| 300×5 | 58.5%（83/142） | 95.2% | 96.9% | 100% | 0.926 / 0.815 / 0.835 | 142 |
| **300×3（当前）** | **81.0%（68/84）** | **95.2%** | **94.3%** | **96.3%** | **0.944 / 0.870 / 0.886** | **84** |

**读法**：`context_precision` 的短板不是"检索不到"（recall 一直 95%），而是
"一次问一两个事实，却交回若干段同主题、答不了问题的窗口"。300×3 的 84 条上下文里
68 条有用；500×5 的 137 条里 74 条有用——**无用条数从 63 降到 16**，precision 的主要来源
是分母变小，不是分子变大。两个因素是交互的：500 字下 k=5→3 是 +11.1pp，
300 字下 k=5→3 是 +22.5pp。

**代价必须一起报**：300×3 的零调用 `recall@k` 是 94.4%，比 500×5 的 95.6% 低 1.2pp
（MRR/nDCG 反而更高）；判定式 recall 同为 95.2%，q26 仍是已知缺口（只存在于草稿/转写，
且模型这一轮没检索转写）。n=27 下 1.2pp 小于一个 case 的粒度，判为方向性持平，
不作"召回无损"的强表述。历史 qwen 读数（51.5%）保留作对照，不与本表相减。

**Contextual Retrieval（已实现、默认关）**：索引时给每块加"规则头 + 一句 LLM 生成的主题"
再嵌入，引用正文不变（`kb_chunk.embed_text` 与 `text` 分开）。300×3 上实测
precision 77.3%（68/88）vs 关时的 81.0%（68/84）：有用条数一样，分母多了 4 条；
MRR 0.870→0.889、nDCG 0.886→0.893、answer relevancy 96.3%→100%，但主指标未过成功线。
因此 `KB_CONTEXTUAL_ENABLED=0`；要开就按 LEDGER 任务 3 的预注册重跑。

**资料覆盖规则（任务 4，2026-10-17）**：系统提示词里加了一条**代码持有**的规则
（`chat_service.SOURCE_COVERAGE_PROTOCOL`）：`list_facts` 的 `source_coverage`
显示还有未确认纪要/转写录音时，"确认纪要里没有"不等于"没发生过"；只有明确问
"有哪些行动项/待办"且清单不完整时，才必须再检索转写；多段录音各检索一次；
"上午/下午"判不出来时两段都查、如实说明。实测 q26 从 **0/3 → 3/3**
（检索集覆盖 task 6+7，答案逐条列出张涛/陈磊/刘颖并说明不可区分），代价是
`context_precision` 81.0%→75.3%（回归门 76.2% 差 0.9pp，按预注册不判"非劣"），
recall 95.2%→100%，CHAT token +19.5%。**这是有记录的产品取舍，不是"精度提升"**。

## 三、改参数的正确顺序

1. 改 `.env`（块大小/模型/k）→ 重启 backend（bootstrap 会把模型配置同步进库）；
2. 触发重建索引（三个来源各 POST 一次 `/kb/index`）——**内容指纹覆盖了正文、payload 结构、
   嵌入模型与 collection**，所以改这些都会自动重建；**稀疏 IDF 与 collection 名绑死**，
   换它要改 `QDRANT_COLLECTION`；
3. 重建后用 `make smoke` 看零调用检索指标（recall@k/MRR/nDCG），再决定要不要跑判定。

## 四、明确**没有**做的（避免被当成遗漏）

| 没做 | 为什么 |
|---|---|
| 语义切分 / 父子块 / Contextual Retrieval | 都要额外机制（父块表、索引时逐块 LLM 调用）；本语料 128 个片段、61 个切片，收益与成本不成比例。**本轮先做 Contextual Retrieval 的 LLM 版并量收益**（LEDGER 任务 3）；语义切分与父子块仍不做，除非 Contextual Retrieval 的收益证明值得再上一层 |
| 多向量 / ColBERT | 需要新索引与 GPU 侧能力；当前混合+重排已覆盖"精确串 + 语义"两类召回 |
| k 的自适应（按问题类型给不同 k） | 需要先有可靠的意图路由；该路由的逐类 F1 目前还有两类不可用（见 LEDGER） |
| 修改 BM25 的 k1/b | Qdrant 的稀疏 IDF 只暴露 modifier；完整 BM25 参数要自建检索服务，超出本项目范围 |
| 重叠窗口去重（**已实现、默认关**） | 300×3 的零调用反事实：删掉的 10 条全部被判有用，precision 81.0%→78.4%、recall 不变。相邻窗口不是重复噪声，而是各含答案的有用上下文。开关 `KB_DEDUPE_ENABLED=1` 可开，阈值见 `.env.example` |
| Contextual Retrieval（**已实现、默认关**） | 索引时逐块一次 LLM 调用（`KB_CONTEXTUALIZE`，61 次/0.023M token）；300×3 上 precision 77.3% vs 81.0%，主指标未过成功线。机制保留，`KB_CONTEXTUAL_ENABLED=1` 可开；语义切分与父子块仍不做 |
