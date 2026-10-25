# 第二轮修复独立验证报告（2026-10-22）

> 验证员：**verifier2**（对抗性独立验证，不写功能代码）
> 验证方式：**零模型调用、零 GPU、未重启后端**；未修改任何产品/实验/结果/LEDGER/文档；**只新增本报告**。
> 所有数字均从仓库内 packet / JSON / 代码回源复算，不采信任何自述数字；篡改类实验只在 `/tmp` 副本上进行。
> 验证快照：2026-10-22 17:45–18:05（系统时钟，与文件 mtime 同）。
> 验证期间 Lead 并发修改了 `evaluation/README.md`（17:45:47）与 `meetingbot/fastapi-app/tests/test_agent.py`（17:46:46）；本报告按验证结束时的文件状态核验（见 §9）。
> 时间基线：仓库文档统一标注 2026-10-22，系统时钟同为 2026-10-22，无跨日问题。

---

## 0. 结论：**有条件 PASS**（PASS with one P1 blocking condition）

### 0.1 已通过（可采信）

1. **recall-v2 的机械实现正确**：39 条缺失事实全部可从归因产物精确对回 gold（39/39 精确匹配、0 回退），`fact_index` 确为 1-based（39/39），judgement 对齐 39/39；`fact_classes.json` 与独立重建完全一致；全部门槛五组数字逐条复算一致（§2）。
2. **可复现**：`build_fact_classes.py` 沙箱重跑（除 `generated_at`）JSON 完全一致；`analysis/recall_report.py` 沙箱重跑**字节一致**；`make recall` 前后 `recall_quality_report.json` sha256 不变（`67d9b0bb…`）。
3. **质量闸加固真实生效**：冻结基线 7 个产物 sha256 独立复算 7/7 一致；复制一字节篡改 → **FAIL（exit 1）**；再冻结 sha 后恢复正常；旧口径行、recall-v2 行、claim 行**三条都参与 PASS/FAIL**（各自独立篡改均能 FAIL）；缺基线/缺 P0 packet 以 exit 2 明确报错（§3）。
4. **CP 区间实现正确**：用独立实现（Incomplete Beta 连分式 + 二分，不用仓库代码）复核 `stats.clopper_pearson_interval`，13 组比例最大偏差 <1e-9；非 p0 行打印的 CI 与 packet `summary` 的 CP 字段 12/12 一致（§3.3）。
5. **产品代码未破坏既有契约**：citations 新字段在真实工具返回形状、None/奇怪形状、duration 0/None/字符串等边界均不崩溃，引用去重身份不变；contracts 前提校验已接入产品硬门控（`agent_service.py:254`），既有 77 项契约单测全过；claim_anchor CSV 100 条×12 列、无重复、引用可对齐、可字节复现（§4）。
6. **测试与命令全绿**：评测单测 **336/336**；产品 targeted（citations 4 / contracts 77 / rag_quality 8 / chat_service 68）+ agent 30 + chat_api 16 = **203/203**；`make smoke` exit 0 且 `model calls made: 0`、产物 sha 不变；`make recall`、`make quality-gate`（含仓库根 `make -C evaluation …`）均 exit 0，gate PASS（§5）。
7. **文档数字主体可回源**：抽查 28 个数字，26 个与产物一致；旧报告勘误已落盘；LEDGER 只新增不删除（+149/−0）；README/评测 README 链接目标均存在（§6）。

### 0.2 阻塞条件（P1，必须先处理再引用）

**`fact_classes.json` 的 23 条剔除中有 4 条被反证为「可检索」**：cross30 的 `cs06` 三条（metadata）与 new_meeting 的 `n01`（fixture）。用**同一题库的对应 packet**（round1 已证 cm01↔m001… 为同题：`eval100_scope_fixed_packet` 的 `m016` 与 `cs06` 题干+expected_facts 逐字相同、`m071` 与 `n01` 相同）核验：这 4 条事实在对应运行里 judge **判 found=true**，且 contexts 里确有其等价内容（见 §2.4 证据表）。按「可检索=正常检索材料里可以出现」的定义，它们应计入分母且判为**真检索缺口**。

修正后的读数（已在 `/tmp` 沙箱复算，仓库未改）：

| scope | 报告值 | 按反证改正 | 后果 |
|---|---:|---:|---|
| cross30 | 93.62%（44/47） | **88.00%（44/50）** | 低于 recall-v2 下限 0.90 → **质量闸 FAIL** |
| new_meeting | 90.91%（20/22） | **86.96%（20/23）** | 低于冻结基线−2pp（0.9091−0.02=0.8891）→ **质量闸 FAIL** |
| mixed100 / p0 / extended68 | 96.20 / 97.22 / 96.75 | 不变 | — |

`/tmp` 沙箱把上述 4 条改判为 retrievable 后实跑 gate：`QUALITY GATE: FAIL`（exit 1，cross30 0.880 < floor 0.900；new_meeting 0.870 < frozen−2pp）。

**因此**：`93.62%/90.91%`、主报告 §0.2 行 1 的「已修」、LEDGER §42 的「剔除后 93.62%」、README/recall 仪器报告的对应读数，当前**不能作为已定稿结论对外引用**。处置建议（二选一）：
- (a) 把 `cs06#0-2`、`n01#0` 重新归类（metadata/fixture → retrievable），重冻基线并重述数字（gate 会 FAIL，需如实报告 FAIL 并说明主指标未达标）；或
- (b) 保留现分类但**必须**先完成双人独立复核（`human_checked` 由 false → true），且文档把「已修/93.62%」改写为「换仪器、单审计归因、待双人复核」；在复核完成前不得作为对外数字。

### 0.3 问题总览（按严重度）

| 级别 | 问题 | 摘要 |
|---|---|---|
| **P1-1** | recall-v2 分类错误 4/23 | `cs06#0-2`、`n01#0` 被对应 packet 反证为可检索（found=true）；改正后 cross30 88.0% 跌破下限、gate FAIL（§2.4） |
| **P2-1** | 主仪器清单不在冻结/失败闭合范围 | `fact_classes.json` 无 sha 冻结、缺清单/空清单时 gate **静默 PASS**（「跳过 recall-v2」）；改一条分类可静默通过（沙箱 T3/T7/T8，§3.4） |
| **P2-2** | CI 与「每个比例都打 CI」不完全成立 | p0 旧口径行的 CI 取 r1 单轮、不是显示的 3 轮均值；claim 行打 `n/a`；p0 recall-v2 CI 是 3 次重复测量的合并计数（伪重复，n=36 偏窄）（§3.3） |
| **P2-3** | contracts 前提校验会误伤/误收 | 真实产物回放：19/19 条唯一 MISSING_OWNER 意见（含明确合法的「我一会跟学生跟那个家长聊」）会被丢弃；timex 漏「月底前/三季度末/周五下班前/上线前」等相对期限；线索词「由」会误收「由于时间关系…」（§4.2） |
| **P2-4** | 文档夸大（已修/定义/防护） | 「已修」「按定义不可能出现在检索上下文」「产物变而未重冻不再可能静默通过」「每个比例打印 CI」四处表述超出证据（§6.2） |
| **P3-1** | LEDGER §42 算术错误 | 「14 条缺失=11 条…另 1 fixture、2 真缺口、1 partial」=15；fixture 属 new_meeting，不在 cross30 的 14 条内（§6.3） |
| **P3-2** | `build_fact_classes.py` 回退路径 off-by-one 隐患 | 报告 `fact_index` 是 1-based，但 fallback 先拿它当 0-based 用再 ±1 猜；当前 39/39 精确匹配未触发，属潜在风险（§2.6） |
| **P3-3** | pass_k status 细节 | 代码值实为 `not_evaluable（需要同一配置重复 k 次 trial 产物）`（非纯字符串 `not_evaluable`）；无测试断言；既有报告产物未重生成、不含该字段（§5.4） |
| **P3-4** | state 引用卡片内容重复、payload 增长 | `source_text` 已含类型/负责人/截止/状态/系列/notice，StateCitationCard 又用 chips 重复展示；20 条 state 引用响应 +46.2%（+2860B）（§4.1） |
| **P3-5** | claim 锚点包 1/100 条引用无效 ref | `ca042` 的 `cited_refs` 含 `[0]`（packet 上下文从 1 开始），`cited_context` 该段为空，标注者无法判定该 ref（§4.3） |
| **P3-6** | 质量闸代码小瑕疵 | `_artifact_paths()` 为死代码且 key 前缀与 baseline 不一致（`claim_audit_cross30_fresh:` vs `claim_audit:`，当前仅用冒号后文件名故无害）；非 p0 packet 缺失时是 traceback 而非友好 exit 2；「只允许 2pp 波动」实际只拦下降不拦上升；`make recall/quality-gate` 未列入 `.PHONY`（§3.5） |

---

## 1. 验证范围与方法

- 对象：第二轮 6 组修复（recall-v2 仪器、质量闸加固、证据通道、条件规则、claim 锚点、文档刷新）。
- 方法：逐文件读代码 → 从原始 packet/gold/归因产物独立重算 → 在 `/tmp` 复制最小沙箱重跑（不写仓库）→ 篡改/反例测试 → 全量测试与命令实跑 → 文档数字回源。
- 纪律：未调用任何模型/LLM（`make smoke` 自报 `model calls made: 0`）；未启动/重启后端；未跑 GPU；**未改仓库任何文件**（最终 `git status` 与验证前一致，仅新增本报告）。
- 快照哈希（关键文件，验证结束时）：

| 文件 | sha256 |
|---|---|
| `evaluation/gold/fact_classes.json` | `456575dba9f5711538a9fe49e94cd30c438987eddb3b9eb3639179ddfb84b3aa` |
| `evaluation/baselines/quality_gate_baseline.json` | `1ac01b0b738bd76695a7f62bf8aea2311d0bf30da2401c508760913281787f04` |
| `evaluation/quality_gate.py` | `78ff6a9bd931a0da4fa5f31a4f6aad5007f3294f2b5944fc3f9307fed28e4710` |
| `evaluation/lib/recall_quality.py` | `20643400e105a06a301d4b9599c30a5c840d3302ba4f8909b2e8f8becc3cf8e3` |
| `evaluation/gold/build_fact_classes.py` | `8987326ae3020b7381cf8ce0375454e6ade88d729142ec1c8d7f804f28a185fd` |
| `evaluation/analysis/recall_report.py` | `8ae6766a12922ca593c6b0d2706bde502ffe5fd20a2be12b088837689cd83eef` |
| `evaluation/results/quality_gap_report.json` | `de8c0a32267e9c1e695ebeb599a5abff806e6bdcbedb02a4e696268993b6fb4f` |
| `evaluation/results/recall_quality_report.json` | `67d9b0bb8ac2d8875cf22101f0dfa86846f15604418d58e7a372e6713d487f71` |

---

## 2. A. recall-v2 仪器（逐项证据）

### 2.1 分类清单溯源：机械检查全部通过

对 `quality_gap_report.json#rag_attribution.missing_facts`（39 条）逐条核对：

| 检查 | 结果 |
|---|---|
| 缺失事实总数 / 分布 | 39 条：cross30_fresh 14、mixed100 15、extended68 6、new_meeting 3、p0_r2 1 |
| 精确对回 gold | **39/39 精确匹配（去首尾空白），0 条走 fallback、0 条失败** |
| `fact_index` 语义 | **1-based**：`gold.expected_facts[fact_index-1] == row.fact` 39/39；0-based 假设仅 0/39 |
| judgement 对齐 | `judgement.context.facts` 中 `index==fact_index` 的行 `found` 与 `judge_found` 一致 **39/39**（0-based 假设 10/39）→ `recall_quality.py` 的 `int(index)-1` 正确 |
| manifest sources 覆盖 | 39/39 条归因行都能在 `fact_classes.json#sets.*.sources` 按 (packet, fact) 找到对应条目 |
| 类别溯源 | 23 条剔除全部来自对应归因类别（metadata←`gold_metadata_or_absence_fact`、fixture←`gold_fixture_limitation`、conflict←`gold_transcript_conflict_or_ambiguity`），**0 条把 `retrieval_miss/partial/answer_wrote` 机械映射进剔除类** |
| 独立重建 | 用归因产物 + `CLASS_MAP` 独立重建的 cases 结构 == 现 `fact_classes.json`（`True`） |
| judge_found | 39 条全为 `false`（因此剔除后分子确实不变，「分子不变、只收窄分母」在**当前产物**上成立） |
| 类别统计 | 剔除 23 = metadata 19 + fixture 1 + conflict 3；计入 16 = 真缺口 15（retrieval_miss/partial/answer_wrote）+ judge_uncertain 1 |

> 结论：**「11 条 metadata 都来自归因产物」在机械层面成立**（来源、文本、索引、类别映射均可对回）；问题不在构建脚本的映射，而在**单审计归因内容本身**（见 §2.4）。

### 2.2 五组数字复算（与报告/基线逐项一致）

独立重算（不调用 `recall_quality.py`，直接读 packet 的 expected_facts + judgement found 后按清单剔除）：

| scope | 旧口径（复算） | recall-v2（复算） | 报告值 | 剔除 | 判定 |
|---|---:|---:|---:|---:|---|
| p0（6 题×3 轮） | 35/36 = 97.22% | 35/36 = 97.22% | 97.22%（35/36） | 0 | ✅ |
| cross30 | 44/58 = 75.86% | **44/47 = 93.62%** | 93.62%（44/47） | 11 | ✅（数字对，分类另见 §2.4） |
| new_meeting | 20/23 = 86.96% | **20/22 = 90.91%** | 90.91%（20/22） | 1 | ✅（同上） |
| mixed100 | 152/167 = 91.02% | **152/158 = 96.20%** | 96.20%（152/158） | 9 | ✅ |
| extended68（诊断） | 119/125 = 95.20% | 119/123 = 96.75% | 96.75%（119/123） | 2 | ✅ |

交叉校验：`recall_quality.py` 输出与独立重算逐 scope 完全一致；旧口径 summary 与逐 case judgement 重算全部一致（含 extended68 的 `summary_f`/`judgements.f` 跨族路径）；所有 case 的 `expected_facts` 长度 == judgement facts 数（无错位）；`skipped_cases` 全为 0；`reported delta_pp`（+17.75/+3.95/+5.18/+1.55）与重算一致。

### 2.3 可复现性

| 检查 | 结果 |
|---|---|
| 沙箱重跑 `gold/build_fact_classes.py`（复制 4 个 gold + 归因 JSON） | 与仓库 JSON **除 `generated_at` 外完全一致**；stdout：cross_30 10 cases/15 facts、eval_100_mixed 11/15、new_meeting_15 2/3、qa_seed_extended 5/6 |
| 沙箱重跑 `analysis/recall_report.py` | 输出 `recall_quality_report.json` 与仓库副本**字节一致** |
| 仓库内 `make recall` | exit 0；输出表格相同；报告文件 sha256 前后不变（`67d9b0bb…`） |

### 2.4 【P1-1】4 条被剔除事实被同题库对应 packet 反证为「可检索」

round1 已验证 cross30 与 eval100 前 30 题为**同题库两产物**（cm01↔m001…），本次利用这一点做交叉反证：对每条被剔除事实，在 `eval100_scope_fixed_packet.json`（mixed100 组装集）里找**题干与 expected_facts 逐字相同**的 case，看其 judge 判定。

| 被剔除事实 | 对应 case | 对应 packet 的 judge 结果 | 对应 contexts 内容（节选） |
|---|---|---|---|
| `cs06#0` 下午纪要三项结论：V2.0按期发布、报表导出入口隐藏 | `m016`（题干/期望事实逐字相同） | **found=true（in_context=2）** | `V2.0按原计划时间发布，报表导出功能入口在发布时隐藏，待异步改造完成后单独上线。`（kind=fact，minutes 条目快照） |
| `cs06#1` 灰度三阶段：10%、50%、全量 | `m016` | **found=true（in_context=3）** | `灰度分三阶段执行：首日10%流量、次日50%、第三日全量…` |
| `cs06#2` 对外通稿与产品公告在发布前一日18:00前定稿 | `m016` | **found=true（in_context=4）** | `对外通稿及产品公告定稿时间为发布前一日下午6点前…截止时间：发布前一日下午6点前` |
| `n01#0` 李娜（new_meeting fixture） | `m071`（题干「会议主持人是谁？」/期望事实相同） | **found=true（in_context=1）** | `[0s][李娜] 各位上午好，今天评审客户支持知识库和智能客服灰度的准备情况…`（转写 chunk） |

补充内证：
- 归因产物自己的 note 就在描述**检索缺口**而非「按定义不可能」：`cs06` 三条 note 写「结论正文不在 contexts 里」「不在 contexts」；`n01` note 写「检索又把…转写带进来，答案答成李月」——这是 retrieval miss 的表述，却被归到 metadata/fixture。
- `cs06` 三条的 expected facts 内容同时出现在 `cross30_fresh` 其它题的 contexts 里（cm01 ref=1 的转写 chunk 含「V2.0按期发布…灰度按10%、50%全量…对外通稿…发布前一日下午六点前定稿」），说明该内容属于正常检索材料。

**反证效果（/tmp 沙箱，仓库未改）**：把上述 4 条改为 retrievable 后 → cross30 **44/50 = 88.00%（<0.90 floor）**、new_meeting **20/23 = 86.96%（<0.9091−0.02）**；`quality_gate.py` 输出 **QUALITY GATE: FAIL（exit 1）**。

**影响**：cross30 的「14 条缺失=11 条不可检索」实际至少 3 条是真缺口（14 条中 2 条 retrieval_miss + 1 partial + 3 条被误剔除 = 6 条真缺口）；「93.62%/已修」与 gate PASS 均依赖这 4 条错误分类。分类来自单审计（`human_checked=false`），本轮未见独立复核证据。

### 2.5 【P2】「按定义不可能」的类别定义与产物/本轮修复自相矛盾

`fact_classes.json` rule 写「metadata：系统状态/元数据/否定性结论/tool notice，检索上下文按定义不包含」。但产物显示：

- 评测 packet 的 contexts 本身就是**引用快照**（`evaluation/judges/rag.py:370`：`citation.get("source_text") or citation.get("quote")`），而引用快照包含工具结果：跨 4 个主要 packet 统计，contexts 的 `kind` 为 chunk 728 / **fact 152 / state 24**；例如 `cs03/ct03` 的 state context 文本就是「…；负责人：王工；截止：8月30日18:00；**状态：OPEN**」。
- 本轮 `services/citations.py` 刚把 state 的 `notice`/类型/系列写入 `source_text` → **下一批 packet 的 state context 会包含工具 notice**，即 `cs09#0/1`（只返回 CONFIRMED…/PROPOSED 不得当事实）与 `cs02#1`（对应待办状态）从「按定义不可能」变成「可能」；`cs05#0`（系列归属）也可能随 series 字段进入快照而部分可检索。

因此「metadata 按定义不可能出现在检索上下文」这一**换仪器依据本身不成立**（至少对 tool-result 类事实）。建议：把定义改为「本轮检索上下文实际未含该事实」并逐条人工复核；或把类别细分为「系统元数据（任何工具都不会返回）」vs「工具结果/notice（通道可返回）」。此条与 P1-1 合并处理。

### 2.6 【P3】`build_fact_classes.py` 回退路径的 off-by-one 隐患

- 已验证归因产物的 `fact_index` 是 **1-based**（§2.1）。
- 但 `_fact_index()` 在无法精确匹配时按 `for offset in (fallback, fallback-1, fallback+1)` 取第一个合法下标，即先拿 1-based 值当 0-based 用；对「gold 文本被修订」的未来归因产物，这会**静默标注相邻事实**（甚至 ±1 猜错）。当前 39/39 精确匹配未触发，不影响现有 JSON；建议改为「找不到就跳过并报错」而非猜测。

### 2.7 p0 的重复测量与 CI

- `p0_fresh6_r1/r2/r3` 是**同一批 6 题×3 轮**（case_ids 与 expected_facts 完全相同；每轮 12 facts；successes=12/11/12）。
- 合并计数 35/36 与「逐轮率均值」相同（0.9722），但按 3 次重复测量合并算 CP CI 属于**伪重复**：门禁打印 `[0.855,0.999]`（n=36），若按独立样本 n=12 的 CP 区间应为 `[0.735,0.999]`。主报告 §1.2 与旧报告勘误已披露「p0 是重复测量/逐轮均值」，但质量闸输出本身没有 caveat，建议在 gate 输出或 README 注明「p0 CI 为合并计数、非独立样本」。

---

## 3. B. `quality_gate.py` 审查 + 篡改模拟

### 3.1 冻结基线 sha256：独立复算与篡改检测

- 独立重算 7 个产物的 sha256 与 `baselines/quality_gate_baseline.json` **7/7 一致**；验证前后（含 smoke/make 之后）仍一致。
- 单元级 `_check_artifacts`：正确基线 → `[]`；改一个 sha → `['产物已变但基线未重冻: cross30:cross30_fresh_packet.json']`；缺产物 → `['缺少产物 cross30:does_not_exist.json']`；基线无 `artifact_sha256` → `[]`（不校验）。

| 篡改实验（/tmp 副本） | 期望 | 实测 |
|---|---|---|
| T1 在 `cross30_fresh_packet.json` 末尾追加 1 字节 | FAIL | **FAIL（exit 1）** + `ARTIFACT PROVENANCE FAIL: 产物已变但基线未重冻: cross30:cross30_fresh_packet.json` |
| T2 同一字节篡改，但把基线里该产物 sha 重冻 | PASS（证明只做 sha 校验） | **PASS（exit 0）** |
| T3 改 `fact_classes.json` 一条分类（cm08#0→metadata），不重冻 | 应 FAIL（若清单被覆盖） | **PASS（exit 0）**，cross30 v2 变 0.9565 仍过线 → **静默通过** |
| T4 基线 `context_recall_retrievable.cross30=0.99` | FAIL | **FAIL（exit 1）**，`NO` 行显示 −0.054 |
| T5 把 cross30 packet 的旧口径 recall 改为 35/58 且重冻 sha | FAIL | **FAIL（exit 1）**，旧口径行 `0.603 <0.700` |
| T6 把 claim 审计 `not_in_any_context_rate` 改为 0.30 且重冻 sha | FAIL | **FAIL（exit 1）** |
| T7 删除 `fact_classes.json` | — | **PASS（exit 0）**，仅打印 4 行「跳过 recall-v2 … 清单缺」 |
| T8 `fact_classes.json` 改为 `{"sets":{}}` | — | **PASS（exit 0）**，同上 |
| T9 基线删除 `context_recall_retrievable` 键 | — | **PASS（exit 0）**，v2 只按 floor 判、无冻结回归 |
| 缺基线 / 缺 P0 packet | 清晰报错 | exit 2，`缺少冻结基线: …` / `缺少 P0 packet: […]` |

### 3.2 双行都参与 PASS（任务要求项）

由 T4（recall-v2 冻结回归 → FAIL）、T5（旧口径 → FAIL）、T6（claim → FAIL）共同证明：旧口径行、recall-v2 行、claim 行**都会独立导致 FAIL**，不存在「双行只打印不判定」。当前仓库状态实跑 `make quality-gate` → **PASS**，25 行指标（4 smoke + 16 旧口径冻结 + 4 recall-v2 + 1 claim）全部 `yes`。

### 3.3 CP 区间的正确性（含一处瑕疵）

- 独立实现（Incomplete Beta 连分式 `betai` + 二分）复核：44/47 `[0.824608,0.986638]`、35/36 `[0.854711,0.999297]`、20/22 `[0.708387,0.988794]`、152/158 `[0.919180,0.985939]`、119/123 `[0.918827,0.991070]`、44/58 `[0.628310,0.861299]`、20/23 `[0.664111,0.972248]`、152/167 `[0.856184,0.948854]`、12/12 `[0.735352,1.0]` 等 13 组与 `lib/stats.py` 最大偏差 <1e-9 → **CP 算法正确**。
- 非 p0 行打印的 CI 与 packet `summary` 的 CP 字段 **12/12 一致**。
- **瑕疵**：p0 旧口径 4 行的 CI 取自 `_summary(P0_PATHS[0])`（**只有 r1 一轮**），不是显示值（3 轮均值）；例如 context_recall 显示 0.9722 [0.735,1.000]（=r1 12/12 的 CI）、context_precision 显示 0.6423 [0.421,0.737]（=r1 24/41=0.5854 的 CI）；而 3 轮合并的 CP 应为 35/36 `[0.855,0.999]`、79/123 `[0.551,0.727]`。claim 行直接打印 `n/a`（0.094 是比例，本可给 CI）。→ 「每个比例打印 CI」不成立（25 行里 5 行不满足）。

### 3.4 【P2-1】主仪器清单不在冻结范围内、且失败不闭合

- 冻结基线只覆盖 7 个 packet/claim 产物，**没有覆盖 `evaluation/gold/fact_classes.json`（recall-v2 的分母定义本身）**，也没有覆盖 `lib/recall_quality.py`/`build_fact_classes.py`。
- 后果（沙箱实证）：T3 改一条分类、T7 删清单、T8 清空清单 → gate 均 **PASS**；T7/T8 时主指标被静默跳过，输出仍写 `QUALITY GATE: PASS`（只在 stdout 有「跳过 recall-v2」）。
- 这与 `reports/2026-10-22_recall_instrument_fix.md §4.3`「产物变了而基线没重冻的情况不再可能静默通过」的表述不符（该句只对 7 个产物成立）。建议：
  1. 把 `fact_classes.json` 的 sha256 也加入基线并校验；清单缺失/为空时 **FAIL（主指标不可评 ≠ 通过）**；
  2. 基线缺 `context_recall_retrievable` 键时同样 FAIL（现在会静默退化为只有 floor）；
  3. 在 P1-1 复核后再重冻基线。

### 3.5 其它小问题

- `_artifact_paths()` **未被调用**（死代码）；且它生成的 claim key 为 `claim_audit_cross30_fresh:claim_audit_cross30_fresh.json`，而 baseline 用 `claim_audit:claim_audit_cross30_fresh.json` —— 因为 `_check_artifacts` 只用冒号后的文件名，当前无害；若未来写「重冻脚本」复用该函数会生成与基线不一致的 key。
- `main()` 只显式检查 P0 packet 存在；非 p0 packet 缺失时直接 `FileNotFoundError` traceback（无友好 exit 2）。
- docstring/note 说「只允许 2pp 波动」，代码只拒绝**下降** >2pp、不限制上升（对质量指标大概是有意，但表述不精确）。
- `Makefile` 的 `.PHONY` 未包含 `recall`/`quality-gate`（当前无同名文件，命令可正常执行；建议补上）。

---

## 4. C. 产品代码审查 + 边界测试

### 4.1 `services/citations.py`（证据通道）

用产品 venv 直接调用（零模型），构造真实工具返回形状与异常形状：

| 用例 | 结果 |
|---|---|
| `query_meeting_state` 真实形状（kind/status/owner/deadline/series/evidence/notice） | 2 条 state citation 均带 `state_type/status/series_name/owner_name/deadline_text`；`source_text` 含「类型：DECISION；负责人：王工；截止：9月1日；状态：CONFIRMED；系列：…；工具说明：只返回 CONFIRMED…」；`quote` 仍为证据原句 |
| 生产接线核实 | `kb_tools.py:725-729` 的 `_query_meeting_state` 确实在结果顶层返回 `notice` → 新逻辑会在生产触发；`get_minutes`/`list_facts` 也返回 notice（本轮只接 state，未接 minutes/list_facts 的 notice——见 P3 建议） |
| recordings duration | `4.5`/`"4.5"`/`15` → 追加「时长 X 分钟」；`0`/`None`/`""` → 不追加（与旧行为一致） |
| minutes 条目 source_text | 正文 + 槽位 + 「证据：<证据原句>」；quote 优先取证据原句（未被破坏；两条同 quote 不同 item_index 仍是两条引用） |
| 去重身份 | 未受新字段影响（`key` 仍按 chunk/state/recording/minutes 身份） |
| None item / evidence 含 None / notice 非字符串 / kind 缺失 / 空 fields / `ok:false` | 均不崩溃；`notice` 非 str 会渲染成 dict 字符串（生产不会出现，仅鲁棒性提示） |
| 前端消费 | `StateCitationCard.vue` 以 `source_text` 为卡片正文，同时 chips 已展示类型/状态/系列/负责人/截止 → **信息重复展示**；`EvidenceList` 用 `quote||source_text` 不受影响 |
| 体积 | 20 条 state 引用、真实 notice：响应 +2860B（6191→9051，+46.2%）——可接受，但列表长/多 series 时需留意 |

结论：功能正确、无契约破坏；P3 级 UI 重复与 payload 增长建议后续优化（如卡片正文改用 quote、notice 单独折叠展示）。

### 4.2 `services/contracts.py`（前提语义校验）

- 接线核实：`agent_service.py:254` 在**产品 REVIEW 步**调用 `gate_issues_by_evidence(issues, reviewed_transcript)`（硬门控，丢弃的意见原样保留在 `dropped` 并计入 step summary）；`check_issue_evidence`（报告口径）同源。规则只在 span **能定位**后追加前提检查（`contracts.py:342-356`），不会取代原「证据必须可定位」规则。
- **合法形态误伤（实测）**：
  - MISSING_OWNER：`张三：这块我来跟进`（说话人归属+承诺）、`我来负责后续跟进`（通过）、`市场部出两个人支持`、`李娜来收口`、`这个事让运营同学看下` → 前 1 与后 3 被丢弃（cue 表只有 负责/牵头/由/安排/指派/承担/owner/负责人/协作人）。
  - MISSING_DEADLINE：`月底前给结论`、`三季度末`、`周五下班前`、`上线前压测`、`发布前完成` → `timex.looks_like_a_time` 均为 False → 被丢弃；而 `下周五前提交`/`本周内完成`/`尽快完成` 通过。
  - 误收：owner cue 里的 `由` 让 `由于时间关系，会议提前结束`、`本次会议由李总主持`、`由数据看，风险可控`、`理由不充分` 通过前提检查（弱词）。
- **真实产物回放**：扫描 `evaluation/**/*.json`（1436 个文件）里带 `issue_type`+`source_span` 的产品审查意见记录 2170 条，其中 MISSING_OWNER 唯一意见 **19 条（来自 6 个 benchmark case）**，**19/19 都会被新前提检查拒绝**（如「我一会跟学生跟那个家长聊」「另外一方面，其实杨老师也组织了一些这个妇女能人…」「接下来其实杨老师就是想开展…培训」）。这些是真实模型/真实流程产物，说明 cue 表 + timex 的覆盖率明显不足，会带来**真漏报**（把合法意见丢掉）。
- 其它：MISSING_ITEM（真实记录 44 条唯一 span）**没有**登记前提检查（`ISSUE_PREMISE_CHECKS` 只有 DEADLINE/OWNER）→ 同类的“无条件触发”假阳性在 ITEM 上仍然存在（非本轮引入，但与本轮目标不对称）。
- 文档声称「把 77.8–85.7% 无条件误报从提示词问题变成数据结构问题」：**前提方向正确、已接线**，但 (a) 修复后误报率**未重测**（原数字来自 METRICS_REPORT 的旧规则/旧产物，目标 ≤20% 无新证据）；(b) 文档未讨论本次实测的**假阴性风险**。建议：把 owner 线索扩展为「说话人标签/人名/组织 + 来/想/会/组织/跟进/对接」等实体/动词模式（或直接复用 speaker-attributed 结构），timex 补「月底/季度末/下班前/上线前/发布前/国庆后」等开放/相对表达；对 MISSING_ITEM 也加 premise；文档改「已接线、待重测」。

### 4.3 `evaluation/gold/claim_anchor.py` + CSV

| 检查 | 结果 |
|---|---|
| 行数/列 | **100 行**；12 列与脚本表头完全一致（sample_id/packet/case_id/question/claim_text/cited_refs/cited_context/annotator_A_verdict/annotator_A_note/annotator_B_verdict/annotator_B_note/adjudicated_verdict） |
| 唯一性 | `ca001..ca100` 连续唯一；无重复 (case, claim)；覆盖 cross30 全部 30 题 |
| 双人标注列 | A/B 两组 verdict/note + adjudicated **全部留空**（待标注，符合设计） |
| 引文对齐 | `cited_refs` 与 claim_text 中 `[n]` 完全一致（100/100）；每个 ref 的 packet context 文本都在 `cited_context` 中可对上（除下述 1 条）；多 ref claim 43 条；`cited_context` 最长 858 字符（未触发 2400 截断） |
| 可复现 | `/tmp` 沙箱重跑 `claim_anchor.py --limit 100` → 与仓库 CSV **字节一致**（112850B） |
| 瑕疵 | `ca042` 的 `cited_refs` 含 `[0]`（packet contexts 的 ref 从 1 起），该段 `cited_context` 为空（`[0] `），标注者无法判该 ref；建议生成时过滤/标记无效 ref |

---

## 5. D. 测试与命令（全部零模型调用）

| 命令 | 结果 |
|---|---|
| `cd evaluation && ../meetingbot/fastapi-app/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | **Ran 336 tests, OK**（330 旧 + 6 个 recall-v2 新测试；3.7s） |
| `make -C evaluation smoke` | **exit 0**；输出 `model calls made: 0`；`results/metrics.json`、`results/METRICS_REPORT.md` 重写后 **sha256 不变**（确定性复现） |
| `make -C evaluation recall`（及仓库内 `make recall`） | **exit 0**；五组表格与报告一致；`recall_quality_report.json` sha256 前后不变 |
| `make -C evaluation quality-gate`（及仓库内 `make quality-gate`） | **exit 0**，`QUALITY GATE: PASS`；25 行指标全 `yes`（4 smoke + 16 旧口径 + 4 recall-v2 + 1 claim） |
| `cd meetingbot/fastapi-app && .venv/bin/python -m unittest discover -s tests -p 'test_citations.py'` | **4/4 OK**（含 duration/state notice/minutes 证据 3 个新测试） |
| 同上 `test_contracts.py` | **77 项 OK（skipped=1）**；skip 为既有「真实事故产物不在本工作区」的产物依赖测试，非本轮引入 |
| 同上 `test_rag_quality.py` / `test_chat_service.py` | **8/8 OK** / **68/68 OK** |
| 同上 `test_agent.py` / `test_chat_api.py`（额外，因 Lead 并发改了 test_agent） | **30/30 OK** / **16/16 OK** |
| pass_k 零调用复核（`regression/trajectory.py --gold … --artifact results/chat_answers.json`，输出到 /tmp） | `pass_k.status = "not_evaluable（需要同一配置重复 k 次 trial 产物）"`，`evaluated_cases=0`、`rate=null`；与报告「pass^3 不可评」一致 |

其它核实：
- 评测全量单测含新增 `evaluation/tests/test_recall_quality.py`（6 项），其中「cross30 metadata 恰为 11 条」「44/47 冻结读数」等测试通过——但注意该测试锁定的正是 §2.4 有争议的分类，**分类改判后这些测试需同步更新**。
- 除 smoke 重写同名产物（内容不变）外，所有命令均未造成 tracked 文件变化；`git status` 在验证前后一致。

---

## 6. E. 文档数字抽查与夸大检查

### 6.1 抽查 28 个数字（回源 packet/JSON/代码）

| # | 文档数字 | 回源复算 | 判定 |
|---|---|---|---|
| 1 | cross30 旧口径 75.86%（44/58） | packet summary 44/58 | ✅ |
| 2 | 旧口径 CP 62.8–86.1% | `[0.62831,0.86130]` | ✅ |
| 3 | recall-v2 93.62%（44/47） | 44/47（**分类见 §2.4，数字本身算得对**） | ⚠️ P1 |
| 4 | cs 42.1%（8/19） | 8/19=42.11% | ✅ |
| 5 | cm 94.7% / ct 90.0% | 18/19=94.74% / 18/20=90.00% | ✅ |
| 6 | p0 97.22%（35/36） | 12+11+12=35/36 | ✅ |
| 7 | p0 faith 90.75/90.71 是逐轮等权均值；pooled 90.64/90.57 | 均值 90.75/90.71、合并 155/171=90.64、144/159=90.57 | ✅（口径已注明） |
| 8 | mixed100 91.02%→96.20%（152/167→152/158） | ✅ | ✅ |
| 9 | new_meeting 86.96%→90.91%（20/23→20/22） | ✅ | ⚠️ P1（分类） |
| 10 | extended68 95.20%→96.75%（119/125→119/123） | ✅ | ✅ |
| 11 | 剔除数 11/0/1/9（+extended 2） | cross30 11、p0 0、new 1、mixed 9、extended 2 | ✅ |
| 12 | cross30 分层 pooled 81.03/66.67/84.47 | QGR mixed100_layers_recomputed：47/58、92/138、223/264 | ✅ |
| 13 | 分层逐 case 均值 81.67/74.57/78.63 | round1 已验证（本轮未重跑，沿用既有证据） | ✅（引用 round1） |
| 14 | cited-gap 18.31%（39/213）、any-gap 9.39%（20/213，CI 5.8–14.1%） | 39/213=0.18310；20/213=0.09390，CP `[0.0583,0.1413]` | ✅ |
| 15 | citation precision 65.38%（68/104，CI 55.4–74.4%） | 68/104，CP `[0.5542,0.7445]` | ✅ |
| 16 | 条目支撑率 50.0%（29/58） | 29/58 | ✅ |
| 17 | E2E 94.98%（227/239，CI 91.4–97.4%） | 227/239，CP `[0.9139,0.9738]` | ✅ |
| 18 | 调用级 98.56%（1302/1321） | ✅ | ✅ |
| 19 | MINUTES 76.00%（19/25） | metrics `by_call_type.MINUTES` 19/25 | ✅ |
| 20 | cpCER 30.09%、CER 25.04%、Δcp +5.06pp | `cpcor_report_c3600_8m.json` macro 0.3009/0.2504/0.0506 | ✅ |
| 21 | recall-v2 floors 0.95/0.90/0.85/0.92 | `RECALL_V2_FLOORS` 一致 | ✅ |
| 22 | 冻结基线 7 个产物 sha256 | 独立复算 7/7 一致 | ✅ |
| 23 | 旧口径 17 条冻结行 + recall-v2 4 行 + smoke 4 行 | gate 输出行统计 16+1+4+4 | ✅ |
| 24 | 缺失事实 39 条归因；23 条剔除、16 条计入（15 真缺口 + 1 judge_uncertain） | ✅ | ✅ |
| 25 | claim 锚点包 100 条×双人标注列 | 100×12，A/B 列留空 | ✅ |
| 26 | 产品 targeted citations 4 / contracts 77 / rag_quality 8 | 实测 4/77/8 | ✅ |
| 27 | pass^3 不可评（evaluated_cases=0） | 产物字段 0；零调用复跑 status=not_evaluable | ✅ |
| 28 | 调研（RAG/Agent）「66 个 URL」 | 文档唯一 URL 恰为 66 | ✅（「61 条参考文献」未逐条核对，不属本轮修复） |

**汇总：26/28 ✅（其中 #3、#9 数字算得对但分类前提有 P1 问题），2 项引用 round1 既有证据。**

### 6.2 夸大/不一致表述（需修正）

1. **「已修」「93.62%/90.91% 定稿」类表述**（主报告 §0.2 行 1、§0.4 第 1 条、R1 段；LEDGER §42；recall 仪器报告；README/评测 README）：在 §2.4 的 4 条分类被反证前，最多只能写「换仪器（单审计、`human_checked=false`），待双人复核；若按对应 packet 反证改正，cross30 88.0% 会低于 0.90 下限」。R1 的「**不是**跨录音检索坏了」也应改为「至少 3 条是检索缺口、11 条中部分为元数据」。
2. **「按定义不可能出现在检索上下文」**（`fact_classes.json` rule、仪器报告 §1/§2）：与产物矛盾（contexts 含 24 条 state/152 条 fact 工具结果快照；本轮 citations 修复后 notice/类型/系列将进入 source_text）——见 §2.5。
3. **「产物变了而基线没重冻 → 不再可能静默通过」**（recall 仪器报告 §4.3）：只对 7 个产物成立；改/删 `fact_classes.json` 可静默 PASS（§3.4 T3/T7/T8）。**「每个比例打印 CP CI」**（仪器报告 §4.2、主报告 §1.3、LEDGER §42）：25 行里 claim 行为 `n/a`，且 p0 旧口径 4 行 CI 不是显示估计量的区间（§3.3）。
4. **contracts「把 77.8–85.7% 无条件误报变成数据结构问题」**（主报告 §0.4 第 4 条、§8 表）：代码已接线，但 (a) 误报率未重测；(b) 真实产物回放显示 19/19 唯一 MISSING_OWNER 意见会被丢弃（假阴性风险未评估）。至少改为「已加前提门控，真/假阳性率待重测」。
5. **`pass_k` 显式 `status=not_evaluable`**：代码实际值是带中文括号的长字符串；既有 `results/*_trajectory*_report.json` 未重生成（不含 status 字段）；没有测试锁定该字段。若下游按精确字符串消费，需统一为纯 `not_evaluable`/结构化 reason。
6. **「质量指标全景与缺口分析.md（455 行）」**（主报告配套材料）：实际 482 行（P3 元数据小错）。
7. **「77 项契约单测」**（主报告 §0.4 第 4 条）：77 是 contracts 套件总数（其中 1 项产物依赖 skip），本轮新增 3 项；建议写「contracts 套件 77 项（新增 3 项前提校验）」以免读成 77 个新增测试。

### 6.3 LEDGER §42 其它事实性错误

- **算术**：§42 写「14 条缺失事实有 11 条是…另 **1 条 fixture**、2 条真检索缺口、1 条 partial」= 15；实际 cross30_fresh 的 14 条 = 11 metadata + 2 retrieval_miss + 1 partial（**fixture 1 条属于 new_meeting 的 n01**，不在 cross30 的 14 条内）。建议改为「14 = 11 metadata + 2 真缺口 + 1 partial；另有 new_meeting 1 条 fixture」。
- §42 其余关键数字（75.86→93.62、97.22→97.22、86.96→90.91、91.02→96.20、95.20→96.75、7 产物 sha、targeted 4/77/8、`human_checked=false`、`instrument_version`、旧键不变）均与产物/代码一致；但受 §2.4 分类问题影响，「93.62%/90.91%」需按 P1 处置。
- LEDGER 变更 **+149/−0**（只追加不删改），符合评测宪法；README +7/−0；旧报告 `rag_quality_gate.md`/`eval100_scale.md` 勘误段已落盘且勘误内容与实际阈值/口径一致。

---

## 7. 问题清单（按严重度，含建议）

**P1（阻塞：先改分类/文档，再谈「已修」）**

1. **recall-v2 4/23 剔除被反证为可检索**（`cs06#0-2`、`n01#0`）：改正后 cross30 88.00%（<0.90 floor）、new_meeting 86.96%（<frozen−2pp）→ 质量闸转 **FAIL**。需重新分类/双人复核；文档撤回「93.62/90.91/已修」；基线重冻；`tests/test_recall_quality.py` 的 11 条/44:47 锁定同步改。

**P2（应修，影响可信度/安全边界）**

2. 质量闸对 recall-v2 **仪器清单不冻结、不失败闭合**：加 `fact_classes.json` sha 到基线；清单缺失/空/基线缺 v2 键时 FAIL（主指标不可评≠PASS）。
3. CI 三处瑕疵：p0 旧口径 CI 取自 r1 单轮；claim 比例无 CI；p0 recall-v2 CI 为重复测量合并（伪重复）。补 p0 用「逐轮均值区间（如按轮 bootstrap 或注明伪重复）」、claim 给 CP。
4. contracts 前提校验覆盖率/误伤：真实产物 19/19 唯一 MISSING_OWNER 被丢；timex 漏相对期限；「由」误收；MISSING_ITEM 无前提。建议扩展实体/说话人/相对时间模式，并对 DEADLINE/OWNER/ITEM 三个缺失类统一 premise 框架 + 回归集（用本次 19 条+构造集做验收）。
5. 文档 4 处夸大/不准确表述（§6.2 第 1–4 条）；「已修」统一降级为「换仪器/已接线，待复核」。

**P3（信息/建议）**

6. LEDGER §42 算术（fixture 归属）。
7. `build_fact_classes.py` fallback off-by-one（改 fail-fast）。
8. pass_k status 字符串/测试/产物未同步。
9. state 卡片内容重复、state 列表 payload +46%。
10. claim 锚点 `ca042` 含无效 ref `[0]`（生成时过滤）。
11. 质量闸死代码/非 p0 缺 packet traceback/2pp 表述不对称/Makefile `.PHONY`。
12. 文档元数据：「455 行」实际 482；「77 项契约单测」表述。

---

## 8. 复现命令（全部零模型调用）

```bash
# 0) 只在 /tmp 做实验；仓库只读。分析脚本位于 /tmp/v2/（本报告描述的独立复算脚本）。
R=/home/song/code/Agent/media_agent/AIMeeting
PY=$R/meetingbot/fastapi-app/.venv/bin/python

# 1) 评测全量单测 + smoke + 两个 make 目标
cd $R/evaluation
$PY -m unittest discover -s tests -p 'test_*.py'        # Ran 336 tests, OK
make smoke                                              # exit 0, model calls made: 0
make recall                                             # exit 0, 报告 sha 不变
make quality-gate                                       # exit 0, QUALITY GATE: PASS

# 2) 产品 targeted 单测
cd $R/meetingbot/fastapi-app
for p in test_citations.py test_contracts.py test_rag_quality.py test_chat_service.py \
         test_agent.py test_chat_api.py; do
  .venv/bin/python -m unittest discover -s tests -p "$p" || exit 1
done

# 3) recall-v2 五组数字独立复算（等价于 /tmp/v2/v2_recall_verify.py）
cd $R/evaluation
$PY - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, ".")
from lib import recall_quality
EV = Path(".")
for scope, names, set_name in [
    ("p0", ["p0_fresh6_r1_packet.json","p0_fresh6_r2_packet.json","p0_fresh6_r3_packet.json"], "cross_30"),
    ("cross30", ["cross30_fresh_packet.json"], "cross_30"),
    ("new_meeting", ["new_meeting_packet.json"], "new_meeting_15"),
    ("mixed100", ["eval100_scope_fixed_packet.json"], "eval_100_mixed"),
    ("extended68", ["rag_packet_extended_tools2_final.json"], "qa_seed_extended")]:
    s = t = e = 0
    for name in names:
        p = json.loads((EV/"results"/name).read_text(encoding="utf-8"))
        m = recall_quality.retrievable_recall(p, set_name)
        s += m["successes"]; t += m["total"]; e += m["excluded"]
    print(scope, f"{s}/{t} = {s/t:.4%}", "excluded", e)
PY

# 4) 沙箱重跑：可复现性
rm -rf /tmp/v2/repro && mkdir -p /tmp/v2/repro/evaluation/{gold,results}
cp $R/evaluation/gold/build_fact_classes.py /tmp/v2/repro/evaluation/gold/
cp $R/evaluation/gold/{cross_meeting_qa_30,new_meeting_15,eval_100_mixed,qa_seed_extended}.json /tmp/v2/repro/evaluation/gold/
cp $R/evaluation/results/quality_gap_report.json /tmp/v2/repro/evaluation/results/
cd /tmp/v2/repro && $PY evaluation/gold/build_fact_classes.py
# 对比：除 generated_at 外与仓库 evaluation/gold/fact_classes.json 相同

# 5) 质量闸篡改模拟（沙箱；先在 /tmp 复制 evaluation/quality_gate.py + lib + gold/fact_classes.json
#    + baselines + 7 个 results 产物，见 /tmp/v2/v2_setup_sandboxes.sh）
cd /tmp/v2/gate && $PY evaluation/quality_gate.py            # PASS
printf ' ' >> /tmp/v2/tamper1/evaluation/results/cross30_fresh_packet.json
cd /tmp/v2/tamper1 && $PY evaluation/quality_gate.py         # FAIL: ARTIFACT PROVENANCE FAIL
# 改 fact_classes.json 一条分类 / 删清单 -> 均 PASS（P2-1 证据）；改正 4 条分类 -> FAIL

# 6) contracts 真实产物回放（统计 MISSING_OWNER 意见，/tmp/v2/v2_contracts_real.py 等价实现）
cd $R/meetingbot/fastapi-app
$PY - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, ".")
from services import contracts
records = []
def walk(node, cid=""):
    if isinstance(node, dict):
        if "issue_type" in node and ("source_span" in node or "target_text" in node):
            records.append((node.get("issue_type"), node.get("source_span") or ""))
        for v in node.values(): walk(v, cid)
    elif isinstance(node, list):
        for it in node: walk(it, cid)
for p in Path("../../evaluation").rglob("*.json"):
    try: walk(json.loads(p.read_text(encoding="utf-8", errors="replace")))
    except Exception: pass
target = [r for r in records if r[0] == "MISSING_OWNER"]
uniq = sorted(set(target))
print("MISSING_OWNER unique:", len(uniq))
print("premise-rejected:", sum(1 for t, s in uniq if not contracts._premise_has_owner(s)))
PY

# 7) pass_k 不可评状态
cd $R/evaluation && $PY regression/trajectory.py --gold gold/trajectory_27.json \
    --artifact results/chat_answers.json --output /tmp/v2/trajectory_report.json
$PY -c "import json;print(json.load(open('/tmp/v2/trajectory_report.json'))['pass_k']['status'])"
```

---

## 9. 验证限制（不能声称的事）

1. **分类语义的“正确性”只能做到交叉反证**：我未重跑产品、未重新裁判，只用同题库另一 packet 的 judge found=true 反证 4 条分类；其余 19 条剔除的合理性属人工判断，未做独立第二人复核（这正是 P1 的处置动作）。
2. **未重测 contracts 修复后的误报率**（77.8%/85.7% → ≤20% 目标）：需要产品/评测重跑或离线重放产品链路产物，本轮零调用纪律下不可得；本轮只证明了**假阴性风险**（19/19 唯一真实意见被丢）。
3. **未验证 MOSS/ASR 侧数字**（主报告 MOSS 三场宏平均 −18.47pp 等）：属 asr-runner 进行中范围，round1 亦未覆盖；本报告只核了 `cpcor_report_c3600_8m.json` 的 cpCER/CER/Δcp。
4. **未重跑 17/30 逐题差异、cross30 分层逐 case 均值**：沿用 round1 独立验证的既有证据（本轮只核 pooled 层）。
5. **质量闸“当前 PASS”≠质量达标**：基线 frozen 值就是当前 packet 读数（delta 全 0），文档已披露；本报告 §3 的 PASS 只表示“机制按设计工作”。
6. **并发写入**：验证期间 Lead 修改了 `evaluation/README.md`、`meetingbot/fastapi-app/tests/test_agent.py`（17:45/17:46）；两者已纳入最终状态核验（README 链接、test_agent 30/30 通过），除此之外未发现其它文件在验证窗口内变化；关键产物 sha 见 §1。

---

## 10. 仓库卫生

- 我未修改仓库任何文件；本报告是唯一新增文件。
- 验证期间 `make smoke` 会重写 `results/metrics.json`、`results/METRICS_REPORT.md`，但**前后 sha256 完全一致**（确定性复现），未造成实际变更。
- `git status`：19 个 tracked 修改（README、LEDGER、Makefile、评测 README、baseline、quality_gate、trajectory、2 份旧报告勘误、4 个产品 services、4 个 tests、2 个 scripts）+ 29 个 untracked（4 份 docs、recall-v2 四件套、claim 锚点、质量归因产物、MOSS/asr-runner 产物、本报告）。其中 `scripts/*moss*` 与 `evaluation/asr/first_test/*moss_direct*` 属 asr-runner 并行工作，**非本轮 Lead 范围**，我未触碰。
