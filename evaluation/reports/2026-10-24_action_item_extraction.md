# S3 行动项抽取：仲裁金标与证据优先抽取（2026-10-24，AI 非人工）

> 预注册：`LEDGER §59`（本轮补记）。目标仍是 `item F1 ≥ 0.70`。
> **结论：仍未达标**；但把"产品 0.174 / qwen 0.279"的旧读数，升级到一个**可复算的仲裁仪器**：
> 产品 F1 **0.263**、qwen 证据优先抽取（过滤后）**0.500**、deepseek 抽取 **0.400**。
> 这是方向性改善，不是达标；不把抽取步接入产品默认链路。

## 1. 仪器：跨族仲裁 gold

- 输入：deepseek 宽松抽取（29 条）+ qwen 严格抽取（14 条）作为两位标注员；
- 仲裁：deepseek 按"未来要执行的具体事项"定义合并去重、排除流程/观点/建议/过去事实，
  输出 10 场共 **21 条**（human_checked=false）；
- 产物：`gold/vcsum_action_items_adjudicated.json`。
- 限制：仍是 AI 仲裁，不是人工真值；只能作方向信号。

## 2. 结果（对同一 21 条 gold，textsim 条目匹配）

| 臂 | pred | matched | P | R | **F1** | 目标 0.70 |
|---|---:|---:|---:|---:|---:|---|
| 产品 `pending_items` | 17 | 5 | 0.294 | 0.238 | **0.263** | ❌ |
| qwen 证据优先抽取（过滤后） | 31 | 13 | 0.419 | 0.619 | **0.500** | ❌ |
| deepseek 证据优先抽取（同族上限参考） | 69 | 18 | 0.261 | 0.857 | 0.400 | ❌ |

- qwen v3 的**召回明显提升**（0.238→0.619），精度仍低（0.419）：大量"非行动项"被收进来；
- 过滤规则：evidence 必须能在转写定位（LCS≥10）+ content≥8 字 + 去重；
- deepseek 更"敢抽"（69 条、召回 0.857）但精度更差；同族评测有高估风险。

## 3. 判定与处置

- **目标 0.70 未达成**；不把独立抽取步接入产品默认链路（不拿未验证的抽取器改主链路）。
- 剩余差距是**精度**（流程/建议/泛泛目标被当成行动项）与**金标质量**；
  在 VCSum 上继续调 prompt 属于对噪声 gold 过拟合，停止。
- 真正干净的下一步仍是：**行动项密集语料 + 人工/双标 gold**（AMI/AMC-AID 风格或自录），
  以及"证据优先 + 槽位约束 + 人工抽检"的抽取流程。
- 槽位守卫（owner/deadline/evidence）继续有效，不受本轮影响。

## 4. 产物

`gold/vcsum_action_items_adjudicated.json`、`results/action_item_f1_v3.json`、
`results/action_item_f1_v3_filtered.json`、`results/action_extract_v4_deepseek.json`、
`analysis/action_v3.py`、`action_v3_filter.py`、`action_v4_deepseek.py`。
