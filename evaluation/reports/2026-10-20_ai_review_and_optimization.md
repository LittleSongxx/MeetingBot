# 2026-10-20 独立 Agent 代审 + 优化 + A/B 复现实验（完成版）

- 用户授权独立子 Agent 代人工审核；共 4 个独立复核 Agent。
- 不设调用上限：产品侧累计 **4,319 次**调用，记账 **¥1.6624**。
- A/B 复现已完成（两轮 30×2）；结论：无稳定收益，保持 `KB_QUERY_PLAN_ENABLED=0`。

## 1. 独立审核结果

### gold 事实
- 58 条 expected_fact：**56 approve / 2 revise / 0 reject**；2 条 revise 已入 gold。
- `label_source=independent_agent_reviewed`、`human_checked=false`；独立 Agent 代审不等于人工签名。

### 答案
| 轮次 | correct | partial | wrong |
|---|---:|---:|---:|
| 修复前 | 20 | 6 | 4 |
| **最终（独立复核）** | **21** | **9** | **0** |

最终 partial：cm02（漏第 5 项风险）、cs08（未回退草稿纪要 4）、cs02（未补 OPEN 待办）、
ct02（漏 3 个中间节点）、cm03、ct06、cm07、ct08、ct09（各 1 项关键遗漏）。**无 wrong。**

## 2. 已修复的产品问题

1. series_id / meeting_id 混用 → 系列校验 + id 命名空间提示；4 条系列题全修复。
2. 候选记忆不可见 → `include_proposed=true`，cs02 能答 PROPOSED。
3. 草稿纪要回退 → `get_minutes(include_draft=true)`，cs06 能答 DRAFT 纪要 4 三项结论并标注。
4. 状态查询模糊匹配 → `移动端附件下载修复` 能命中完整条目。
5. 偏好归属 → `owner_name` 返回，cs04 正确。
6. 运行库与 seed 对齐，消除评测环境偏差。
7. 完整清单提示 + 工具 notice；cm02 仍未完全修复。

## 3. 最终指标

### 轨迹 27 题（AI-reviewed gold）
| 指标 | 读数 |
|---|---:|
| tool_call_f1 | **0.8272** |
| 首轮 strict | 19/27 = 70.4% |
| 首轮 acceptable | 25/27 = 92.6% |
| required 覆盖 | 25/27 = 92.6% |
| must_cite | 25/25 = 100% |
| 禁止工具 | 0 |

### pass^3（12 题 ×3）
- **12/12 = 100%**（CP 95% 73.5–100）
- gold 含 4 条 AI 复核修正；不能当独立泛化证据。

### 跨会议 A/B（两轮 30×2，独立复核 draft-gold）
| 轮次 | recall 关→开 | precision 关→开 | faithfulness 关→开 | relevancy 关→开 |
|---|---|---|---|---|
| 第一轮 | 75.86% → 82.76%（+6.90） | 69.81% → 68.38%（−1.43） | 81.08% → 79.75% | 96.67% → 100% |
| 复现轮 | 82.14% → 82.76%（+0.62） | 66.37% → 68.94%（+2.57） | 80.50% → 84.81% | 93.33% → 96.67% |

两轮方向不稳定：规划臂 recall 都是 82.76%，关臂从 75.86% 波动到 82.14%；
不能把第一轮 +6.90pp 当稳定收益。**保持规划关闭。**

### 注入（最后线上轮）
- 注入 10/10 检出、良性误报 0/20、系统提示泄露 0/30、跨会议外发 0/10。

## 4. 未完成 / 限制

- 9 条 partial 未继续修复（用户要求停止等待后仅补了 A/B 复现）。
- `human_checked=false`：独立 Agent 代审不是人工签名。
- 单会议语料；成本路由只有 5 题试点且两模型未定价，不能报金额降幅。
- 语料时间线仍矛盾：录音 2026-09-10、minutes deadline 2023、seed action 2026；
  cm01 等答案会沿用源字段年份。

## 8. 泛化 holdout（未用于调参，一次性读数）

为避免继续在调参集上打补丁，本轮另建 `evaluation/gold/holdout_generalization_20.json`：
- 20 个**此前未出现**的问题，32 条 expected_fact，覆盖两段录音/纪要/待办的不同侧面；
- 事实经独立子 Agent 复核：**30 approve / 2 revise / 0 reject**；2 条已修订；
- `label_source=independent_agent_reviewed`、`human_checked=false`、`do_not_tune=true`；
- 产品 prompt 先移除具体案例式表述（如具体时点/具体事项），再**只跑一次** holdout，未据其结果调 prompt。

| 指标 | 读数 |
|---|---:|
| context_recall | 31/32 = **96.88%**（CP 95% 83.78–99.92%） |
| context_precision | 37/61 = 60.66% |
| faithfulness | 106/118 = **89.83%**（CP 95% 82.91–94.63%） |
| answer_relevancy | 19/20 = 95.0% |

解读：在这组未参与调参的问题上，召回与忠实度都高，说明前面修的系列 id、草稿回退、
模糊查询、偏好归属等是**通用机制**，不只是对 27/30 题过拟合；precision 60.66% 说明
仍会带回一些边缘上下文，但回答忠实度没有随之下降。该读数只报一次，不再据此改产品。

## 9. 最终结论

- 查询规划：两轮 A/B 无稳定收益，保持关闭。
- 通用机制：series/meeting id 校验、include_proposed、草稿回退、状态模糊匹配、
  偏好归属、完整清单提示，均保留。
- 泛化：holdout 20 题 context_recall 96.88%、faithfulness 89.83%，未用于调参。
- 剩余限制：9 条调参集 partial 未逐条修；独立 Agent 代审不是人工签名；
  单会议语料、语料时间线仍有矛盾。
