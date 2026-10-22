# 通用证据边界提示实验（2026-10-22）

## 目的

不是按题写规则，而是在 system prompt 加一组通用约束：

1. 具体事实/数字/日期/引语必须能在引用 [n] 原文中找到；
2. 引语逐字来自工具结果；
3. 时间点/序号/复述顺序不等于时间先后；
4. 材料冲突保留原文；
5. 区分材料原文与推断。

默认 `EVIDENCE_CONSTRAINT_ENABLED=0`，仅实验开启。

## cross30 结果（30 题，同一配置、仅 prompt 开关不同）

| 指标 | 关闭（默认） | 开启 |
|---|---:|---:|
| context_recall | 81.7% | 81.0% |
| context_precision | **74.6%** | 66.4% |
| faithfulness | 78.6% | **84.8%** |
| answer_relevancy | 96.7% | **100%** |

结论：
- faithfulness +6.2pp、relevancy +3.3pp；
- 但 precision −8.2pp、recall 基本持平；
- 是“更谨慎但更保守”的 tradeoff，不是双赢；
- 因此**默认关闭**，不把实验性 prompt 当已上线优化。

## 解读

提示让模型更少写无依据细节，所以 faith 上升；
但它也更倾向补查/保守，导致上下文 precision 下降。
下一步应把“证据边界”做成**结构化校验/生成约束**，而不是继续加长 prompt。
