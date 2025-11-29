# 行动项槽位审计：26 场 VCSum 产品纪要（2026-10-23，零模型调用）

> 数据：`benchmarks/results/vcsum-test26-singlepass-r2-20261006` 的产品 `revised` 纪要 +
> `benchmarks/manifest_test_chunk6000.json` 官方转写；规则代理，非人工金标。

## 结果

| 量 | 读数 |
|---|---|
| 行动项/决策条目 | 74 |
| 有负责人的条目 | 61 |
| **owner fabricated** | **45 / 61 = 0.7377** |
| 有期限的条目 | 65 |
| **deadline field_misuse** | **19 / 65 = 0.2923** |
| deadline 在转写中不可定位 | 46 |
| evidence 不可定位（LCS<10） | 0 / 74 = 0.0 |

## 校验器将降级的条数（不覆盖原文，只不把无依据槽位写成事实）

- owner 降级：45 条
- deadline 降级：65 条
- evidence 降级：0 条

## 限制

- 转写来自 VCSum 官方 test26（人工整理转写），不是产品 ASR；owner/deadline 是规则代理，不是人工金标
- 只审计 pending_items/decisions 的槽位，不评行动项抽取召回（无 gold 行动项集）
- 校验器降级 ≠ 指标提升：它保证的是'不把无依据槽位当事实写进待办'
