# 轨迹 pass^k 可评化（2026-10-23，S1b）

> 预注册：`evaluation/LEDGER.md §49b`。27 题轨迹集，同配置 3 次独立 trial（新 session），
> 零模型判定（`regression/trajectory.py` 只读产物）。

## 结果（n=27，k=3，81 trial）

| 量 | 读数 | 口径 |
|---|---:|---|
| **pass^3** | **85.19%（23/27）**，CP 66.3–95.8% | 同一题 3 次 trial 均满足 required 覆盖 + must_cite |
| tool set F1（macro，首 trial） | 0.706（P 0.639 / R 0.852） | 最小 required 集合；多调 acceptable 扣 precision |
| first-tool strict | 14/27 = 51.85% | 首轮工具 ∈ required |
| required 覆盖 | 23/27 = 85.19% | required 全被覆盖 |
| must_cite | 25/25 = 100% | must_cite 题的引用非空 |
| forbidden 命中 | 0/81 | 禁止工具 |
| 缺失题 | 0 | 3 份产物均 27/27 匹配 |

- 与历史单 trial 读数（F1 0.7309 / strict 14/27 / coverage 24/27）一致；**pass^3 与单 trial 差异不大**
  （85.19% vs 单 trial 参考 85.19%），说明这 27 题在当前配置下不存在"偶尔成功"的明显不稳定性。
- 不足 k 次的题：0；缺失：0 → 报告可引用（此前 pass^k 状态为 `not_evaluable`）。

## 限制

- 金标为代理起草启发式（`human_checked=false`）；工具 F1 是"最小集合"口径，多调但不影响证据的调用会扣分
  （历史实验已证：按数量硬压会伤 faithfulness 88.63→73.53–83.69，因此本项目不把 F1 当唯一目标）。
- pass^3 只衡量"工具覆盖 + 必引"，不评答案正确性；单裁判/单数据集边界不变。

## 产物

`evaluation/results/trajectory_passk_trial{1,2,3}_answers.json`、
`evaluation/results/trajectory_passk27_report.json`。
