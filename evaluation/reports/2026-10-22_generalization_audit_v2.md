# 优化设计通用性/泛化自查 v2（2026-10-22）

## 1. 审计结论

**默认产品链路没有发现按 case/会议硬编码的作弊式补丁。**
主要风险不在产品代码，而在评测口径：

- 默认启用的只有通用检索、rerank、引用协议、确定性 grounding 检查；
- 所有按意图/评分/过滤的实验开关默认关闭；
- 搜索到的具体人名/日期主要出现在工具描述示例、注释、测试/评测里，不参与生产分支判断。

## 2. 默认启用清单（产品）

| 机制 | 默认 | 通用性判断 |
|---|---|---|
| dense + BM25 + RRF | 开 | 通用检索，无 case 规则 |
| rerank 24→3 | 开 | 通用 |
| 首轮 required tool_choice | 开（供应商支持时） | 通用协议约束 |
| citation `[n]` + 实体核查 | 开 | 通用 |
| `rag_quality` 数字/引语 grounding 标注 | 开 | 通用确定性检查，只标注不拦截 |
| 300 字符切片 | 开 | 通用参数 |

## 3. 默认关闭的实验（不应作为“已优化”宣传）

| 实验 | 默认 | 风险 |
|---|---|---|
| intent router / selective / hint / budget / force first tool | 全关 | 规则在 27 题上调过，可能过拟合 |
| turn filter | 关 | 答案质量无稳定增益 |
| evidence hint | 关 | 同上 |
| CRAG | 关 | holdout 无稳定收益 |
| contextual retrieval | 关 | 单会议语料，收益不可迁移 |
| parent context | 关 | 之前实现有引用语义问题 |
| query plan | 关 | 收益不足 |
| relevance filter / dedupe / score floor | 关 | 单会议调参风险 |

## 4. 评测口径风险

1. **mixed100 是组装集**：cross30/A/D/new/B 来自不同轮次，不是单次 100 题运行；
2. **holdout A/B/D 参与过调参**，不是干净 holdout；
3. **new meeting 是合成语料**，gold 由我/fixture 派生，独立 Agent 只做一致性复核；
4. **cross30 gold 含实现元数据**（supersedes_id、系列只含会议2 等），context 里没有时 judge recall 会误低；
5. **quality_gate 原阈值贴着当前分数**，存在“为过门而设阈值”的过拟合风险；
6. **盲评只 20 条**，且不是人工签名。

## 5. 本轮修正

### 5.1 quality gate 改为相对基线
- 冻结 `evaluation/baselines/quality_gate_baseline.json`；
- 允许 2pp 波动；
- 低于绝对下限或低于冻结基线 2pp 才失败；
- 不再按当前分数临时调阈值。

### 5.2 保留“只标注不拦截”的 grounding 检查
- 数字/日期、引语不在被引上下文 → warning；
- 不按 case 改答案、不硬编码正确结果。

### 5.3 未采纳的优化
- `query_meeting_state` 系列元数据实验：series 10 题 recall 0.579/faith 0.671，
  未稳定改善，已回滚；
- 不把“减少工具调用”的 F1 优化默认上线（faithfulness 下降）。

## 6. 结论与原则

后续优化必须满足：

1. **机制级**：改检索/生成/验证的通用策略，不按题面/会议写分支；
2. **默认关闭实验**：未过新 holdout 的规则不得默认开；
3. **评测分层**：smoke / realistic / hard / real+human，不拿 smoke 当泛化；
4. **门禁相对化**：冻结基线 + 容忍度，不贴着当前分数设阈值；
5. **人工/独立盲评**：不把 judge 当最终事实。

## 7. 下一步

- 完成单次 100 题运行，替换组装集；
- 抽 40–50 条无 expected facts 盲评；
- cross30 的 citation attribution/推断边界用通用生成约束修，不按题写规则；
- 真实会议 3–5 场 + 人工金标。
