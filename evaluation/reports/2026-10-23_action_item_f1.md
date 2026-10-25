# 行动项抽取 F1：产品 pending_items vs 10 场 AI-draft gold（2026-10-23）

> 预注册 `evaluation/LEDGER.md §54`；gold 为 deepseek AI-draft（human_checked=false），
> 只作方向信号；内容是 rule/proxy 匹配，不是人工语义判定。

## 结果

| 量 | 值 |
|---|---|
| gold items | 29 |
| product items | 17 |
| matched | 4 |
| precision | 0.235 |
| recall | 0.138 |
| **F1（目标 ≥0.70）** | **0.174** |
| 判定 | FAIL |
| owner 一致（有 owner 的匹配对） | 0/0 |
| deadline 一致 | 0/0 |

## 逐场

| case | gold | product | matched |
|---|---:|---:|---:|
| vcsum_0 | 5 | 1 | 1 |
| vcsum_108 | 2 | 0 | 0 |
| vcsum_110 | 7 | 3 | 3 |
| vcsum_120 | 0 | 1 | 0 |
| vcsum_125 | 4 | 5 | 0 |
| vcsum_146 | 6 | 2 | 0 |
| vcsum_150 | 0 | 0 | 0 |
| vcsum_165 | 0 | 0 | 0 |
| vcsum_172 | 0 | 3 | 0 |
| vcsum_174 | 5 | 2 | 0 |

## 限制

- gold 为 AI-draft（deepseek，human_checked=false），只作方向信号
- 内容匹配用 textsim.is_same_item（产品/评测共用的条目身份判据），可能漏近义改写
- 产品 pending_items 来自 26 场 preservation 运行；10 场是其中固定子集
