# S2 备选：工具输出契约扩展（系列/状态元数据，2026-10-23）

> 预注册：`evaluation/LEDGER.md §53/§53b/§53c`。**不改提示词、不按 case 特判**：只把
> "任何会议都成立"的结构化元数据补进工具返回与引用快照。
> 结果：**PASS → 默认开** `CHAT_TOOL_ENRICHMENT_ENABLED=1`。

## 1. 机制

- `list_facts(kind=meeting/recording)` facts 增加
  `series_id / series_name / series_meeting_count / series_meeting_titles`；
- `query_meeting_state` 记忆条目增加 `minutes_status`（来源纪要 DRAFT/CONFIRMED）、
  `supersedes_id`、系列范围（count/titles）；
- `citations` 引用快照把上述字段写进 `source_text`（判定者/用户可见）。
- 全部由 `services/tool_metadata.py` 提供，开关 `CHAT_TOOL_ENRICHMENT_ENABLED`；
  不改工具调用次数、不改答案结构。

## 2. 主集：cross30 30 题（4 次运行/臂，均值）

| 指标 | OFF | ON | Δ |
|---|---:|---:|---:|
| **recall-v2（主指标）** | **83.62%** | **89.22%** | **+5.60pp** |
| context_precision | 62.84% | 61.21% | −1.63pp（非劣过线） |
| faithfulness（内容级） | 89.21% | 90.18% | +0.97pp |
| answer_relevancy | 98.33% | 99.17% | +0.84pp |

判定（§53）：主指标 +5.60pp ✓；precision/faith 非劣 ✓；分母一致（58/58）✓ → **PASS**。
前两次运行 precision 曾 −2.84pp（超线 0.84pp），按 §53b 追加重复后收敛到 −1.63pp
（与 S1 的跨运行波动经验一致）。

## 3. 泛化检查（防过拟合）

| 集 | recall-v2 Δ | precision Δ | faith Δ | 判定 |
|---|---:|---:|---:|---|
| new_meeting 15 题（3 次/臂） | 0.00pp | +1.22pp | +0.48pp | **中性/不降** |
| mixed100 子集 20 题（1 次/臂） | **+10.00pp** | +8.08pp | +2.96pp | **同向** |

- new_meeting 单次曾出现 faith −2.62pp（超 2pp 线 0.62pp）；按 §53c 追加 2 次运行后
  3 次均值 faith +0.48pp、recall 不降 → 通过（单次波动解释）。
- mixed100 子集 recall 100%、precision +8.08pp、faith +2.96pp；未出现"只在 cross30 提升"。
- 结论：**不是单集过拟合**；enrichment 对系列/状态元数据类事实是通用增益。

## 4. 影响范围与限制

- 只影响 `list_facts` 的 meeting/recording facts 与 `query_meeting_state` 条目的引用快照；
  search/get_minutes/get_transcript_excerpt 不变。
- 上下文变长会增加 token（少量），但 A/B 中 precision/faith 未劣化。
- 单裁判/单数据集边界不变；cross30 A/B 的绝对值 89.22% 仍低于 93% 的改进目标，
  但相对同配置 OFF +5.60pp；93% 还需下一步（查询分解/时间节点检索）。
- 默认开：`.env` `CHAT_TOOL_ENRICHMENT_ENABLED=1`；回滚 = 删该行 + 重建 backend。

## 5. 产物

`services/tool_metadata.py`、`services/kb_tools.py`、`services/state_service.py`、
`services/citations.py`、`tests/test_tool_enrichment.py`、
`evaluation/analysis/tool_enrichment_ab.py`、`tool_enrichment_generalization.py`、
`evaluation/results/tool_enrichment_ab.json`、`tool_enrichment_generalization.json`。
