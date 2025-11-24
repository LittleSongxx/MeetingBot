# 延迟 / 成本基线（2026-10-20）

数据来源：`/observability/overview`（累计）+ `ai_call_log` 最近 4 小时聚合 +
`evaluation/benchmarks/latency_cost.py` 的 `/kb/search` 实测。
脚本只走 HTTP，不 import 产品内部，可重复运行。

## 1. 累计概览（截至 2026-10-20）

| 指标 | 读数 |
|---|---:|
| 调用总数 | 13,723 |
| 平均单次调用耗时 | 1,888.62 ms |
| 总 token | 44,460,986 |
| 记账成本 | ¥5.124651（**全部 13,723 次均标记 unpriced**） |
| 调用成功率 | 99.88% |

> 成本数字不能当真实账单：chat/embedding 的模型单价配置为 0，只有 rerank
> 有价格；`unpriced_call_count=13,723` 说明所有调用都未完整计价。
> 要得到真实成本，需要先在 AI 模型配置里填 input/output 单价。

## 2. 按调用类型（累计）

| call_type | 调用数 | 平均耗时 | 总 token | 记账成本 |
|---|---:|---:|---:|---:|
| CHAT | 6,050 | 2,445.33 ms | 30,353,359 | 0 |
| KB_QUERY_EMBED | 3,269 | 309.76 ms | 75,254 | 0 |
| KB_RERANK | 2,905 | 274.13 ms | 11,605,681 | ¥5.124651 |
| KB_QUERY_PLAN | 849 | 1,635.54 ms | 862,561 | 0 |
| CHAT_SUMMARY | 370 | **20,731.52 ms** | 1,331,574 | 0 |
| KB_CONTEXTUALIZE | 122 | 1,272.48 ms | 46,136 | 0 |
| KB_EMBEDDING | 93 | 446.38 ms | 116,849 | 0 |

## 3. 最近 4 小时（126 次调用，含最终实验）

| call_type | n | mean | p95 | input tokens | output tokens |
|---|---:|---:|---:|---:|---:|
| CHAT | 49 | 2,200.8 ms | 4,184 ms | 267,120 | 4,963 |
| KB_QUERY_EMBED | 37 | 340.4 ms | 637 ms | 0 | 0 |
| KB_RERANK | 37 | 253.5 ms | 321 ms | 0 | 0 |
| CHAT_SUMMARY | 3 | 14,405.0 ms | 15,396 ms | 5,151 | 2,355 |

检索链路（embed + rerank）约 **0.59s**，占一次问答的少数；主要延迟在
**CHAT（2.2–2.4s）** 和 **CHAT_SUMMARY（14–21s）**。

## 4. `/kb/search` 实测

`benchmarks/latency_cost.py --searches 10`：

| 指标 | 读数 |
|---|---:|
| n | 10 |
| mean | 650.3 ms |
| p95 | 798.1 ms |
| max | 939.1 ms |
| 平均命中 | 3 条 |

优化前同样 10 条 query 的临时实测：mean **629.5 ms**、p95 **820.0 ms**；
把 dense 与 BM25 从串行改为 `asyncio.gather` 并发后，临时实测 mean **543.3 ms**、
p95 **617.3 ms**（−13.7% / −24.7%）。样本小、受冷启动影响，**不宣称稳定收益**；
但两路召回互不依赖，并发在结构上只会减少墙钟。

## 5. 优化结论

- **已保留**：dense + BM25 并发召回；检索错误仍分别吞掉，不互相阻断。
- **未启用**：CRAG 过滤（多一次生成模型调用，实测/估算会抬高延迟与成本；
  且 holdout 召回未达标）。
- **未启用**：KB_QUERY_PLAN（平均 1.64s，A/B 无稳定质量收益）。
- **下一步成本杠杆**：
  1. 给 chat/embedding 配真实单价，否则无法算单次问答成本；
  2. CHAT_SUMMARY 是最大延迟项（20.7s），可评估 cheaper summary 模型/降低触发频率；
  3. 对长会话限制 summary token，而不是继续压缩检索链路。

## 6. Turn 级上下文过滤的延迟/成本（2026-10-20）

| 指标 | 读数 |
|---|---:|
| 调用类型 | `KB_TURN_FILTER` |
| 次数 | 60 |
| 平均耗时 | **1,471.77 ms** |
| 总 token | 59,577 |
| 失败 | 0 |
| 记账成本 | 0（unpriced） |

- 约 1.2 次/题，折合每题约 +1.8s；相比 per-search CRAG 过滤的 3.7s/次，
  延迟约为一半、调用数更少。
- 质量换取：holdout E precision 73.33%→90.38%、faithfulness 93.00%→97.80%，
  recall 97.06% 不变。
- 默认开启；若延迟敏感，可通过 `KB_TURN_FILTER_ENABLED=0` 关闭。
