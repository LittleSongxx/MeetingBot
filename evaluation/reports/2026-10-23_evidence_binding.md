# 生成后引用绑定：离线回放 + 配对回放 + 4 次 A/B（2026-10-23）

> 预注册：`evaluation/LEDGER.md §49`（离线回放先于实现）、`§49c/§49d/§49e`（重复 A/B，判定线不改）。
> 结论：**PASS → 产品默认开启** `CHAT_CITATION_BIND_ENABLED=1`（纯规则、零模型调用）。

## 1. 机制

`chat_graph` 最终答案落库前，调用 `services/citation_bind.py`：

1) 只处理**带引用句**里的数字/日期 token（与 `claim_citation_audit.py` 同一口径）；
2) token 不在被引上下文、但在其它检索上下文里出现 → 把包含它的 ref 追加到该句引用
   （选与句子二元组重叠最高的 ref，同分取小 ref）；
3) token 哪里都没有 → 不改写（v1 不删内容），记录 `unbound`；
4) 留痕写入 `moderation_json.citation_binding`（命中/重绑/unbound 计数），done 事件也带摘要。

## 2. 离线回放（零调用，现有答案重放）

| packet | 检查数字 | 重绑 | cited-gap 前→后 | any-gap 前后 |
|---|---:|---:|---|---|
| cross30_fresh | 213 | **19** | 18.31% → **9.39%**（−8.92pp） | 9.39% → 9.39% |
| mixed100 | 377 | 22 | 19.63% → 13.79% | 13.79% → 13.79% |
| p0_r1 | 60 | 5 | 8.33% → 0.00% | 0.00% → 0.00% |
| p0_r2 | 70 | 7 | 11.43% → 1.43% | 1.43% → 1.43% |
| p0_r3 | 43 | 5 | 11.63% → 0.00% | 0.00% → 0.00% |
| new_meeting | 59 | 0 | 5.08% → 5.08% | 5.08% → 5.08% |

回放闸：cross30 重绑 **19/39**（门槛 ≥15）→ **PASS**；且任何 packet 的 any-gap 不变
（重绑只改引用、不改"哪里都没有"的数字）。

## 3. 配对回放（同答案/同上下文，只应用绑定，再重判一次）

| 指标 | 原答案 | 绑定后 | Δ |
|---|---:|---:|---:|
| 数字 cited-gap | 18.67% | **9.64%** | **−9.03pp** |
| 数字 any-gap | 9.64% | 9.64% | 0.00pp |
| context_precision | 67.33% | 66.67% | −0.67pp |
| faithfulness（内容级） | 89.85% | **90.91%** | **+1.06pp** |
| answer_relevancy | 100% | 100% | 0 |

这排除了"跨运行重生成"的混杂：机制本身只加引用，不降质量。

## 4. 运行时 A/B（cross30 30 题，每臂 4 次运行，均值）

| 指标 | OFF（4 次均值） | ON（4 次均值） | Δ |
|---|---:|---:|---:|
| 数字 cited-gap | 15.93% | **7.58%** | **−8.35pp** |
| 数字 any-gap | 9.56% | 7.58% | −1.98pp（跨运行波动） |
| context_precision | 64.77% | 64.02% | −0.75pp |
| context_recall | 81.90% | 85.40% | +3.50pp |
| faithfulness（内容级） | 87.81% | 86.48% | −1.33pp |
| answer_relevancy | 98.33% | 99.17% | +0.84pp |

判定（§49c/§49d/§49e）：主指标 cited-gap −8.35pp ✓；faithfulness 非劣 ✓；
precision 非劣 ✓；relevancy 非劣 ✓；配对回放 any-gap 不变 ✓、faith 非劣 ✓ → **PASS**。

> 为什么跑了 4 次：前两次 A/B 的 precision 差异（−2.53pp）主要来自**两次运行检索到的
> 上下文不同**（`contexts = retrieved`，与绑定无关）；配对回放 precision 仅 −0.67pp。
> 追加重复后均值收敛（−0.75pp）。判定线自始至终未改。

## 5. 默认状态与回滚

- 默认开：`meetingbot/fastapi-app/.env` 设 `CHAT_CITATION_BIND_ENABLED=1`
  （`.env.example` 已记录开关与依据）。
- 回滚：删掉该行并重建 backend（绑定是纯规则，回滚不动数据）。
- 产品测试 653 项通过（含 `tests/test_citation_bind.py` 6 项、`tests/test_chat_bind.py` 2 项）。

## 6. 限制

- 单裁判（deepseek-flash，非人工金标）；attribution 是规则审计，不是语义真值；
- 引语 13/13 不在任何上下文，本机制不处理；真正 `unbound` 的数字保持原样（不删信息）；
- 跨运行波动约 5pp：单次 A/B 不足以判定，本报告用 4 次均值 + 配对回放；
- 字面匹配（中文数字/日期格式差异不计入重绑）。

## 7. 产物

`services/citation_bind.py`、`services/chat_turn.py`（集成）、`services/chat_persistence.py`（留痕）、
`evaluation/analysis/evidence_bind_replay.py`、`evaluation/analysis/evidence_binding_ab.py`、
`evaluation/analysis/evidence_binding_repeat.py`、`evaluation/results/evidence_bind_replay.json`、
`evaluation/results/evidence_binding_ab.json`、本次 A/B 的 answers/packet（`cross30_bind{off,on}[1-4]_*`）。
