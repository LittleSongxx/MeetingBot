# RAG/问答链路 TTFT 与首 token 延迟（2026-10-23）

> 结论：**TTFT 不长，但也不是"检索完就出字"**。10 题实测：首个 SSE 事件 **21ms**，
> **TTFT p50 3.16s / p95 5.83s**，端到端 **p50 5.21s / p95 8.49s**。
> 检索本身只占 ~0.6–0.8s；主要时间花在 **Agent 的 2–4 轮模型调用**（首轮判断要不要调工具 + 工具轮 + 生成首 token）。

## 1. 口径

- 客户端先 `POST /chat/session`（每题新建），再 `POST /chat/stream` 读 SSE；
- `t0` = 发请求；记录：
  - `first_event_ms`：第一个 SSE 事件（`meta`，不含生成）；
  - `TTFT`：第一个 `event: token`（含检索、工具、模型首 token）；
  - `done_ms`：收到 `event: done`；
- 10 题：`qa_new_meeting_15` 前 10 题；当前开关：引用绑定 on、工具契约扩展 on、
  query_plan off、rerank_top_k=3；**单次运行、n=10，只作方向信号**。

## 2. 结果

| 量 | p50 | p95 | mean | max |
|---|---:|---:|---:|---:|
| **TTFT** | **3,162ms** | **5,826ms** | 3,768ms | 6,361ms |
| 首个 SSE 事件（meta） | **21ms** | 25ms | — | — |
| 端到端 done | **5,211ms** | **8,491ms** | 6,327ms | 8,677ms |
| 图节点 total（DB `timing`） | 6,690ms | — | — | 8,665ms |
| 其中 agent 节点 | 6,044ms | — | — | 8,061ms |
| 其中 tools 节点 | 613ms | — | — | 836ms |

逐题 TTFT：2.08s、2.67s、2.81s、2.95s、3.16s、3.22s、4.24s、4.36s、5.83s、6.36s。
平均工具调用 1.6 次/题；每题 2–4 次 CHAT 模型调用。

## 3. 时间花在哪里

单题典型链路（从日志/节点耗时拆）：

```
连接 + meta             ~20ms
检索（embed 0.27s + rerank 0.26s + 混合召回） ~0.55–0.75s
第 1 轮 CHAT（判断/发起工具调用）           ~1.1–1.9s
工具执行（search/list_facts/get_minutes）    ~0.02–0.84s
第 2 轮 CHAT 生成 → 第一个 token            剩余时间（约 0.5–2s）
其余轮次/长生成 → done                      再 1–5s
```

- **检索不是瓶颈**：embed p50 0.27s、rerank p50 0.26s；历史 `/kb/search` 实测
  mean 650ms / p95 798ms；一次检索往返 p95 也 <1s。
- TTFT 由 **Agent 回合结构**决定：首轮要求先调工具（`tool_choice=required` 的可用条件下），
  且首轮文本先缓冲、确认调工具才补发；所以用户看到第一个字之前，
  至少已经走完"第 1 次模型调用 + 工具 + 第 2 次模型调用首 token"。
- 首 token 之后到 done 还有约 2–3s 的流式生成，因此 e2e 比 TTFT 多 2–3s。

## 4. 成本（估算单价，非账单）

同 10 题按新配置的估算单价（qwen3.8-max 11715/35152 微元每 1k）：

| 量 | 读数 |
|---|---:|
| 单题成本 mean / p50 / max | **¥0.150 / ¥0.123 / ¥0.234** |
| 单题 CHAT 调用数 | 2–4 次 |
| 单题模型耗时（所有 CHAT+检索） | p50 6,542ms |

成本主要在 CHAT（每轮 4k–6k token），检索嵌入/重排约 ¥0.002/题。

## 5. 解读

- **TTFT 3–6s 不是 RAG 检索慢，而是"Agent 必须先检索再回答"的回合成本**；
  首个 SSE 事件 21ms 说明连接/服务端排队没问题。
- 如果要把 TTFT 压到 1s 级，只有三条路（都要质量验证）：
  1. 关闭/放宽"首轮必须调工具"，允许直接回答简单事实题（有 unsupported 风险）；
  2. 首轮文本不缓冲、边流式边等工具（会先吐未检索内容，历史上正是被修掉的缺陷）；
  3. 预检索/并行检索（把工具轮压到 1 次，但会改变工具轨迹与成本）。
- 当前不为了 TTFT 牺牲"先检索再回答"的质量约束；此报告只提供基线，
  后续任何 TTFT 优化必须先预注册并做质量非劣检验。

## 6. 产物

`evaluation/benchmarks/ttft.py`、`evaluation/results/ttft_report.json`
（含每题 message_id、TTFT、done、工具数；可与 `ai_call_log`/`moderation_json.timing` 对账）。
