# 泛化 / 过拟合自查（2026-10-20）

## 1. 结论先行

- 没有发现产品默认配置被“单题 badcase 补丁”绑死；所有高风险特判都保持默认关闭。
- 发现并清理了若干**语料特定示例/注释**（不改变工具语义，只降低 prompt 偏置）。
- 评估集与 gold 仍是 AI 起草/复核，`human_checked=false`；holdout C/D 没有独立事实复核，
  不能当“人工金标”或强泛化证明。
- 这轮之后**冻结默认**；下一步只做架构级改动（turn 级上下文过滤/更强检索模型），
  不再针对单题改 prompt。

## 2. 产品改动审计

| 改动 | 性质 | 证据 | 默认 | 过拟合判断 |
|---|---|---|---|---|
| series_id 存在性校验 | 工具语义（id 命名空间） | 4 条系列题从全错到正确 | 开 | 通用，保留 |
| `include_proposed` | 读取能力（候选仍标 PROPOSED） | cs02 能答候选 | 关（按需） | 通用，保留 |
| `get_minutes(include_draft)` | 读取能力（草稿标 DRAFT） | cs06 正确给草稿结论 | 关（按需） | 通用，保留 |
| 状态模糊匹配 | 文本规范化 | “移动端附件下载修复”类改写命中 | 开 | 通用，保留 |
| 偏好 `owner_name` | 字段补全 | cs04 归属正确 | 开 | 通用，保留 |
| 完整清单规则 | 通用回答原则 | 9 条 partial 中多数改善，cm02 仍漏 | 开 | 通用原则，保留 |
| top_k=2 | 检索截断 | precision +2.1，recall −14.8 | 关 | 拒绝 |
| Contextual Retrieval | 索引增强 | recall −8.0，faithfulness −5.5 | 关 | 拒绝，已重建基线索引 |
| rerank score floor | 重排过滤 | dev +4.29 precision；holdout B −3.28 | 关（0） | 拒绝 |
| CRAG binary/3way | 检索评估器 | 检索级 holdout D 通过；产品 per-search recall −6.2、延迟 3.7s/次 | 关 | 代码保留为实验开关，默认关 |
| BM25-only 降级 | 故障韧性 | 嵌入供应商故障时不 500 | 开 | 通用，保留 |
| dense/BM25 并发 | 延迟 | mean 629→543ms（小样本） | 开 | 通用，保留 |

## 3. 发现的偏置/过拟合风险

1. **语料特定示例**：工具描述/注释里出现“报表导出”“发布范围”“移动端附件下载修复”
   等具体案例。已改为通用表述；行为规则本来就不是按这些词写死的。
2. **历史 qXX 注释**：大量注释记录某次回归的 q 编号，属于开发历史，不影响运行；
   但会诱导后续维护者按单题加规则。审计报告记录，不再新增。
3. **gold 来源**：cross30 的 4 条 required 标签经 AI 复核修改，holdout A 事实经独立
   Agent 复核，holdout C/D 未做独立事实复核；所有 `human_checked=false`。
4. **holdout 复用**：A/B/C/D 各自只用于一次验证，但都是同一会议语料、AI 起草，
   题目与开发集语义接近；只能当 sanity check，不能当独立外部测试集。
5. **产品级 CRAG**：离线检索级有效，但 per-search 实现伤害 recall；已否决，没有
   为了让指标好看而默认打开。

## 4. 与行业主流对齐

- 检索：dense + BM25 混合、cross-encoder rerank、RRF、top-k 漏斗；与主流 RAG 一致。
- 评测：RAGAS 式 context recall/precision/faithfulness/relevance；dev→holdout 分离；
  预注册门槛；独立 judge（DeepSeek）与产品模型（qwen）不同族。
- 增强：Contextual Retrieval 做过对照并拒绝；CRAG 式过滤做了检索级/产品级两段验证；
  没有引入未经验证的自定义规则。
- 工程：prompt prefix cache、并行召回、失败降级、成本/延迟观测；属于常规生产实践。

## 5. 当前质量与默认

- 默认：top_k=3、contextual off、rerank floor=0、planning off、relevance filter off。
- cross30（prompt 去案例化后）：recall 76.79%、precision 68.87%、faithfulness 81.86%、
  relevance 100%（单轮；与去案例化前 80.36/67.27/79.10 相比互有升降，在噪声范围内，
  不声称提升）。
- 最终答案独立复核仍是 21 correct / 9 partial / 0 wrong。
- 测试：后端 621（615 通过/6 跳过）、评测 330、前端 65。

## 6. 下一步（不再单题补丁）

1. 把 CRAG 3-way 从“每次 search 内过滤”改成“一个 turn 合并最终上下文后过滤一次”；
   这是架构级改动，需要新 holdout E 和完整答案生成验证。
2. 或者换更强/更便宜的检索评估器（小模型/规则预筛），先量延迟与 token。
3. 给 chat/embedding 配真实单价，建立单次问答成本指标。
4. 若要对外声称泛化，必须补人工签名或第三方独立集；当前只能标“AI 复核/自测”。
