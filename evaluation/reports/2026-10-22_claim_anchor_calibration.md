# claim 级人工锚点校准（独立子 Agent 代审，2026-10-22）

> 预注册：`evaluation/LEDGER.md §44`。两位独立子 Agent 盲标 100 条 claim，
> 只给 question/claim/cited_context；**不是人工签名**（human_checked=false）。

## 结果

| 量 | 值 |
|---|---|
| 有效配对 | 100 / 100 |
| 原始一致率 | 0.870 |
| Cohen κ | 0.438 |
| Krippendorff α（nominal） | 0.426 |
| α 目标（≥0.7） | FAIL |
| A 分布 | {'supported': 93, 'uncertain': 3, 'unsupported': 4} |
| B 分布 | {'supported': 82, 'unsupported': 8, 'uncertain': 10} |
| consensus（A=B 且非 uncertain） | 86 |

## 与 LLM judge 的对照（consensus 样本）

- 匹配到 judge claim：72 条；未匹配：14 条
- 一致率：0.944
- 混淆：{'supported->judge_unsupported': 3, 'supported->judge_supported': 65, 'unsupported->judge_unsupported': 3, 'unsupported->judge_supported': 1}

## 判定（按 §44）

- α < 0.70 → 登记 claim 级裁判不可靠，faithfulness 继续只作方向信号
- 子 Agent 代审不能冒充人工金标；正式金标仍需真人双标。

