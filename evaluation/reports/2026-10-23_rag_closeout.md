# RAG 收口报告（2026-10-23，作品集冻结版）

> 结论：**RAG 相关纳入"冻结收口"**：尺子冻结、两个通用机制默认开、一个负结果登记、
> 已知缺口写清；**不再继续追 cross30 93% 目标**（差距集中在时间节点类事实，继续做的成本/收益不划算）。
> 本报告只冻结"当前读数与机制状态"，不代表达到所有改进目标。

## 1. 冻结读数（quality_gate.py 实跑，PASS）

| 数据集 | context_precision | context_recall（旧口径） | recall-v2（主指标） | faithfulness | answer_relevancy |
|---|---:|---:|---:|---:|---:|
| p0（6×3 轮） | 64.2% | 97.2% | **97.2%**（35/36） | 90.7% | 100% |
| cross30 | 63.6% | 75.9% | **88.0%**（44/50，excluded 8） | 88.6% | 93.3% |
| new_meeting | 71.0% | 87.0% | **87.0%**（20/23） | 80.0% | 100% |
| mixed100（组装集） | 71.8% | 91.0% | **96.2%**（152/158，excluded 9） | 89.2% | 97.0% |

- 全部指标带 CP 95% 区间；冻结基线 7 个产物 + `fact_classes.json` sha256；
  `make quality-gate` **PASS**。
- mixed100 另有**单次/三次 fresh 运行**对照：三次均值 P 71.83 / R 90.02 / F 88.34 / AR 99.00，
  单次波动约 5pp；质量闸仍用组装集（单次运行未过预注册非劣线，详见 LEDGER §43/§46）。

## 2. 机制状态（默认开/关）

| 机制 | 主指标 | A/B 结果 | 泛化 | 状态 |
|---|---|---|---|---|
| S1 生成后引用绑定 | 数字 cited-gap | 4 次/臂：15.93%→7.58%（−8.35pp）；faith 内容级 −1.33、precision −0.75（过线） | 离线回放 6 个 packet（cross30 19/39、mixed100 22 条…） | **默认开** |
| S2 工具契约扩展（系列/状态元数据） | cross30 recall-v2 | 4 次/臂：83.62%→89.22%（+5.60pp）；precision −1.63、faith +0.97 | new_meeting 中性（3 次/臂）、mixed100 子集 +10pp | **默认开** |
| S2 状态提示（线索词） | cross30 recall-v2 | 2 次/臂：−5.17pp、faith −4.08pp | — | **负结果，默认关** |
| 引用可定位/未越权 | 零调用不变量 | 55/55、77/77（历史） | — | 保持 |
| tool pass^k | pass^3 | **85.19%（23/27，k=3）**；F1 macro 0.706、strict 51.9% | 27 题×3 trial | 已可评 |

## 3. 已知缺口（冻结时如实登记）

1. **cross30 recall-v2 88.0%（A/B 开启 enrichment 后 89.22%）距 93% 目标差 ~3.8pp**；
   剩余缺口集中在**时间节点类事实**（m022/m026 类），需要多时间点检索/查询分解，
   本轮不做（成本高、易过拟合）。
2. **单裁判**：faithfulness/AR 来自 deepseek 单裁判；claim 级锚点校准 α=0.426<0.70 →
   绝对数只作方向信号，不能外推为"答案正确率"。
3. **mixed100 是组装集**；单次 fresh 运行的 faithfulness 比组装集低约 4pp（运行波动 5pp 量级），
   因此质量闸未切换主口径。
4. **claim 级裁判不可靠**（α 校准失败）；引语 13/71 不在任何上下文，引用绑定只处理数字/日期。
5. **无人工 gold**：所有 gold 都是 AI/draft 或代理生成（human_checked=false）。

## 4. 收口判定

- **可以收口**（作为作品集 v1）：尺子 PASS、机制有 A/B + 泛化、负结果保留、缺口写清；
  后续任何改动都必须重新预注册并更新本报告，不得把旧读数与新读数相减。
- **不等于达标**：cross30 93% 目标未达；若将来要追，唯一下一步是"多时间点检索/查询分解"
  这条通用机制（预注册 + 跨集 A/B），而不是按 case 打补丁。

## 5. 证据

- `evaluation/LEDGER.md` §42–§55b；`make quality-gate` 输出；
- `evaluation/reports/2026-10-23_evidence_binding.md`、`2026-10-23_tool_enrichment.md`、
  `2026-10-23_state_coverage.md`、`2026-10-23_trajectory_passk.md`；
- `evaluation/results/{evidence_binding_ab,tool_enrichment_ab,tool_enrichment_generalization,trajectory_passk27_report}.json`。
