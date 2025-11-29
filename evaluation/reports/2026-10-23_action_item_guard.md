# 行动项槽位校验：审计 + 闭环落地（2026-10-23，S3）

> 定位：这是**确定性规则修复/守卫**，不是模型机制 A/B（不声称因果指标提升）。
> 审计数据：`benchmarks/results/vcsum-test26-preservation-20261005` 的 26 场产品纪要
> （`revised.output.pending_items`）+ VCSum 官方 test26 转写；规则代理，非人工 gold。

## 1. 机制

新增 `services/action_validate.py`（零模型调用）：
- **owner**：负责人文本拆分（与/和/及/、）后，必须全部在转写里出现；机构名允许去后缀匹配；
  “未明确/待确认/全体/大家”等记 `missing`（不算 fabricated）；找不到 → `fabricated`；
- **deadline**：空/未提及 → missing；不是时间表达 → `misuse`（把行动写进时间槽，历史 defect）；
  时间表达按组件（今年/8月份/截止…）在转写里核对 → located/unlocated；
- **evidence**：引句与转写 LCS ≥10 字才算 located。
- 不通过时**只降级该槽位**（owner→空、deadline→空），不删条目、不改内容。

闭环接入 `action_item_service.sync_from_minutes`：确认纪要 → 生成待办前先校验；
有转写才校验，无转写（老任务/测试）fail-open。**不把无依据的负责人/期限写进待办。**

## 2. 26 场审计结果（50 条 pending_items）

| 量 | 读数 |
|---|---:|
| 条目 | 50 |
| 有具体负责人 | 6（其余 44 条“未提及/待确认/未明确”，不计入 fabricated） |
| **owner fabricated** | **0 / 6** |
| 有期限 | 6 |
| deadline located / unlocated / **misuse** | 4 / 2 / **0** |
| evidence located（LCS≥10） | **50 / 50** |
| 校验器降级 | owner 0、deadline 2 |

- 说明：这 26 场是论坛/访谈型公开会议，多数 pending_items 本身没有负责人（44/50），
  因此 owner 分母只有 6 条；历史轴4“owner fabricated 9/143=6.3%”来自另一批数据，不能直接对比。
- 真实缺陷是 **2 条 deadline 在转写里不可定位**：校验器会在写待办前把它降级为空（不再当事实）。

## 3. 闭环

```
纪要确认 → sync_from_minutes → 每条 pending_item 走 validate_item（有转写时）
        → 合法 owner/deadline 写入 action_item；无依据槽位置空
        → action_item 进入状态查询（query_meeting_state）与系列待办视图
```
单元测试：`tests/test_action_validate.py` 6 项；产品全量 **666 项** 通过。

## 4. 限制与未完成

- **item F1 未测**：没有“行动项抽取”人工 gold；现有 pending_items 是产品抽取结果，
  用它当 gold 会循环。要报 item F1 需要先建 10–20 场人工/AI-draft 金标（下一步）。
- 审计是规则代理：负责人分词、机构名匹配、时间组件核验都有边界（如“发言人1”这类角色不是名字）。
- 只校验**槽位**，不提升抽取召回；44/50 无负责人本身是纪要抽取步的问题（S4 完整性/抽取步）。

## 5. 产物

`services/action_validate.py`、`services/action_item_service.py`、`tests/test_action_validate.py`、
`evaluation/analysis/action_item_audit.py`、`evaluation/results/action_item_audit.json`、
`evaluation/reports/2026-10-23_action_item_audit.md`。
