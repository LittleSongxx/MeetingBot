# 工具 F1 为什么低，以及如何修复（2026-10-22）

## 1. 先纠正一个直觉

只有 5 个工具，不代表选择容易：
- 每题可能需要 1–3 个工具的组合；
- F1 按**最小 required 集合**算，多调一个 acceptable 工具就扣 precision；
- 小样本 27 题，一两题就能让 F1 动 5–10pp。

## 2. 当前 fresh trajectory 拆解

| 情况 | 数量 |
|---|---:|
| 工具集完全匹配 | 12 |
| 只多调 | 12 |
| 既漏又多余 | 3 |
| 只漏调 | 0 |

- 真正漏 required 的只有 3 题（t13、t15、t26）
- micro precision 0.55、recall 0.88、F1 0.677
- macro F1 0.731
- 首轮 strict 14/27、acceptable 22/27、coverage 24/27

**F1 低的主因是 over-call，不是选错。**

## 3. over-call 里哪些是真多余

用 citation kind 做代理审计（`evaluation/analysis/tool_utility.py`）：

- 18 次 extra call
- **7 次完全没有对应 citation kind**（多余且无贡献）
- 其余 11 次多调工具确实贡献了引用（例如 list_facts 的 fact 被引用）

例子：
- t06/t07/t10/t17：多调 `query_meeting_state`，引用全是 chunk → 无贡献
- t18/t20：多调 `list_facts`，引用全是 chunk → 无贡献
- t19/t21：多调 `list_facts`，引用包含 fact → 有贡献
- t24：多调 `search_knowledge`，引用含 chunk+state → 有贡献

结论：over-call 是混合的，不能一律压掉。压掉有贡献的 extra call 会伤答案。

## 4. 直接压 over-call 的实验结果

| 方案 | tool F1 | strict | acceptable | coverage | cross30 faithfulness |
|---|---:|---:|---:|---:|---:|
| router off | 0.7309 | 14/27 | 22/27 | 24/27 | **88.63%** |
| 全量路由 | **0.8519** | **18/27** | **23/27** | **26/27** | 83.06% |
| 选择性路由 | 0.8025 | 17/27 | 21/27 | 24/27 | **73.53%** |
| 仅 hint | 0.7963 | 17/27 | 21/27 | 24/27 | 80.24% |
| 仅 budget | 0.7654 | 14/27 | 22/27 | 25/27 | 83.69% |

- 全部方案都改善了一部分 F1，但都以 **faithfulness 下降** 为代价；
- 说明“减少工具调用”本身不是无损优化，模型多查往往是在补证据。
- 因此不能把 F1 作为唯一目标，也不能靠全局 hint/budget 硬压。

## 5. 根因

1. **F1 口径**：最小 required 集合，不奖励“多查但有用”的证据。
2. **意图边界**：cross/aggregation/state 的措辞重叠，导致选错或先调错工具。
3. **探索性调用**：模型不确定时会多调一个工具确认；有些贡献 citation，有些没有。
4. **小样本**：27 题，per-intent 4–10 题，方差大。

## 6. 修复方向（按优先级）

### P0 换指标
不要只看 tool F1，至少同时报：
- required coverage / recall
- first-tool strict / acceptable
- over-call rate
- **extra-call utility**（是否贡献引用）
- 答案 faithfulness、延迟、token
- pass^k（多轮可靠性）

### P1 只压“无贡献 over-call”
- 用 `tool_utility.py` 标注哪些 extra call 没有 citation 贡献；
- 在生成后/下一轮做 stop 策略：如果一个工具已经返回、且结果没有被引用/没有补齐槽位，
  下一轮不再重复调同类工具；
- 只对这类调用做抑制，不动有贡献的 extra call。

### P2 意图与首工具
- 只对 cross_meeting/state 做首工具提示（选择性路由已验证有效但伤 faith）；
- 先只做 **首工具强制**（tool_choice），不加全局 hint/预算，观察是否只改善 strict 不伤答案；
- 需要新 holdout 和独立盲评。

### P3 工具结果前置判断
- 在工具结果里返回 `coverage/slot_status`，模型看到“已足够”就停；
- 而不是靠 intent 规则猜。

### P4 不要做的事
- 不用全局 prompt 逼模型“少调工具”；
- 不把 F1 单指标当答案质量；
- 不用工具预算硬压有贡献的 extra call。

## 7. 当前状态

- `INTENT_ROUTER_ENABLED=0`，所有实验开关默认关闭；
- 默认仍是 rerank on、top_k=3；
- 工具 F1 0.7309 是当前稳定基线；它衡量的是“最小工具集精确匹配”，
  不能直接等价为“5 个工具选不对”。
