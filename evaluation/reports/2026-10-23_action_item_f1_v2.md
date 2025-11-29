# 行动项抽取仪器 v2：两族共识 gold 与产品 F1（2026-10-23）

> 预注册 §55；严格定义下 gold 极稀疏，本报告是**仪器可行性**结果，不是稳定质量读数。

| 量 | 值 |
|---|---|
| 共识 gold（deepseek ∩ qwen） | 2 |
| 产品 pending_items | 17 |
| matched | 0 |
| precision | 0.0 |
| recall | 0.0 |
| F1 | None |

## 逐场

| case | deepseek | qwen | consensus | product | matched |
|---|---:|---:|---:|---:|---:|
| vcsum_0 | 0 | 0 | 0 | 1 | 0 |
| vcsum_108 | 0 | 0 | 0 | 0 | 0 |
| vcsum_110 | 0 | 5 | 0 | 3 | 0 |
| vcsum_120 | 0 | 0 | 0 | 1 | 0 |
| vcsum_125 | 0 | 0 | 0 | 5 | 0 |
| vcsum_146 | 0 | 0 | 0 | 2 | 0 |
| vcsum_150 | 0 | 0 | 0 | 0 | 0 |
| vcsum_165 | 0 | 0 | 0 | 0 | 0 |
| vcsum_172 | 0 | 5 | 0 | 3 | 0 |
| vcsum_174 | 2 | 4 | 2 | 2 | 0 |

## 限制

- 严格定义下 gold 极稀疏（10 场中多数 0 条），共识 gold n 很小，F1 不稳定
- 两族一致只降低 rubric 噪声，不替代人工金标
- VCSum 是论坛/访谈型会议，多数没有真正的行动项；抽取 F1 的样本效率低
