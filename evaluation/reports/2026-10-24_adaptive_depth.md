# 自适应检索深度（多时间点问题）：A/B 负结果（2026-10-24）

> 预注册：`evaluation/LEDGER.md §58/§58b`。机制：问题含 ≥2 个时间表达或“时间线/时间节点”
> 线索时，把该轮最终 `rerank_top_k` 从 3 提到 5；不改提示词、不改工具调用。
> 结果：**未过预注册判定线，默认关。**

## 1. 主集 cross30（30 题，OFF 4 次 / ON 4 次均值）

| 指标 | OFF | ON | Δ |
|---|---:|---:|---:|
| recall-v2（主指标） | 89.22% | 90.95% | **+1.73pp**（目标 +3pp，未达） |
| context_precision | 61.21% | 64.60% | +3.39pp |
| faithfulness（内容级） | 90.18% | 88.32% | −1.86pp（非劣过线） |
| answer_relevancy | 99.17% | 96.67% | −2.50pp |

前 3 次 ON 均值 +2.73pp（差 0.27pp），按 §58b 追加第 4 次后回落到 +1.73pp —— 单次波动约 1pp，
说明“加宽 top_k”对时间节点类 recall 的收益没有稳定过线。

## 2. 泛化检查

| 集 | recall-v2 Δ | precision Δ | faith Δ | 判定 |
|---|---:|---:|---:|---|
| new_meeting 15 题（ON 4 vs OFF 3） | 0.00pp | — | **−3.42pp** | **未过中性线** |
| mixed100 子集 20 题（ON 2 vs OFF 1） | +8.33pp | −0.94pp | −0.04pp | 同向 |

new_meeting 上 faithfulness 下降 3.42pp（超过 2pp 非劣线），说明更宽的上下文对
“短事实题”有副作用，不能无条件放大检索深度。

## 3. 结论与处置

- **默认关**：`CHAT_ADAPTIVE_DEPTH_ENABLED` 未写入 `.env`；代码保留（可复现实验）。
- 机制本身是通用的（时间表达/时间线索），没有按 case 特判；但**没有稳定的 recall 收益**，
  且泛化集有负向，按 §58 判定为负结果，不产品化。
- RAG 指标线继续维持冻结：cross30 recall-v2 88.0（enrichment 后 89.22），**93% 目标不再追**。
- 现有更强证据仍来自 §53 的工具契约扩展（+5.60pp，跨集同向）。

## 4. 产物

`services/retrieval_policy.py`、`services/kb_service.py`（top_k contextvar）、
`tests/test_retrieval_policy.py`、`evaluation/analysis/adaptive_depth_ab.py`、
`evaluation/results/adaptive_depth_ab.json`。
