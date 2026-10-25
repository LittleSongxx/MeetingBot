# 独立验证报告：本轮产品修复 / 预注册实验 / 分析文档（2026-10-22）

> 验证员：verifier（对抗性独立验证，不写功能代码）
> 验证方式：**零模型调用、零 GPU、未重启后端、未修改任何产品/实验/结果/LEDGER/文档**；只新增本报告。
> 所有数字均从仓库内 packet / JSON / 报告 / 代码回源复算，不采信任何自述数字。
> 时间基线：系统时钟与文件 mtime 均为 2026-10-22 16:xx（见问题 P2-10：文档日期标注 2026-10-22 与之不一致，不影响内容）。

---

## 0. 结论：**有条件 PASS**（PASS with findings）

**可以合入/采信的部分**

1. **产品修复安全**：`services/citations.py`、`services/rag_quality.py`、`services/chat_turn.py` 改动小、无按 case 硬编码、不破坏既有契约（只标注不拦截、引用语义、`source_text` 语义）；边界行为与旧实现一致或更好；产品 targeted 单测 **94/94 通过**。
2. **测试与门禁全绿（零调用）**：评测侧全量单测 **330/330**；`make -C evaluation smoke` 退出 0 且 `model calls made: 0`；`quality_gate.py` **PASS**（21 行输出，其中 17 条带冻结基线的行 delta 全 ≈0）。
3. **预注册实验数字可复算**：`87.88→88.39%（+0.51pp）`、`83.78→85.51%（+1.73pp）`、`2 改善/0 变差/28 持平`、`守卫回退 8 题（删除>30% 6 + 压缩<60% 2）`、`33 条删除判定`、`recitation 47 处`、`预算 57 次`、`Wilcoxon p=0.5`、`词法覆盖 −0.05pp / ±0` 全部与两个结果 JSON 一致；未跑 qwen 跨族、未接产品流式等限制如实披露。
4. **主报告绝大多数读数可回源**：抽查 40 个关键数字，约 36 个与 packet/JSON/报告一致（见 §4）。

**必须修正后才能对外引用的部分（详见 §5）**

- **[P1-1] 主报告"cross30 分层 81.7/74.6/78.6 在三份产物上都复现不出来"是错的**：该组数字可复现——`eval100_scope_fixed_packet.json` 前 30 题按**逐 case 等权均值** = **81.67/74.57/78.63**；`eval100_packet.json` 前 30 题 = 78.33/67.94/80.82（对应旧报告"修正前 78.3/67.9"）。正确表述是"**逐 case 均值 ≠ pooled，不可混比**"（主报告同一段后半句和 LEDGER §40b 其实已写对，前后矛盾）。
- **[P1-2] `CER 24.91%` 与 `cpCER 30.09%` 不是同一次运行**：`cpcor_report_c3600_8m.json` 的 CER 宏平均 = **25.04%**；24.91% 来自 `cpcor_report_main_8m.json`（旧默认 c1200）。主报告 §0.2 第 9 行 / §1.1 / §1.2 把两者并列成 c3600 读数，属口径混用（LEDGER 既有文本也混，本轮主报告继承）。
- **[P1-3] 预注册"先于运行"无法独立验证**：§40 与 §40b 在**同一个未提交 diff** 里；LEDGER mtime（16:47:59）**晚于**两份结果 JSON（16:44:46 / 16:45:51）。只有文件内顺序与 JSON 自述字段可证。建议后续预注册先单独 commit 或落 hash/时间戳。
- **[P2] 4 处低严重度事实问题**：实验报告"删除 33 条断言"未注明其中 27 条被守卫回退、最终实际删 6 条；实验报告把 **cs08** 列为"低 faithful"（其 faith=1.0，低的是 precision/recall）；主报告"**所有** packet 的 adjudicator 都是 deepseek-flash"不成立（65 个 packet 中 52 个 deepseek、**11 个 qwen**、43 个无该字段）；主报告引用的 qwen v3 core **0.869 无法从当前 artifact 复算**（当前 `completeness_keypoints_v3_qwen.json` 无 summary，按 records 复算 ≈0.911/13 场，属 LEDGER 旧读数未标"旧"）。

**结论**：产品修复与预注册实验本身**通过**；分析文档的数字主体可信，但上述 P1 级口径/归因问题会实质性削弱"评测审计"章节的可信度，**引用前必须修正**。未发现硬编码/过拟合、未发现既有证据文件被误改、未发现功能回归。

---

## 1. 代码审查（git status + git diff）

改动（本轮 Lead 范围）：
`services/citations.py`（+12/−3）、`services/rag_quality.py`（+135/−40）、`services/chat_turn.py`（+3/−1）、`tests/test_citations.py`（+17）、`tests/test_rag_quality.py`（+30）、`evaluation/LEDGER.md`（+65/−0）、`README.md`（+3/−0）。

### 1.1 硬编码 / 过拟合检查

- 在 `citations.py` / `rag_quality.py` / `chat_turn.py` / 两个测试 / `experiments/claim_repair.py` 中 grep `m0xx|cm0x|cs0x|ct0x` **无任何 case id**；无题面/会议名特判；无按数据集分支。
- 测试新增数据全部为临时构造（如"产品发布评审会议"），未复制真实 packet 语料。
- `claim_repair.py` 的校验类别/守卫阈值来自预注册文本（删除>30%/压缩<60%/引用清空），非按题调参。

### 1.2 契约检查

| 契约 | 检查结果 |
|---|---|
| 只标注不拦截 | `chat_turn.py` 仅在答案生成后拼 `quality_note`，无拒绝/改写路径；diff 无其他消费方 |
| `audit_answer` 兼容性 | 新签名 `audit_answer(answer, citations, retrieved=None)`；**不传 retrieved 时行为与旧版逐项一致**（单测 `test_without_retrieved_keeps_legacy_kind` 覆盖，人工边界测试亦确认） |
| 引用语义 | 仅新增两种 kind：`number_attribution_mismatch` / `quote_attribution_mismatch`，旧 kind 保留；`suggested_refs` 为新增可选字段，`note()` 只渲染 kind/text（产品文案不变） |
| `source_text` 语义 | recording 分支原本就 `source_text=quote`，现在 quote 追加"时长 …"；meeting 分支的空标题/空时间规则未动；其他 kind 未动 |
| 其他调用方 | 全仓库 grep `audit_answer` 只有 tests 与 `chat_turn.py`；`rag_quality.note` 只有 `chat_turn.py`，无外部契约破坏 |
| 测试是否被弱化 | `git diff tests/` 全为新增行（无删除/放宽旧断言） |

### 1.3 边界行为（人工对抗测试，产品 venv 直接调用，零模型）

| 输入 | 行为 | 判定 |
|---|---|---|
| 空 answer + 空 citations | `[]` | OK |
| citations=None / answer 含 `[9]` 不存在 ref | `missing_citation` + 旧 kind（与旧版一致） | OK |
| 无 citations、数字在其它检索上下文 | `number_attribution_mismatch` + `suggested_refs` | 新能力正确 |
| 无 citations、数字哪里都没有 | 旧 `uncited_number`（未误报为归属错） | OK |
| 数字在被引 `source_text` 里 | 不标（旧实现用 quote/text/source_text 三键，保持） | OK |
| 引语在正确 ref / 引语含省略号不在上下文 | 通过 / `quote_not_in_cited` | OK |
| retrieved=[]（空列表） | `classify=True` 但无可建议 → 回落旧 kind | OK（不误报） |
| ref 非整数 / 秒级 locator / 百分比数字 / 4 字引语 | 不崩溃、按设计处理 | OK |
| retrieved 含非 dict 元素（如 None） | `AttributeError` | 仅鲁棒性提示：生产路径 `registry.as_dicts()` 只产生 dict，不可达；旧实现同样会崩，非回归 |
| `[123]` 这类 3 位引用编号 | 会被当成数字 token 多报一条 `number_not_in_cited` | **既有缺陷**（旧实现相同），非本轮引入 |

### 1.4 `citations.py` duration 修复

- 仅在 `kind=="recording"` 分支追加 `时长 {duration} 分钟`；`duration_minutes` 为 None/0/空时不写（合理）。
- 数字/字符串 duration 均正常（4.5 → "时长 4.5 分钟"）。
- 与工具字段核对：`kb_tools.py:946` 确实返回 `round((row.total_duration_ms or 0)/60_000, 1) or None`，即 `duration_minutes` 是工具真实返回字段 → 修复方向成立。
- 注意边界：answer 若写"约 4 分 30 秒"仍不匹配（确定性词法检查的固有上限，非缺陷）。

---

## 2. 测试结果（全部零模型调用）

| 命令 | 结果 |
|---|---|
| `cd meetingbot/fastapi-app && .venv/bin/python -m unittest discover -s tests -p 'test_citations.py'` | **2/2 OK** |
| 同上 `-p 'test_rag_quality.py'` | **8/8 OK** |
| 同上 `-p 'test_chat_service.py'` | **68/68 OK**（输出中有一条预期内的 mock `RuntimeError: 规划模型挂了`，最终 OK） |
| 同上 `-p 'test_chat_api.py'` | **16/16 OK** |
| `cd evaluation && ../meetingbot/fastapi-app/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | **330/330 OK**（4.0s） |
| `make -C evaluation smoke` | **退出 0**；输出 `model calls made: 0` |
| `cd evaluation && ../meetingbot/fastapi-app/.venv/bin/python quality_gate.py` | **QUALITY GATE: PASS**；21 行指标全过（4 行 smoke 无冻结基线 + 17 行带 frozen，delta 全 +0.000/−0.000）；`frozen_at=2026-10-22` |
| 合计 | 产品 targeted **94 项**、评测侧 **330 项**、smoke 1 次、质量闸 1 次，全部通过、零失败；零模型调用 |

---

## 3. 预注册实验复核（LEDGER §40/§40b + 报告 + 两个 JSON）

### 3.1 预注册时序（可证部分与不可证部分）

- **可证**：`evaluation/LEDGER.md` diff 中 §40 在 §40b 之前；§40 写死了主结局（`faithfulness_content` ≥ +5pp）、样本（cross30 30 / new_meeting 15）、护栏（词法覆盖 ≤2pp、relevancy ≤3pp、单题删除>30% ≤3 例）、失败判定与 ≤250 调用预算；两份 JSON 内也有 `pre_registration` 字段。
- **不可证**：两节在同一次未提交改动中，无独立 commit / hash / 时间戳锚点；LEDGER mtime（2026-10-22 16:47:59）晚于结果生成时间（16:44:46 / 16:45:51）。**"先于任何调用写死"目前只有自述证据**，建议下轮预注册先行 commit。

### 3.2 关键数字逐项复算（全部独立重算，未采信报告）

| 报告/LEDGER 陈述 | 复算值（来源 JSON 字段） | 判定 |
|---|---|---|
| cross30 `faithfulness_content` 87.88→88.39%（+0.51pp） | 203/231=0.878788 → 198/224=0.883929，pooled Δ=**+0.514pp** | ✅ |
| cross30 `faithfulness` 88.63→89.11% | 226/255=0.886275 → 221/248=0.891129 | ✅ |
| new_meeting `faithfulness_content` 83.78→85.51%（+1.73pp） | 62/74=0.837838 → 59/69=0.855072，Δ=**+1.723pp** | ✅ |
| new_meeting `faithfulness` 80.00→83.54% | 68/85=0.800 → 66/79=0.835443 | ✅ |
| answer_relevancy 不变 | cross30 93.33%（28/30）两臂同；new 100%（15/15）两臂同 | ✅ |
| 词法覆盖 cross30 −0.05pp / new ±0 | 0.4100074→0.4094698（−0.0538pp）；new 0.64333→0.64333 | ✅ |
| cross30 2 改善 / 0 变差 / 28 持平 | cm01（0.9167→1.0）、cs02（0.75→1.0）；worsened=0；unchanged=28；Wilcoxon n_nonzero=2，p=0.5 | ✅ |
| new 2/0/13 | JSON delta 字段 improved=2, worsened=0, unchanged=13 | ✅ |
| 守卫回退 8 题（删除>30% 6、压缩<60% 2） | `fallbacks={"removed_ratio>0.30":6,"compressed<0.60":2}` | ✅ |
| "删除 33 条断言" | `actions.remove=33`（**删除判定数**）；其中 8 个回退 case 内 27 条、**最终实际删除 = 6 条** | ⚠️ 表述需注明（见 §5 P2-1） |
| 预注册条件"(c) 单题删除>30% 的 case ≤3" | JSON `catastrophic_removal_cases=6`（**守卫前**）；守卫后回退 6 题 → 交付答案 0 | ⚠️ 表格写"0（6 例均回退）"可接受，应与 JSON 字段名对齐说明 |
| recitation 臂改动 47 处句子 | `changed_segments` 求和 = 47（19 个 case） | ✅ |
| 预算 57 次（cross30 35 + new_meeting 22） | JSON `budget.used`=35/22；cross30=30 校验 + 5 重判，new=15 校验 + 7 重判 | ✅（但"250"见 §5 P2-8） |
| 未跑 qwen 跨族 | 两 JSON `qwen_baseline/repaired` 全 null、`judge_errors.*_qw="skipped"` | ✅ 如实 |
| 限制披露（同族闭环、未接流式、词法代理、n 小） | 实验报告 §5 + LEDGER §40b 全部写明 | ✅ |
| 全部断言口径 1 题略降（主报告 §0.4） | per-case `faithfulness`：cm10 1.0→0.857（唯一变差）；improved 3 | ✅ 与主报告一致 |
| 口径混用检查 | 报告 §3.1 表头明确 "pooled"，数值确为 pooled；faith 与 faith_content 分列；无"全部断言当内容级"混用 | ✅ |
| §3.3 "233 条内容断言 / 35 条不支撑" | `eval100_scope_fixed_packet` 前 30 题内容级 198/233 → 不支撑 35（15.03%），与报告一致；报告已注明 packet 名 | ✅（数字对，拆分 12/4/2/17 为人工分类，无法机器复现） |
| "低 faithful 题 cs05/cs06/cs08/cs09/ct05，faith 0.14–0.4" | cs05=0.4、cs06=0.143、cs09=0.4、ct05=0.2 均回退 ✅；**cs08 faith=1.0**（低的是 precision 0.333/recall 0）→ 列表错误 | ❌ 见 §5 P2-2 |
| m015 两条时长断言 15.9/4.5 分钟"并非编造" | `cs05` 答案确有 15.9/4.5 分钟断言；`kb_tools.py:946` 确有 `duration_minutes` 字段；但 fresh packet 的 contexts 里**没有**该字段（正是报告说的证据通道缺口） | ✅ 归因成立 |
| 确定性重引用臂 47 处不安全 | 与 `recitation_changes` 47 一致；报告给 cm09/ct01 张冠李戴样例在 JSON rows 中可查 | ✅ |

### 3.3 实验小节结论

实验**主结论可信**：机制安全、增益小（+0.51/+1.73pp）、未过 +5pp 成功线、不产品化——这些判断与数据一致。两个瑕疵：删除数表述、"低 faithful 题列表"含 cs08；不影响主结论方向。

---

## 4. 主报告数字抽查（40 个，回源 packet/JSON/报告）

> 来源缩写：CFP=`results/cross30_fresh_packet.json`；NM=`results/new_meeting_packet.json`；E68=`results/rag_packet_extended_tools2_final.json`；M100=`results/eval100_scope_fixed_packet.json`；QGR=`results/quality_gap_report.json`；MET=`results/metrics.json`；CP3600=`asr/first_test/cpcor/cpcor_report_c3600_8m.json`；CPMAIN=`asr/first_test/cpcor/cpcor_report_main_8m.json`。

| # | 主报告数字 | 回源复算 | 判定 |
|---|---|---|---|
| 1 | cross30 context_recall 75.86%（44/58） | CFP summary 44/58=0.758621 | ✅ |
| 2 | cross30 CP 62.8–86.1% | CFP Clopper-Pearson [0.6283,0.8613] | ✅ |
| 3 | cross30 context_precision 63.64%（84/132） | CFP 84/132=0.636364 | ✅ |
| 4 | cross30 faithfulness 88.63%（226/255）/ content 87.88% / relevancy 93.33% | CFP 226/255、203/231、28/30 | ✅ |
| 5 | cross30 缺口 14 条中 11 条元数据；cs 42.1%（8/19）、cm 94.7%、ct 90.0% | QGR `rag_attribution.by_packet.cross30_fresh`；自算 cm 18/19、cs 8/19、ct 18/20 | ✅ |
| 6 | new_meeting 70.97/86.96/80.00/83.78/100（15 题/23 facts） | NM summary + facts 计数 | ✅ |
| 7 | human27 81.82/100/94.21/97.20/100（27 题/62 facts） | `rag_packet.json` summary | ✅ |
| 8 | extended68 67.24/95.20/87.68/89.97/100（68 题/125 facts） | E68 `summary_f` 156/232、119/125、306/349、269/299、68/68 | ✅ |
| 9 | mixed100 71.83/91.02/89.21/90.17/97.00（100 题/167 facts） | M100 summary 255/355、152/167、529/593、468/519、97/100 | ✅ |
| 10 | 数字 cited-gap 18.31%（39/213）、any-gap 9.39%（20/213），CI 5.8–14.1% | `claim_audit_cross30_fresh.json` 213/39/20；CP CI [0.0583,0.1413] | ✅ |
| 11 | 引语 any-gap 18.31%（13/71） | QGR `citation_attribution.quotes.cross30_fresh` 71/13 | ✅ |
| 12 | 深判折算真 unsupported ≈7.25%/3.72%、规则高估 ≈2.7× | QGR `sensitivity_estimate`：39/20×(1−0.375−0.2292) = 7.25%/3.72%；18/188=9.6% vs 25.5% | ✅（报告已标"仅量级"） |
| 13 | 同一批 cross30 两产物 75.86% vs 81.03%；17/30 题逐题计数不同 | 两 packet 30 题题面逐条相同（cm01↔m001…）；独立比较 per-case (facts/contexts/claims/missing) 计数，**17/30 有任意指标计数不同**；layer pooled=47/58=81.03% | ✅ |
| 14 | 分层 81.7/74.6/78.6 在产物上**不可复现** | **可复现**：M100 前 30 题逐 case 等权均值 = 81.67/74.57/78.63；`eval100_packet` 前 30 = 78.33/67.94/80.82（对应旧报告"78.3/67.9"） | ❌ **报告表述错误**（见 §5 P1-1；"口径不同"的说法才对） |
| 15 | first-tool strict 14/27（51.85%）、acceptable 22/27、coverage 24/27 | `trajectory_freshbase27_report.json`；14/27=0.51852 | ✅ |
| 16 | tool F1 macro 0.7309 | 同文件 tool_set macro f1 0.730864 | ✅ |
| 17 | state F1 0.433（precision 0.3125） | 同文件 per_intent state f1 0.43333、precision 0.3125 | ✅ |
| 18 | over-call 18 次中 7 次无引用贡献；`query_meeting_state` 5/5 无贡献 | QGR `trajectory.over_call`：extra_calls 18、extra_without 7；query_meeting_state 5/5 | ✅ |
| 19 | 12 题只多调、3 题既漏又多、0 题只漏调 | `2026-10-22_tool_f1_deep_dive.md` §2 | ✅ |
| 20 | v3 core 0.781（deepseek）/ 0.869（qwen）；宏平均 74.57%（349/468，n=26） | `completeness_keypoints_v3.json` baseline macro 0.7457265、core 0.7810042、micro core 349/468；独立按 records 复算同为 0.7457/0.7810 | ✅ deepseek；**qwen 0.869 无法从当前 artifact 复算**（见 §5 P2-4） |
| 21 | plan+gap-fill +1.92pp（11↑12↓3=） | LEDGER §R4 原文；`completeness_plangap_v3.json` core macro 0.80023 vs baseline 0.78100 → +1.92pp | ✅ |
| 22 | cpCER 30.09%（8 场）、Δcp +5.06pp | CP3600 macro cpcor=0.3009、delta_cp=0.0506（8 场） | ✅ |
| 23 | CER 24.91% | **CP3600 macro cer_no_punct=0.2504**；24.91% 出自 CPMAIN（旧默认 c1200） | ❌ **非同源**（见 §5 P1-2） |
| 24 | 旧默认 c1200 cpCER 83.16% | CPMAIN macro cpcor=0.8316 | ✅ |
| 25 | MOSS 公开 cpCER 22.17 / CER 24.86 / Δcp −2.69 | 外部模型卡数字 | ⚠️ 本轮零调用纪律下未联网独立核验；报告已写"不可外推、22.17 只是方向锚" |
| 26 | E2E 94.98%（227/239，CI 91.4–97.4%） | MET `reliability.end_to_end` 227/239=0.94979，CP [0.9139,0.9738] | ✅ |
| 27 | 调用级 98.56%（1302/1321）；失败 16 = timeout12 / output_truncated3 / request_rejected1 | MET `call_success` 1302/1321=0.985617；`failure_modes.by_bucket` 完全一致 | ✅ |
| 28 | MINUTES 76.00%（19/25） | MET by_call_type MINUTES 19/25=0.76 | ✅ |
| 29 | p50 20.04s / p95 93.81s（全类型） | MET `latency_ms.by_call_type.all_calls`：n=1318、median=20041ms、p95=93812ms | ✅ |
| 30 | citation precision 65.38%（68/104，CI 55.4–74.4） | `citation_packet.json` 68/104，CP [0.5542,0.7445] | ✅（但裁判是 **qwen**，报告未标族，见 §5 P2-3） |
| 31 | 条目支撑率 50.0%（29/58）；多余引用 32.69%（34/104） | 同文件 29/58、34/104 | ✅ |
| 32 | 会议级问题率 23.08%（12/52） | `meeting_level_packet_v2` baseline 7/26 + `v2r` baseline 5/26 = 12/52 | ✅ |
| 33 | owner fabricated 9/143（6.3%）；期限 field_misuse 14 | METRICS_REPORT：grounded123/unknown10/empty1/fabricated9；field_misuse 14 | ✅ |
| 34 | MISSING_DEADLINE 误报 77.8%（7/9）/ 85.7%（12/14） | METRICS_REPORT 分桶 7/9、12/14 | ✅ |
| 35 | 初稿无依据原子 7.43%→5.21%（n=296/288） | METRICS_REPORT §1：22/296、15/288 | ✅ |
| 36 | 意图 F1 88.16% | METRICS_REPORT 宏平均 88.160% | ✅ |
| 37 | 引用可定位/未越权 100%/100% | MET `chat_citations` 55/55、77/77；METRICS_REPORT 71/71、107/107 | ✅（100% 两口径一致） |
| 38 | pass^3 不可评（evaluated_cases=0） | `trajectory_freshbase27_report.json` pass_k.evaluated_cases=0 | ✅ |
| 39 | mixed100 是组装集（非单次运行） | `2026-10-22_eval100_scale.md` §1 明写"由既有 fresh-session 产物组装…不是单次 100 题连续运行"；QGR P01 同 | ✅ |
| 40 | §6.2 交付计数：64 行状态板 / 39 条缺失事实归因 / 169 条不支持断言分类 / 13 条口径问题 / 11 个争议数字 / 18 条样例 | QGR：status_board.axes=64、missing_facts=39、type_totals 求和=169、protocol_issues=13、disputed_numbers=11、representative_samples=18 | ✅ |

**汇总**：40 项中 **36 项 ✅、2 项 ⚠️（外部数字/待核）、2 项 ❌（第 14、23 项）**，另有第 20 项 qwen 部分不可复算。

---

## 5. 发现的问题（按严重度）

### P1（必须修正，影响对外引用）

1. **"分层 81.7/74.6/78.6 不可复现"是错误的归因**（主报告 §1.2 注释行、`docs/质量指标全景与缺口分析.md` L22/L378、QGR `disputed_numbers`）。
   - 事实：从 `eval100_scope_fixed_packet.json` 前 30 题按逐 case 等权均值复算 = **81.67 / 74.57 / 78.63**；从 `eval100_packet.json` 前 30 题 = **78.33 / 67.94 / 80.82**（与旧报告"修正前 78.3/67.9"一致）。数字完全可复现，只是估计量不同（逐 case 均值 vs pooled）。
   - 影响：主报告 §1.2 同一段后半句与 LEDGER §40b 已正确指出"原报告是逐 case 均值"——**同段自相矛盾**；"三份产物上都复现不出来"的表述会被读者当成"旧报告数据来源不明/不可信"，进而削弱 P02/P13 论证。建议改为"**旧表是逐 case 等权均值（可复现为 81.67/74.57/78.63），与 pooled 81.03/66.67/84.47 不是同一估计量，禁止混比**"。
2. **`CER 24.91%` 与 c3600 cpCER 不同源**（主报告 §0.2 第 9 行、§1.1 轴1、§1.2）。c3600 报告是 `cpcor_report_c3600_8m.json`（cpcor 0.3009、cer **0.2504**、Δcp 0.0506）；24.91% 来自 `cpcor_report_main_8m.json`（旧默认 c1200）。给 c3600 的 cpCER 配旧运行的 CER 属口径混用，应写"CER 25.04%（c3600）"或明确标注来源运行。
3. **预注册时序不可独立验证**：§40/§40b 同一未提交 diff、无 commit/hash 锚点，LEDGER mtime（16:47:59）晚于结果 JSON（16:44:46/16:45:51）。不构成造假指控，但"先于任何调用写死"目前是自述；建议下轮预注册先 commit 或在 JSON 中落 `pre_registration_sha256`。

### P2（应修正，低严重度）

1. **"删除 33 条断言"表述不完整**：`actions.remove=33` 是删除**判定数**，其中 8 个回退 case 内 27 条被守卫回退，**最终实际删除 ≈6 条**（cross30；new_meeting 7 条全应用）。建议写"33 条删除判定 / 27 条触发回退 / 最终删除 6 条"。另 JSON `catastrophic_removal_cases=6` 是守卫前值，报告表"0"是守卫后值，建议注明"回退后"。
2. **实验报告低 faithful 列表含 cs08 错误**：`cs08` faith=1.0（其短板是 precision 0.333 / recall 0），不属于"faith 0.14–0.4"；cs05/cs06/cs09/ct05 属实且均被守卫回退。
3. **"所有 packet 的 adjudicator_disclosure = deepseek-flash / not_gold" 过度概括**：grep 65 个结果 packet → 52 个 deepseek-flash、**11 个 qwen**（如 `citation_packet.json` 为 qwen3.8-max-0902、meeting_level 系列 qwen3.8-max）、43 个无该字段。主报告引用 citation precision 65.38% 时未标裁判族（该数是 qwen 判的），与"单裁判 deepseek-flash"叙述冲突。
4. **qwen v3 core 0.869 不可从当前 artifact 复算**：`completeness_keypoints_v3_qwen.json` 无 `summary` 字段；按其 records 复算 baseline 宏平均 core ≈0.911（13 场有判定），与 LEDGER 的"15 场干净配对 0.869"不一致。该数属 LEDGER 旧读数，主报告应标注"旧/补判中，未复算"。
5. **预算口径**：预注册上限 ≤250，但两条复现命令授权 150+80=**230**，实际用 57。报告写"57/250"会把"授权即 230"藏掉，建议写"57/230（预注册上限 250）"。
6. **p0 行粉饰风险**：`p0 6 题×3 轮 64.23/97.22/90.75/90.71/100` 中 faith 90.75/90.71 是**逐轮等权均值**（quality_gate 基线同源），若按 pooled 是 90.64/90.57；precision/recall 两种算法恰好相同所以看不出混用。建议注"faith 为逐轮均值"或统一 pooled。
7. **`17 行`质量闸描述**：实际输出 21 行（4 smoke 行无冻结基线 + 17 行带 frozen 的 delta 行）。"17 行全部 value≥floor 且 ≥frozen−2pp"指后者成立，但建议写"17 条冻结行 + 4 条 smoke 行全过"。
8. **附录 A 文件清单漏 `README.md`**（本轮 Lead 改了 README 文档索引，见 git diff，但附录未列）。
9. **broken link（既有问题，本轮主报告继续传播）**：`evaluation/reports/2026-10-22_blind40_review.md` 在 git 历史与工作区均不存在（HEAD 的 LEDGER §39 与主报告 §9 都引用）；实际证据是 `blind40_review.json` / `blind40_answers.csv`（34 correct / 6 partial / 0 wrong 可核）。
10. **日期标注**：文档/结果统一标 2026-10-22，但系统时钟与全部文件 mtime 为 2026-10-22 16:xx。不影响内容，建议核实是否跨日约定。
11. **临时文件**：16:54 快照时仓库根存在 `tmp_cat.txt`/`tmp_gapkeys.txt`/`tmp_meta.txt` 三个未跟踪临时文件；复核末次快照时已消失（并发清理），建议确认非交付残留、后续加 `.gitignore` 或清理习惯。

### P3（信息/建议，非缺陷）

- `rag_quality` 的 `suggested_refs` 只存在于返回 flags 里，`note()` 不会把它展示给用户/模型（产品行为无变化，属预留字段）；若后续要"可修复线索"需要另加消费方。
- `_MAX_FLAGS` 在"引语循环 break 后继续数字循环"时最多可到 9 条（旧实现也可能超 8，非回归；但注释/测试若承诺 ≤8 需修）。
- `[123]` 三位引用编号会被当数字二次标注（旧实现相同）；非本轮引入。
- `retrieved` 传入非 Mapping 元素会崩（生产路径 `as_dicts()` 不可达；旧实现同样崩）。
- 实验报告 §3.2 new_meeting 动作分解漏了 `recite=1`（57 keep/1 recite/7 remove/4 derived/31 meta）。

---

## 6. 仓库卫生

- **`git status --short` 与交付对应**：Lead 范围 = `README.md`、`LEDGER.md`、3 个 product services、2 个 tests；交付物 = 4 份 docs、`quality_gap_report.py/json`、`claim_repair.py`、实验报告、2 个 claim_repair JSON。其余（`scripts/moss_*`、`evaluation/asr/first_test/moss_direct/`、`task_*.moss_direct.json`）属 asr-runner 进行中任务，**非 Lead 范围且未被我触碰**。
- **LEDGER 只追加**：`git diff --numstat evaluation/LEDGER.md` = **65 行新增 / 0 行删除**；未覆盖/删除旧结论。
- **既有证据文件未被误改**：`git status` 中无任何 `evaluation/asr/first_test/` 旧文件（`task_*.moss.json`、`cpcor/*`）、无既有 packet/report/result 的 `M`；`evaluation/asr/first_test/` 仅有 asr-runner 新增的 untracked 产物。
- **临时文件**：见 P2-11（已自行消失）。
- **并发说明**：验证期间 asr-runner 仍在写 `scripts/*` 与 `evaluation/asr/first_test/moss_direct/`，meeting-researcher 已 inactive；本报告只在写入前做最终快照，未与任何并发写入冲突。
- **验证员自身**：仅新增本报告，未改任何其它文件（所有分析脚本写在 `/tmp` / Windows 临时目录）。

---

## 7. 复现命令（全部零模型调用）

```bash
# 1) 代码审查
git status --short
git diff meetingbot/fastapi-app/services/{citations,rag_quality,chat_turn}.py
git diff meetingbot/fastapi-app/tests/
git diff --numstat evaluation/LEDGER.md

# 2) 产品 targeted 单测（合计 94）
cd meetingbot/fastapi-app
.venv/bin/python -m unittest discover -s tests -p 'test_citations.py'   # 2/2
.venv/bin/python -m unittest discover -s tests -p 'test_rag_quality.py' # 8/8
.venv/bin/python -m unittest discover -s tests -p 'test_chat_service.py'# 68/68
.venv/bin/python -m unittest discover -s tests -p 'test_chat_api.py'    # 16/16

# 3) 评测侧全量单测 + smoke + 质量闸
cd evaluation
../meetingbot/fastapi-app/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'  # Ran 330, OK
make smoke                # 退出 0，model calls made: 0
../meetingbot/fastapi-app/.venv/bin/python quality_gate.py              # QUALITY GATE: PASS

# 4) 实验数字复算（读两个 JSON 的 summary/rows，可独立重算 §3.2 全部数字）
../meetingbot/fastapi-app/.venv/bin/python - <<'PY'
import json
d=json.load(open("evaluation/results/cross30_claim_repair.json"))
s=d["summary"]
print("faith_content", s["deepseek_baseline"]["faithfulness_content"]["rate"],
      "->", s["deepseek_repaired"]["faithfulness_content"]["rate"])
print("delta", s["faithfulness_content_delta_ds"])
print("actions", s["actions"], "fallbacks", s["fallbacks"], "budget", d["budget"])
PY
```

主报告数字回源路径见 §4 表（每行来源 packet/JSON 已写明）。

---

## 8. 验证限制（不能声称的事）

1. **预注册先于运行的时序**：只能核到"文件内顺序 + JSON 自述"，无 commit/hash 锚点（P1-3）。
2. **MOSS/ASR 侧**：按纪律未跑 GPU、未重启后端，`evaluation/reports/2026-10-22_moss_asr_comparison.md` 属 asr-runner 进行中；主报告 MOSS 部分只核验了云端历史值（R8001 42.04%、R8003 27.18% 与 c3600 报告一致）、R8007 截断证据（task JSON `SUCCEEDED`、142 段、last_end=229.31s），MOSS 25.92/11.98/90.86 来自历史快照，本轮未独立重算。
3. **外部文献数字**（MOSS 22.17/24.86/−2.69、ALCE 等）未联网核验（零调用纪律），报告已标注不可外推。
4. **人工盲评**：`blind40_review.json` 可核 34/6/0，但它是 AI/subagent 盲评、非人工签名，本报告只核"数字存在且一致"，不核"评审质量"。
5. 抽查覆盖主报告 40 个关键数字而非全部 497 行；未覆盖的引用型旧数字请按 §4 相同方法回源。
