# 评测体系重构设计书（2026-09-27）

本文记录评测目录重构的**依据、决策与边界**：为什么这样设计、删了什么、
保留了什么、下一步往哪走。目录现状就是本文的实现；引用任何结论前先读
`LEDGER.md` 的存活结论登记。

---

## 一、重构动机（审计结论，2026-09-27）

对旧体系（40+ 脚本、17 节 RUN_INDEX、7.1GB 仓库）的严苛审计给出四个判词：

1. **评测纪律本身是好的，但资产结构失效**：预注册、Holm 校正、伪影自查、
   主动撤回等做法值得保留；但一次性实验仪器（oracle 臂、门控复算、验证器
   诊断…）与主线仪器混在同一目录，没有任何机制区分"还在被跑的"和"结论已
   归档的"。
2. **裁判循环未被结构隔离**：同族闭环（qwen 审查删除 → qwen 盲判删除质量）
   与机械自证（gap-fill 驱动函数 = 覆盖端点计算函数）这类问题在旧结构里
   只能靠文档纪律防，代码结构上无法防。
3. **人工金标锚点缺位**：项目自己的文献调研警告了单一 LLM 裁判的风险
   （AutoMin：GPT-4 judge 成对判别常低于 2/3），但所有关键读数仍是单裁判，
   校准用的 120 条人工标注包三周未发。
4. **精力倒挂**：自测出"上游 ASR 是第一约束"（远场 CER ≈36.5%、假设比金标
   短 20–40%），优化火力却持续投向下游纪要微调。

## 二、垂类主流指标（调研结论，作为主线选型依据）

「会议转写 + 纪要生成」垂类的业界共识是四层价值漏斗，每层不可互相替代：

| 轴 | 主流指标 | 依据 |
|---|---|---|
| 转写对 | **cpCER**（说话人归属性 CER）；单声道 CER 为其退化形式 | M2MeT/AliMeeting 官方主指标；唯一同时惩罚识别错与张冠李戴；MeetEval 现成工具；AliMeeting Eval 上 SOTA 仍在 16–30% |
| 纪要实 | **忠实度/幻觉率**（HHEM 式会议级 + 条目级声明判定） | ROUGE 结构性测不出幻觉（Liu & Liu 2008 起证据链）；会议纪要驱动行动，幻觉代价最高 |
| 纪要全 | **关键点召回**（清单式，非打分） | FineSurE：清单法与人工相关 0.677 vs 自由打分 0.295；自动指标在召回维度与人工相关性最弱 |
| 结构准 | **行动项三元组（owner/action/deadline）逐字段正确性** | 会议垂类付费点；错误可回溯到 cpCER 说话人错误 |

ROUGE 仅作回归护栏（本项目历来如此）；延迟/可靠性单列为体验层。

## 三、新结构：四轴 × 三层 × 一个金标锚

```
evaluation/
  README.md      评测宪法：三层定义、运行手册、预注册模板（唯一入口文档）
  LEDGER.md      运行账本 + 存活结论登记（接替 17 节的 RUN_INDEX）
  DESIGN.md      本文
  Makefile       四个入口：smoke / deep / gate / gold
  lib/           共享基础（无业务语义）
    stats.py           小样本统计：Clopper-Pearson、McNemar、配对 bootstrap
    model_client.py    评测侧调模型的唯一入口（key 不落盘、分段计时）
    corpora.py         语料钉定与装载（数据集目录名是证据的一部分）
  asr/           轴1（转写）
    cer_tool.py        CER 三口径（raw/no_punct/normalized）
    first_test/        首测脚本与证据（3 场远场真实链路）
  judges/        轴2/3/4 的判定仪器（都是"调模型判读"的一类）
    packets.py         盲态裁决包构建（分层抽样、打乱、匿名）
    item_judge.py      条目级四分类判定（S/D/X/C + D1–D5，冻结标准）
    meeting_level.py   会议级整篇判读（单位对齐 HHEM，配置随产物披露）
    citations.py       引用级（ALCE 式）：引用是否支撑断言
    completeness.py    关键点清单式完整性（清单只看转写）
    actions.py         AMC-AID 行动项指标 + 论文基线（中文公开锚点）
  regression/    L0 零调用回归（每次改动必跑，秒级）
    run.py             离线复算主入口（契约不变量/可定位率/可靠性，0 调用）
    grounding.py       词法代理与共享归一化（代理登记表在模块内）
    analysis.py        配对转移矩阵
    contract_checks.py 契约不变量
    reliability.py     可靠性与开销（纯计数）
  gate/          L2 变更门禁（A/B 对照，产品改动的合并前置）
    holdout_arm.py     留出集双臂 runner（经产品 API，不 monkeypatch 产品）
    holdout_batch.py   批驱动
    content_judgment.py 双臂内容级盲判（预注册端点）
    arms.py            臂间对照分析（配对统计）
  gold/          金标与裁判校准（防"AI 裁判无锚点"的唯一手段）
    annotation.py      人工标注包构建（120 条 CSV，可直接发众包）
    judge_calibration.py 裁判跨族校准（读数带 ±26% 抖动警示的来源）
    ANNOTATION_GUIDE.md 标注指南
  benchmarks/    公开集语料与生成运行器
    manifest*.json     VCSum/AliMeeting 钉住清单（dev/test26/holdout6/extra8）
    run_public_eval.py 公开集生成主运行器（预算护栏、预检、账本）
    manage_fixture.py  容器内夹具管理
    vcsum_adapter.py / alimeeting_adapter.py / download_alimeeting.py
    scoring.py / bench_common.py（分块估算唯一实现、六字段独立校验）
    alimeeting_review.py / alimeeting_annotation_score.py（轴4 人工标注流水线）
  results/       判定证据（选择性跟踪：只留 LEDGER 引用到的产物）
  tests/         评测代码自身单测（190 项，零调用，cd evaluation && unittest discover）
```

**为什么是这五个抽象**（调研结论：这五个是必要的，registry/插件/沙箱/托管
平台/多模型网格属过度设计）：**数据集**（不可变清单，benchmarks/manifests）、
**被测系统适配器**（benchmarks/run_public_eval 与 gate/holdout_arm 经产品
HTTP API 驱动真实构建，不打桩）、**打分器**（judges + regression，纯函数或
单次判定）、**运行器**（三个入口）、**结果对比**（gate/arms 配对统计绑定
commit）。反模式明确禁止：**加权综合分仪表盘**——只报按轴 pass 率与配对
delta。

## 四、删除决策表（严苛标准：说不清楚的一律清）

**标准**：一个资产留存，当且仅当它直接服务于四轴、可靠性或金标锚点，且
（仪器）仍会被跑 或（证据）被 LEDGER 引用。全部删除记录在 git 历史
（commit 639e8d2）与本表。

| 类别 | 删除项 | 理由 |
|---|---|---|
| 大体量数据 | asr_first_test/audio（9GB）、tarball、派生 mp3、fastapi-app/files（130MB）、vue/.npm-cache（45MB） | OpenSLR 可重下 / 运行时产物 / 构建缓存；git 历史已 filter-repo 剥离（3.77GiB→12.6MiB） |
| 无关内容 | 面试备考/知识图谱/项目经历三份个人文档、HANDOFF_PROMPT、output/ | 与项目无关或已被 README+LEDGER 取代 |
| 废弃评测代 | 合成集整套（cases.json、run_eval.py…）、asr_smoke | 自认"仅工程回归、不能检验 UNSUPPORTED"；连通性冒烟由产品测试覆盖 |
| 一次性实验 | oracle/passthrough 臂 runner 与分析、gate_replay、threshold_sensitivity、repeated_consensus、citation_prune_measurement | 结论已归档；问题已答；不属四轴主线 |
| 已否证/未完成 | verifier.py（MiniCheck/LettuceDetect 反事实诊断否证）、claim_metrics（未完成的重复件）、passk、modality、human_effort、aid_anchor、db_export | 死代码；服务对象已删 |
| 同族闭环产物 | oracle_deletion_quality、deletion_packet | 审计判定：qwen 审查 + qwen 盲判构成裁判循环，不足以支撑"换审查者"的方向决策（见 LEDGER 存疑条目） |
| 自证端点 | holdout_analysis.py | 覆盖端点与 gap-fill 共用同一机械（4-gram≥0.3），结构性自证；独立仪器 completeness.py 保留 |
| 过期文档 | RUN_INDEX（17 节）、METRICS_RESEARCH、GAP_REMEDIATION_RESEARCH、各旧结论文档、preflight 控制台 | 结论蒸馏进 LEDGER；全文在 git 历史 |
| 无主产物 | 57 个结果文件中未被 LEDGER 引用的 41 个（shard、旧版 packet、db dump、可再生成报告） | 证据边界清晰化 |

**保留适配三例**（删除引发的真实依赖，按"拆共用件"处理）：
run_eval 的共用工具拆入 `benchmarks/bench_common.py`；annotation 内联
`_slot_text/_minutes_payload`；run_metrics 的装载器拆为 `lib/corpora.py`。

## 五、主线协议（写进流程的防刷分机制）

调研给出的机制，落地为以下硬规则（README「评测宪法」为执行文本）：

1. **三层入口**：L0 `make smoke`（零调用，每次改动）；L1 `make deep`
   （抽样+裁判，里程碑）；L2 `make gate`（留出集 A/B，产品改动合并前置）；
   `make gold`（人工标注/裁判校准）。新增需求先问"是不是这三个入口的参数"。
2. **裁判纪律**：裁判与被测不同族；单一职责、二元优先；**一切单裁判读数
   只是方向信号**，进入决策必须 `make gold` 校准或双族一致；换裁判 = 换仪器，
   历史绝对数全部重述。
3. **holdout 记账**：dev 集（test26/dev22）可日常消费；holdout（holdout6/
   extra8）只在门禁裁决时打开并在 LEDGER 登记；被消费 5–10 次后从池子
   （191 场未消费）换血。
4. **预注册**：L2 门禁先写假设/主指标（只选一个）/阈值/样本量/何为失败，
   存入 LEDGER 后才跑；功效算不出的差异明说"检不出"，禁止说"无效果"。
5. **隔离边界**：评测不得 import 产品内部实现；共享归一化只允许"产品定义、
   评测委托并标注"的单一方向（现状：`regression/grounding.py` 委托
   `common/textsim`）——放宽归一化让读数上涨的通道（旧 R2）仍属登记在案的
   风险，触碰须先在 LEDGER 登记预期影响。
6. **机制与端点不得共用机械**：任何"产品机制由 X 驱动、端点又用 X 计算"的
   组合（本次删除 holdout_analysis 的原因）在评审时直接打回。

## 六、推进路线 v1（2026-09-27 制定）

### 6.0 路线的第一原则：**先定基板，再测机制**

ASR 上游是全部下游指标的第一约束（轴1 首测：假设比金标短 20–40%——大量内容
根本没进转写）。若先在当前基板上做下游机制门禁（②③④⑤ 的验证），ASR 一换、
转写全变、所有下游数字作废——**在即将更换的地基上装修是纯浪费**。因此路线把
工作流按依赖排序：锚定裁判 → 量准上游 → 换基板 → 四轴重基线 → 恢复机制门禁。

### 6.1 工作流与依赖

```
W1 金标锚（无依赖，纯人工）────────┐
                                    ├→ G1 裁判可用性判定
W2 cpCER 基线（重下 AliMeeting）───┤
                                    ↓
W3 ASR 选型对比（依赖 W2 的评测 harness）→ G2 换不换 ASR（预注册判据）
                                    ↓
W4 四轴重基线（依赖 W3 落定的基板；26 场 VCSum 全套重跑）
                                    ↓
W5 机制门禁恢复（①跨族复核后先开；②③④合并臂一次预注册门禁，n 按功效算）
                                    ↓
W6 常态化（smoke 进 CI、amc_aid 接产品输出、裁判卡维护、holdout 换血）
```

### 6.2 各工作流要点

**W1 金标锚**（1 天，0 API 调用）：不等众包——先用 `gold/annotation.py` 的包
在团队内做 50 条双人盲标（2–3 小时/人），算裁判（qwen3.8-max、冻结提示词、
thinking=OFF）对人工锚点的 TPR/TNR/κ；众包作为后续加密度手段。
**G1 判据**：一致率 ≥0.7 → 单裁判读数可用于回归方向；≥0.85 或接近人人一致 →
可用于门禁裁决；<0.6 → 裁判换强模型或降级为"仅双族一致才引用"。
同时落 `gold/JUDGE_CARD.md`（LLJ Card：模型/版本/温度/完整 prompt/校准数字）。

**W2 cpCER 基线**（**2026-09-27 全部完成：8/8 场 × 2 条件，产品默认已按预注册规则翻转 1200→3600**）：重下 **AliMeeting Eval 集（8 场，参考
TextGrid 公开；Test 集 20 场无参考，别浪费时间找）**，走产品真实链路，MeetEval 出
**CER / cpCER / Δcp 三件套**——说话人归属损失第一次单独可见。
**口径事实（2026-09 调研确认）**：AliMeeting 近/远场共享同一套近讲来源 TextGrid，
M2MeT 官方 cpCER 就是"远场系统 vs 该共享转写"——**我们现有金标选择符合官方惯例**
（系统性高估远场错误是口径本身属性，文献公认）；参照系：该口径下 M2MeT 基线
30–49%、冠军系统 18.79%、MOSS 0.9B CER 24.86、豆包 cpCER 37.57。
**MeetEval 实操**（`pip install meeteval`，先装 numpy+Cython）：中文按字切分后用
`cpwer`（数值等价 cpCER，绕开 CJK 分词 issue）；输入 STM（TextGrid→每说话人拼接
文本→STM，参考 M2MeT baseline 仓库 yufan-aslp/AliMeeting 的 local/ prep 脚本）；
cpCER **不需要时间对齐**（beg/end 占位即可），说话人数不匹配按删除/插入计——
所以假设输出必须先按 speaker 分组。license：CC BY-SA 4.0，内部评测可用。
预注册：主指标 cpCER，n=8 配对连续量 MDE≈6–7pp。

**W3 ASR 选型对比**（1–2 周，含自托管搭建）：同一批音频（Eval 8 场）、同一 harness。
**候选与证据（2026-09 调研落名）**：
| 路线 | 说明 | 证据与可行性 |
|---|---|---|
| **A（首推）MOSS-Transcribe-Diarize 0.9B 自托管** | 复旦 OpenMOSS，端到端说话人归属性转写 | Apache-2.0；Eval 集 cpCER 22.17 / CER 24.86 有公开实测（同口径商用 API：豆包 37.6、Gemini 41.6）；**FunASR ≥1.4.12 原生集成**（sentence_info 带 spk），Python 3.12 ✓，宿主 RTX 5070 Ti 16GB 单卡可跑（RTF<0.12）；时间戳仅秒级（产品展示够用） |
| B. 自托管级联 | FireRedASR/Paraformer-large + pyannote（+可选 MVDR 前端） | 级联最好 cpCER≈23.2（DiariZen+Paraformer）；**DiariZen 权重 CC BY-NC 不可商用**，商用走 pyannote（≈24.5）；FireRedASR 本体最准但输入 ≤60s 需切分 |
| C. DashScope 换模型 | qwen3-asr-flash-filetrans | 无公开会议数字、**未列 diarization 参数**、云端不吃 8 通道——弱化候选 |
| D. 跟踪项 | SpeakerLM（cpCER 16 无权重）、Speaker-Reasoner（开源但 30B MoE + 定制 vLLM）、MOSS-Pro API | 只跟踪不压注 |
| **正交实验：8 通道前端 A/B** | 平均下混 / best_rms / torchaudio MVDR 三种输入喂同一 ASR | M2MeT 证据：波束形成收益强依赖训练域匹配，**盲上可能负收益**——必须实测裁决 |
**G2 判据（现在写死）**：某路线 cpCER 相对当前链路改善 ≥5pp 且延迟/成本在产品预算内
→ 采纳并进入 W4；改善 <5pp → 记录后保持现状，把预算还给下游。
**W2 首测已改写优先序（2026-09-27）**：主条件 cpCER 84.7%（Δcp=+48.2pp，碎片化
标签 8 vs 4），而 c3600 单块切分 cpCER 40.0%（Δcp 仅 +4.3pp）——**跨块身份重置
一项就贡献 ≈44pp**。两个立即行动：(a) `TRANSCRIPTION_CHUNK_SECONDS=3600` 的
剩余 5 场确认（计费）后可提为默认（产品早有开关，本次即受控对比）；(b) W3 的
MOSS 对比（cpCER 22.17）在单块基线上仍差 18pp，继续按计划推进。
**已知坑**（别踩）：Whisper 系中文会议 CER 20%+；AliMeeting Test 集无参考；
给无 speaker 分组的输出算 cpCER 无意义。

**W4 四轴重基线**（一次 ~600 次调用）：新基板上重跑 26 场生成（~250 调用）
+ 四轴判定（~300 调用），全部登记进 LEDGER 并给旧数字打"旧基板"标签。
此批产物同时是未来一切对比的"前臂"。

**W5 机制门禁恢复**：
- ①审查者族覆盖：先补一次**跨族**删除质量盲判（deepseek 判 qwen 的删除，
  ~30 调用）修复 oracle 同族闭环的效度缺口，通过则设为默认配置（它只影响
  "给人看的建议"，终有人工确认门兜底，风险有界）；
- ②quote-first+③gap-fill+④owner-hint：**合并臂一次门禁**（预注册主指标=
  条目级 grounded 率，次指标必须含 completeness——上次教训：只测增益不测
  召回代价）；n 按 W4 实测方差的功效计算定（预期 ≥30 场配对，从 191 池抽取）；
- ⑤引用剪枝/引句上限：排最后——指标感知强、用户价值弱，仅当内容级端点
  一起测时才允许评估。

**W6 常态化**：`make smoke` 挂 pre-commit/CI；amc_aid 的产品输出映射规范
（轴4 的最后一块）；裁判每次更换即更新 JUDGE_CARD 并重述历史绝对数；
holdout 消费满 5 次换血。

### 6.3 功效账（跑之前先算，别再烧 n=6）

| 设计 | n | 可检出效应（MDE，α=0.05，power 0.8） |
|---|---|---|
| 会议级比例（配对 McNemar） | 26 | ≈25pp（即 23%→0% 级"奇迹效应"才显著） |
| 同上 | 50 | ≈18pp |
| 条目级比例（配对，~1400 条/26 场） | 26 场 | ≈4–5pp |
| CER/cpCER（配对连续，SD≈6pp） | 10 | ≈6pp |
| 同上 | 20 | ≈4pp |

**结论**：机制门禁类（预期效应 10–20pp）需要 n≥30 场配对或条目级口径；
ASR 对比类（预期效应 ≥10pp）n=10 即够。holdout6/extra8 级别的 n=6–8 只配做
"无退化"把关，不配做效果判定——这已写进宪法第 3 条。

### 6.4 预算概算

| 项 | 调用量 | 性质 |
|---|---|---|
| W2 cpCER 基线 | 8–10 场 ASR 计费（外部） | 一次性 |
| W3 对比 | 每条件 8–10 场 ×3–4 条件 | 一次性（自托管部分免费） |
| W4 重基线 | ≈600 | 一次性 |
| W5 ①复核 | ≈30 | 一次性 |
| W5 ②③④门禁 | 30 场 ×2 臂 × (生成+判定) ≈900 | 里程碑级 |
| W1/W6 | 0 | 人工/工程 |

### 6.5 现在不做的事（与理由）

- 机制微门禁（n=6–8 效果判定）：功效不足，已两次付学费；
- 再建裁判基础设施：金标锚（W1）之前，任何新裁判仪器都是无锚的；
- 验证器模型本地化：反事实诊断已否证两个，候选先过同一诊断再说；
- ROUGE 复辟：与人工相关性差的证据链完整，维持回归哨兵定位。
