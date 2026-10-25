# 答案级证据校验与有界修复实验（预注册 LEDGER §40，2026-10-22）

## 1. 结论先行

- **机制安全但不达成功线**：cross30 fresh `faithfulness_content` 87.88% → **88.39%（+0.51pp）**，
  new_meeting 83.78% → **85.51%（+1.73pp）**；两集都是「2 题改善、0 题变差、其余持平」。
  预注册成功线是 **≥ +5pp**，**未通过**（配对 Wilcoxon p=0.5，功效不足，只能报方向）。
- **没有靠删内容换分**：答案期望事实词法覆盖cross30 −0.05pp、new_meeting ±0；答案长度中位数
  不变；守卫触发回退 8/30（删除比例>30% 6 例、压缩<60% 2 例），回退即原答案，无灾难性删除。
- **根因不在"答案多写了"而在上游**：低 faithful 的 series/跨录音题（cs05–cs09、ct05 等）
  即使把不被上下文支撑的断言全删掉，也没法在不丢内容的前提下变好——这些答案本身是
  **范围/证据选择错了**，不是措辞越界。优化优先级应回到检索/作用域/证据通道，而不是继续
  加强答案侧删除。
- **附带发现（更重要）**：cross30 的低 faithful 里约 1/3 的"不支撑断言"是**工具元数据/协议
  信息没有进入引用证据快照**（如 list_facts 已返回 `duration_minutes`，但 citations.py 构造
  录音引用时丢弃；state 工具结果级 `notice` 未进入任何引用）。这属于**证据通道缺陷 +
  判定仪器口径**，已在产品侧修第一处并加单测（见 §6）。

## 2. 机制（通用、无题面规则）

预注册见 `evaluation/LEDGER.md` §40。三臂：

| 臂 | 做什么 | 调模型 |
|---|---|---|
| baseline | 既有 packet 里的答案与 deepseek 判定 | 0 |
| recitation（确定性） | 数字/引语在被引 refs 缺失、但在其它检索上下文出现 → **只追加引用，不改文字** | 0 |
| repair（校验+有界修复） | deepseek 逐断言判 `supported_by_cited / supported_by_other_context / citation_missing / derived / unsupported / meta`；机械地「补引用 / 删无支撑断言」，不重写、不新增事实 | 1 次/题 |

守卫：删除断言 >30% 或答案压缩 <60% 或引用被删空 → 回退原答案。判定沿用
`judges/rag.py` 的 ANSWER_SYSTEM；上下文不变，因此 baseline 的 context 判定复用（不变量），
只重判答案（`faithfulness_content` 内容级口径）。

样本：`results/cross30_fresh_packet.json`（30 题，fresh）与 `results/new_meeting_packet.json`
（15 题，unscoped 合成新会议）。预注册预算上限 250 次；两次运行实际授权 150+80=230 次，**实际使用 57 次**（cross30 35 + new_meeting 22，
跳过 qwen 跨族判定：qwen 判定实测 30–120 秒/次，30 题预计 20+ 分钟，超出本轮时间预算；
这是本实验的已知缺口，见 §5）。

## 3. 结果

### 3.1 四指标（pooled，deepseek 主判）

| 集合 | 臂 | faithfulness | faithfulness_content | answer_relevancy | 词法覆盖（期望事实） |
|---|---|---|---|---|---|
| cross30（30） | baseline | 88.63% | 87.88% | 93.33% | 0.4100 |
| cross30（30） | repair | **89.11%** | **88.39%** | 93.33% | 0.4095 |
| new_meeting（15） | baseline | 80.00% | 83.78% | 100% | 0.6433 |
| new_meeting（15） | repair | **83.54%** | **85.51%** | 100% | 0.6433 |

> context_precision / context_recall 由"检索上下文"决定，本机制不改检索；上下文判定复用基线，
> 两臂同值（这是不变量，不是提升）。

### 3.2 逐案配对

| 集合 | 改善 | 变差 | 持平 | 删除断言数 | 回退 |
|---|---:|---:|---:|---:|---|
| cross30 | 2 | 0 | 28 | 校验器判 unsupported **33**（153 keep / 9 derived / 58 meta）；其中 **27 条落在守卫回退题、未实际删除**，实际删除 **6** 条 | 8 |
| new_meeting | 2 | 0 | 13 | 判 unsupported **7**、实际删除 **7**（57 keep / 1 recite / 4 derived / 31 meta） | 0 |

- **口径必须写清**：上表"0 变差"指**内容级faithfulness_content**。若看**全部断言口径**，
  cross30 有 1 题略降（cm10 1.000→0.857：删掉 1 条断言后判定器对该答案的 claim 分割变化，
  内容级仍 1.000），另有 3 题改善（cm01/cs02/ct03）；new_meeting 全部断言口径 0 变差（n08/n13 改善）。
  这不是"机制伤质量"，但足以说明单裁判的 claim 分割本身有噪声，报告不能只报一个口径。
- 变差为 0 是好事，但**改善只有 2/30、2/15**：绝大多数答案本来就 faithful（87.9%/83.8%），
  没有可修的"越界断言"。
- 真正低 faithful 的题（cs01 0.50、cs05 0.40、cs06 0.14、cs09 0.40、ct05 0.20）全部被守卫拦下
  （删除比例>30% 或压缩<60%）；另有 cs03/cs04/cs08 也触发守卫回退，但它们本身 faith 不低
  （0.67–1.00），回退是守卫在保护内容而不是修复失败——**拦下是对的**：这些题删掉大半答案
  会立刻失去内容，属于"上游答错、下游删不动"。

### 3.3 为什么"删"不动：低 faithful 题的上游归因

对 `eval100_scope_fixed_packet.json` 前 30 题（cross30 主口径）逐条拆：

- 内容级断言 233 条，不支撑 **35 条（15.0%）**；
- 其中 `<35` 条里：引用了系列/状态元数据 12 条、工具协议/notice 4 条、
  录音时长 2 条、其它（真·过度推断/与原文矛盾）17 条；
- m015 的两条时长断言（15.9 分钟 / 4.5 分钟）**并非编造**：工具 `list_facts(kind=recording)`
  已返回 `duration_minutes`（`kb_tools.py:946`），但 `citations.py` 构造录音引用时把它丢了，
  判定者拿到的引用快照只有 task/time/period，只能判"不支撑"。
- m019 关于 `notice`"只能把 CONFIRMED 当有效结论"的断言同样来自工具结果级 notice，
  而 notice 没有进入任何引用快照。

结论：**"faithfulness 低"里有一块是证据通道/仪器口径，不是生成质量问题**。先修证据通道
（零调用）比继续加 prompt/删除策略 ROI 高。

### 3.4 确定性重引用臂：不安全，不能默认开

recitation 臂在 cross30 上改了 47 处句子（只追加 ref），但审计显示它按**字面 token**匹配，
会把"09:00 开始"这类句子追加到只含元数据时间的 ref（例如 `cm09` 给标题行追加 [1][2]，
`ct01` 给"上午会开始时间"追加 [1][3]）。这种 re-citation 会制造新的张冠李戴。
**结论**：重引用必须由语义校验器给证据 ref（本实验 repair 臂正是这样，但校验器把能支持的
都判成 supported_by_cited 了，实际 recite=0），不能靠字面匹配；该臂不进入产品默认。

## 4. 判定（按预注册）

| 预注册条件 | 实测 | 结论 |
|---|---|---|
| cross30 faith_content ≥ +5pp | +0.51pp | **未过** |
| 期望事实词法覆盖下降 ≤2pp | −0.05pp | 过 |
| answer_relevancy 下降 ≤3pp | 0 | 过 |
| 单题删除>30% 的 case ≤3 | 0（6 例均回退） | 过 |
| 复现集方向一致 | +1.73pp，同向 | 过（方向） |

**登记**：机制"安全、无副作用、但增益不足以抵消成本"，**不进入产品默认**；
若未来上线，只应作为"高价值会话"的可选后处理，且需先修 §5 的仪器缺口。

## 5. 限制与未做

0. **预注册时序不可独立证明**：预注册（LEDGER §40）与结果（§40b）在同一未提交工作树里，
   LEDGER mtime 晚于结果 JSON；内容顺序正确，但没有独立 commit 证明"先于调用写死"。
   后续实验应在跑数前把预注册单独 commit。


1. **未跑 qwen 跨族判定**：主判 deepseek 与校验器同族，存在"校验器-裁判同族"闭环风险；
   本实验的 +0.5/+1.7pp 只能当方向信号，不能当人工质量证据。
2. **未跑真实链路**：本实验在既有 packet 答案上做后处理，产品侧尚未把"校验+修复"接入
   流式链路（修复发生在 token 已推给用户之后；产品化需要"纠正事件/最终稿替换"或
   "非流式高价值模式"的设计）。
3. **词法覆盖是代理**：不是金标；它只能证明"没有大段删除"，不能证明"删掉的全是该删的"。
4. n=30/15，CI 很宽；faith 改善只有 2 个不一致对，统计上不可区分于 0。

## 6. 产品侧已落地的零调用修复（本轮）

**引用证据快照补全**（`services/citations.py`）：`list_facts(kind=recording)` 的引用
quote/source_text 现在包含工具已返回的 `duration_minutes`（例如
`录音 task 7 · 2026-09-10 14:00:00 · 下午 · 时长 4.5 分钟`）。单测
`tests/test_citations.py::test_recording_fact_citation_carries_duration`。
作用：判定者/用户点回都能看到答案里合法的时长断言所依据的原始字段，减少"证据没被引用
快照带出"导致的假 unsupported（m015 类）。

**RAG 质量闸归因升级**（`services/rag_quality.py` + `chat_turn.py`）：数字/引语不在
**被引**上下文、但在**本轮其它检索上下文**里出现时，标为
`number_attribution_mismatch` / `quote_attribution_mismatch` 并给 `suggested_refs`；
哪里都找不到才保留 `number_not_in_cited` / `quote_not_in_cited`。只标注不拦截。
单测 8 项全过。作用：把"可修的引用归属错"与"疑似编造"分开，减少误报（与实体核查同款纪律）。

## 7. 下一步（按 ROI，均已写入主报告）

1. **判定仪器补 meta 类别**（需要新 label + 重基线）：把"工具协议/usage notice 类断言"
   与"纯出处说明"一样归 `meta`，不要计入内容级 faithful 分母。当前 cross30 有 4 条此类
   断言被算作不支撑，属仪器误判。
2. **上游作用域/证据选择**：series 题的 `series_id`/范围、录音选择、跨录音证据合并——
   这是 cross30 低 recall/低 faith 的主因；不是答案侧能修的。
3. **校验+修复的产品化**：只有在"最终稿替换"UX 方案确定后才值得接；否则维持离线/评测侧。
4. **qwen 跨族复判**：把本实验的 15/30 题在 qwen 下重判，补跨族证据；成本约 60 次调用。

## 8. 复现

```bash
set -a; . meetingbot/fastapi-app/.env; set +a
meetingbot/fastapi-app/.venv/bin/python evaluation/experiments/claim_repair.py \
  --packet evaluation/results/cross30_fresh_packet.json \
  --out evaluation/results/cross30_claim_repair.json \
  --authorized-llm-calls 150 --skip-cross
meetingbot/fastapi-app/.venv/bin/python evaluation/experiments/claim_repair.py \
  --packet evaluation/results/new_meeting_packet.json \
  --out evaluation/results/new_meeting_claim_repair.json \
  --authorized-llm-calls 80 --skip-cross
```

产物：`results/cross30_claim_repair.json`、`results/new_meeting_claim_repair.json`。
