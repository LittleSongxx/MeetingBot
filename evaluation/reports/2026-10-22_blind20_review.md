# 100 题抽样盲评（20 条，无 expected facts）

## 结果

| verdict | 数量 |
|---|---:|
| correct | **14** |
| partial | **6** |
| wrong | **0** |

盲评输入只给 question、answer、retrieved_contexts，不给 expected_facts/gold；
审核者另有独立材料可读，但未见 gold 期望答案。

## 主要问题

- m011：核心决策正确，但“李月在会上的发言”引语/发言人归属不在 retrieved context；
- m019：引用了 context 中不存在的 notice 原文；
- m001/m060：9:00 完成公告 vs 10:00 发布公告，时间/对象被过度解读；
- m025：“改造在前、灰度在后”证据不足，把复述顺序当时间顺序；
- m030：“回滚无需动数据”与转写字面“回滚步需要动数据”相反；
- 部分引用编号与 context 顺序不完全对应，转写说话人标签有噪声。

## 结论

- 100 题 judge-only 的 91.0% recall / 89.2% faith 不代表 20 条盲评全对；
- 答案层的主要剩余问题不是“查不到”，而是：
  1. 引语/字段超出被引上下文（citation attribution）；
  2. 时间/对象过度推断；
  3. 单条转写与答案矛盾时没有保留原文。
- 这支持新增 `rag_quality` 的 quote-not-in-cited 检查，并继续做矛盾/推断边界。

## 产物

- 输入：`evaluation/reports/blind20_answers.csv`
- 结果：`evaluation/reports/blind20_review.json`
- 评审：subagent-blind，2026-10-22
