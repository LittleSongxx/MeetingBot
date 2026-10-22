# RAG 全链路质量闸（2026-10-22）

## 1. 当前质量基线

| 口径 | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| P0 6 题×3（fresh） | **97.22%** | **64.23%** | **90.75%** | **100%** |
| fresh cross30（1 轮） | 75.86% | 63.64% | 88.63% | 93.33% |

claim/citation 审计（fresh cross30）：9.4% 的数字断言不在任何上下文。

## 2. 新质量闸

### 评测侧（CI/发布前）
`evaluation/quality_gate.py`，零模型调用：

| scope | 指标 | 当前 | 阈值 |
|---|---|---:|---:|
| p0 | recall | 0.972 | ≥0.90 |
| p0 | precision | 0.642 | ≥0.58 |
| p0 | faithfulness | 0.907 | ≥0.85 |
| p0 | relevancy | 1.000 | ≥0.95 |
| cross30 | recall | 0.759 | ≥0.70 |
| cross30 | precision | 0.636 | ≥0.60 |
| cross30 | faithfulness | 0.886 | ≥0.85 |
| cross30 | relevancy | 0.933 | ≥0.90 |
| cross30 | claim not-in-any-context | 0.094 | ≤0.12 |

当前结果：**QUALITY GATE PASS**。任一指标跌破阈值会非零退出，阻止“指标退化但没人发现”。

### 运行时（生成后、落库前）
新增 `services/rag_quality.py`：
- 逐句提取带引用的数字/日期；
- 检查是否在**被引用上下文**的 source_text/quote 里；
- 输出 `number_not_in_cited / uncited_number / missing_citation` 提示；
- **只标注、不拦截**，写入 `error_message` 与页面 warning（与实体核查一致）；
- `RAG_QUALITY_GUARD` 默认 1，可关。

这层能兜住 ct06/ct08 一类“数字被绑到错误上下文”的问题，但只是确定性检查，不替代 judge/盲评。

## 3. 当前全链路质量保证结构

1. **检索**：dense+BM25 并发、RRF、rerank top3；
2. **生成**：单 Agent、工具循环、首轮强制检索、引用协议；
3. **生成后**：实体核查 + RAG 数字/引用核查（只标注）；
4. **评测**：P0 6×3、fresh cross30、claim audit、quality gate；
5. **发布门槛**：quality gate 通过才允许改默认。

## 4. 局限

- 9.4% 的数字缺口仍需 LLM/人工逐条判定；
- 质量闸阈值基于单会议语料，迁移到新语料必须重新校准；
- judge 指标不是人工金标，仍需独立盲评；
- 运行时核查只覆盖数字/日期/引用，不覆盖所有语义不支撑。

## 5. 下一步

- 用 `quality_gate.py` 作为所有 RAG 改动的硬门槛；
- 对 9.4% 缺口做 claim 校验/时间绑定修复；
- 扩 unseen-meeting holdout 后重设阈值。
