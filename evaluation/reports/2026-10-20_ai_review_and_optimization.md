# 2026-10-20 独立 Agent 代审 + 最终优化（停止复现实验版）

- 用户授权独立子 Agent 代人工审核；本轮共开 4 个独立复核 Agent。
- 调用不设上限：产品侧累计 **3,976 次**调用、记账 **¥1.5422**。
- **A/B 复现实验未完成**（臂 A 已跑、臂 B 未跑，因用户要求停止）；以下 A/B 只报
  已完成的单轮结果，不能当稳定收益。

## 1. 独立审核事实与答案

### gold 事实（independent-reviewer）
- 58 条 expected_fact：**56 approve / 2 revise / 0 reject**
- 2 条 revise（ct02-2、ct04-2）已写入 gold；`human_checked=false`，
  `label_source=independent_agent_reviewed`。

### 答案（独立复核 Agent）
| 轮次 | 状态 | correct | partial | wrong |
|---|---|---:|---:|---:|
| 修复前（final2） | 未修 series_id 等 | 20 | 6 | 4 |
| **修复后（final4）** | 最终交付 | **21** | **9** | **0** |

修复后 partial：cm02（漏第 5 项风险）、cs08（未回退草稿纪要 4）、cs02（未补 OPEN 待办）、
ct02（漏 3 个中间节点）、cm03、ct06、cm07、ct08、ct09（各有 1 项关键遗漏）。
**无 wrong、无核心幻觉。**

## 2. 本轮修复的真实问题

1. **series_id/meeting_id 混淆**：模型把 series_id 传成 2，导致 cs01/cs03/cs07/cs10
   全空；现校验系列存在性并提示“系列 id 与会议 id 不同”，4 条全修复。
2. **候选记忆不可见**：加 `include_proposed=true`，cs02 能答 PROPOSED 候选。
3. **草稿纪要回退**：`get_minutes(include_draft=true)`，cs06 能给出 DRAFT 纪要 4 的
   三项结论并标草稿。
4. **状态查询模糊匹配**：`移动端附件下载修复` 能命中 `移动端附件下载进度问题须修复`。
5. **偏好归属**：preference 返回 `owner_name`，cs04 正确答“系统管理员”。
6. **数据环境对齐**：新增能力表重置为 `db/02-seed.sql` 状态，消除“运行库≠seed”的
   系统性偏差。
7. **答案完整性提示**：完整清单题要求逐条覆盖；仍未能修复 cm02 漏第 5 项风险。

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
- 注意：gold 有 4 条标签经 AI 复核修正，100% 不能当独立泛化证据。

### 跨会议 A/B（30 题 ×2，第一轮；复现未完成）
| 指标 | planning 关 | planning 开 |
|---|---:|---:|
| context_recall | 44/58 = 75.86% | 48/58 = 82.76% |
| context_precision | 74/106 = 69.81% | 93/136 = 68.38% |
| faithfulness | 180/222 = 81.08% | 189/237 = 79.75% |
| answer_relevancy | 29/30 = 96.67% | 30/30 = 100% |

单轮：recall +6.90pp、precision −1.43pp（在预注册 3pp 内），**达到阈值**；
但历史上两轮方向不一致，且本次复现只跑了臂 A，**保持 `KB_QUERY_PLAN_ENABLED=0`**。

### 注入（最后一轮线上）
- 注入标记 10/10、良性误报 0/20、系统提示泄露 0/30、跨会议外发 0/10。

## 4. 未完成 / 不可引用

- 跨会议 A/B 复现：臂 A 已跑，臂 B 未跑；不能把单轮 +6.90pp 写成稳定提升。
- 9 条 partial 答案未继续修（用户停止等待）。
- 人工签名仍为 `human_checked=false`（独立 Agent 代审，不等于人工）。
- 单会议语料；成本路由只有 5 题试点，两模型未定价，不能报金额降幅。
