# 评测集规模与公正性审计（2026-10-22）

## 1. 结论先行

用户质疑是对的：**新会议 scoped 的 recall 100% 不能当“真实泛化 100%”**，原因是评测设计过易：

- 限定 `meeting_ids=[3]` 后没有干扰会议；
- 15 题基本逐条对应 fixture 中的句子，题面与事实词面高度接近；
- gold 与题目由同一作者（我）从同一 fixture 派生；
- answer blind review 的 CSV 仍带 `expected_facts` 列，不是严格盲评；
- 只有 15 题/23 facts，CI 很宽。

同一批题的 **unscoped** 结果才是更像 RAG 的读数：

| 新会议 15 | recall | precision | faithfulness | relevancy |
|---|---:|---:|---:|---:|
| scoped（smoke） | 100% | 97.96% | 86.67% | 100% |
| **unscoped（主口径）** | **86.96%** | **70.97%** | **80.00%** | **100%** |

quality_gate 已改为使用 **unscoped** 新会议作为门禁；scoped 只作为 smoke 展示。

## 2. 当前评测集规模（去重前）

| 资产 | 题数 | 事实数 | 说明 |
|---|---:|---:|---|
| cross30 | 30 | 58 | 旧会议，跨录音 |
| holdout A | 20 | 32 | 独立事实复核 |
| holdout B | 20 | 28 | floor 验证 |
| holdout C | 20 | 22 | CRAG 验证 |
| holdout D | 20 | 33 | 3-way 验证；仅 18 个新题面 |
| holdout E | 20 | 34 | turn filter 验证；仅 11 个新题面 |
| new meeting | 15 | 23 | 合成新会议 |
| trajectory | 27 | — | 工具轨迹 |
| pass3 | 12 | 23 | 稳定性 |
| qa_seed / extended | 112 / 68 | 187 / 125 | 早期开发集 |

全目录约 **40 个 QA/gold 文件、1032 条原始 case、259 个唯一题面**，但其中大量是：

- 同一批题的 reviewed/independent/final 副本；
- 同一会议的不同 holdout；
- 反复用于调参的开发集。

**真正“未见 + 未参与调参 + 独立复核”的题目只有新会议 15 题这一批，且它仍是合成语料。**
人工金标为 0；所有 gold 都是 `human_checked=false`。

## 3. 评测设计的问题

1. **范围泄漏**：scoped 到单会议后，检索几乎不可能错，recall 100% 是 scope 的产物。
2. **题面—事实同源**：题目和 expected facts 从同一段话派生，词面重叠高。
3. **无干扰项**：没有其他会议、相似主题、不可回答题。
4. **审核非严格盲**：答案 review CSV 带 expected facts，审核者可以直接对照。
5. **样本小**：15 题/23 facts，CP 区间 85–100%，不能当精确值。
6. **单语料/合成语料**：真实会议泛化仍未知。

## 4. 更可信的评测设计（建议）

### 分层
- **Smoke（scoped）**：只验证链路是否工作，不作为质量结论。
- **Realistic（unscoped，多会议 + 干扰项）**：主口径。
- **Hard（paraphrase / multi-hop / 时间绑定 / 不可回答）**：压力测试。
- **Real + human**：真实会议、人工签名金标；最终对外结论只认这一层。

### 约束
- 答案盲评 CSV **不带 expected facts**，只给问题、答案、来源材料；
- 事实复核与答案复核由不同 Agent/人完成；
- 题目生成者与 fixture 作者分离，至少一个独立改写轮；
- 固定题集、预注册阈值、3 轮重复、配对 CI；
- 新会议至少 3–5 场，包含同名实体/相似主题的干扰会议；
- 报告必须同时给 sample size 和 CI，不报单点 100%。

## 5. 对当前结论的修正

- 不再说“新会议 recall 100% / precision 98%”是泛化结果；
- 主口径改为 unscoped：recall 86.96%、precision 70.97%、faithfulness 80.00%；
- scoped 100% 只能写“smoke 通过”；
- quality_gate 已切换为 unscoped；
- 收尾结论应写：**链路在新合成会议+跨会议混淆下可用，但真实会议/人工金标仍缺失**。
