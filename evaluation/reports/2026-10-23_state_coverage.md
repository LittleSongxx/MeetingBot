# 系列/状态覆盖 A/B：负结果（2026-10-23，S2）

> 预注册：`evaluation/LEDGER.md §50`。机制：state-aware CoveragePlan + `query_meeting_state` 观察 +
> 一条定向提示（不预检索、不替模型答）。开关 `CHAT_STATE_COVERAGE_ENABLED`（默认关）。

## 结果（cross30 30 题，每臂 2 次运行，均值）

| 指标 | OFF | ON | Δ |
|---|---:|---:|---:|
| **cross30 recall-v2** | **85.34%** | **80.17%** | **−5.17pp** |
| context_precision | 64.72% | 66.21% | +1.49pp |
| faithfulness（内容级） | 90.91% | 86.83% | −4.08pp |
| answer_relevancy | 100% | 98.33% | −1.67pp |
| `query_meeting_state` 调用次数 | 13.0 | 22.5 | +9.5 |
| state 上下文条数 | 16.0 | 24.0 | +8.0 |
| 有状态调用的题数 | 12.0 | 17.5 | +5.5 |

判定（§50）：recall-v2 +3pp ✗（实际 −5.17pp）；precision 非劣 ✓；faith 非劣 ✗（−4.08pp）；
状态调用不降 ✓；分母一致 ✓ → **FAIL → 默认关**。

## 解释（为什么提示更多状态查询反而更差）

1. 状态调用确实变多了（+9.5 次/30 题），但 **recall-v2 下降、faith 下降**：提示把模型的注意力
   从"检索证据"推向"查状态"，而缺失事实主要是 **工具契约里没有的字段**，不是"没查"：
   - m012 期望"对应待办 OPEN 状态"——`query_meeting_state(kind=decision)` 不返回关联待办；
   - m017 期望"supersedes_id 为空"——状态工具不暴露该字段；
   - m018 期望"DRAFT vs CONFIRMED"——需 `get_minutes(include_draft=true)` 且要核对草稿；
   - m015 期望"系列成员"——`list_facts(kind=recording)` 不返回系列成员。
2. 多一轮状态提示会**挤占轮次/token**，时间类事实（m022/m026）的检索反而变少。
3. 结论：**"提示模型去查"不是这类缺口的正确修法**；需要改的是**工具输出契约**（状态条目带
   OPEN/supersedes/确认态、录音/会议带系列成员）或**确定性的状态-证据绑定**，属于后续工作。

## 处置

- `CHAT_STATE_COVERAGE_ENABLED` **默认关**；代码保留（`services/coverage.py` 扩展 + 7 项单测），
  等工具契约补齐后再验证。
- 下一步（S3）：行动项抽取 + 规则校验 + 确认后闭环；S2 的负结果写入报告与 LEDGER，不再硬凑。

## 产物

`results/cross30_state{off,on}{1,2}_*`（answers/packet）、`results/state_coverage_ab.json`；
`services/coverage.py`、`tests/test_coverage.py`。
