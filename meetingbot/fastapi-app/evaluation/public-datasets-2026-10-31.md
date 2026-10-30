# 公开会议评测集调研（2026-10-31）

> 目的：判断本项目（中文企业会议：转写 → 六字段纪要 → 原文证据 → 行动项 → 单场问答）能否直接复用公开评测集/公开评测标准。
> 结论先行：**有可复用的公开数据集和事实标准，但没有一个覆盖本项目全部能力的中文标准集**；需要「公开集适配 + 内部 golden set」组合。

## 1. 结论摘要

| 需求 | 是否有现成公开标准 | 建议使用 |
|---|---|---|
| 中文会议摘要/纪要 | 部分有：VCSUM（摘要/highlight）、MUG（抽取式摘要/标题） | VCSUM + MUG 做中文摘要/证据代理指标 |
| 结构化会议纪要（bullet minutes） | 有事实标准：AutoMin / ELITR Minuting Corpus（英/捷） | 借其任务定义与人工评分 rubric，不自称中文标准 |
| 中文行动项 | 部分有：MUG Action Item Detection（句子级） | MUG AID 做“是否提到行动”的检测指标；负责人/截止/状态仍需内部标注 |
| 单场会议 QA | 有：ELITR-Bench（英）、MeeQA（英）、MeetAll/MeetBench（中英，新） | ELITR-Bench 作 QA 评分方法；MeetAll 作中文 QA 参考（注意许可） |
| 逐字原文证据/引用 | 有 span 级数据：QMSum `relevant_text_span`、VCSUM highlights、MUG key sentences；**没有会议引用标准** | QMSum/VCSUM 做 span 检索指标；引用合法性仍需本项目自有校验 |
| 转写/说话人 | 有：AMI、AliMeeting/AISHELL-4 等 | 只用于 ASR/diarization 子评测 |
| 六字段 + 证据 + 行动项 + 带引用问答的端到端 | **没有** | 必须建内部中文企业 golden set |

## 2. 数据集逐项评估

### 2.1 MUG / AliMeeting4MUG（中文，首选行动项与抽取式证据）

- 654 场普通话会议（15–30 分钟，2–4 人），524 场带完整五任务标注；MUG 论文表 1 统计。
- 任务：话题分割、话题级/会议级抽取式摘要、话题标题、关键词、**行动项检测（AID）**。
- AID 是**句子级二分类**：标出“可执行任务（任务描述、时间、负责人）”的句子，评测 positive Precision/Recall/F1；**没有**结构化的 owner/due/status 字段。
- 抽取式摘要/关键句可作为“原文证据”的代理标注（但不是逐条 claim 的对齐引用）。
- 许可：ModelScope 数据集元数据为 **CC-BY-4.0**；下载需要 ModelScope SDK token（页面要求登录）。
- 适合：中文 AID 基线、中文话题/证据抽取、中文领域鲁棒性；不适合：直接评估六字段生成质量、自由问答。

来源：[MUG 论文](https://arxiv.org/abs/2303.13939)、[ModelScope 数据集页](https://www.modelscope.cn/datasets/modelscope/Alimeeting4MUG)、[基线代码](https://github.com/alibaba-damo-academy/SpokenNLP)。

### 2.2 VCSUM（中文，摘要 + 显著句/证据）

- 239 场真实中文会议，总时长 230+ 小时，平均约 14K tokens/场（论文表 2）。
- 标注：话题分割、分段 headline、分段摘要、整场摘要、**highlight 显著句**（词级 0/1，可作为抽取式证据）。
- 划分：193/25/21 场整体摘要，1076/135/136 段分段摘要（论文 §5）。
- 任务：highlight sentence extraction、segmentation-based multi-granularity summarization、abstractive meeting summarization。
- 许可：GitHub 仓库 **MIT License**（代码/仓库）；论文本身 arXiv 标注 **CC BY-NC-SA 4.0**；用于商业产品前需复核数据来源和仓库 LICENSE 适用范围。
- 适合：中文摘要质量/显著句提取；可作为“证据 span”代理；**没有行动项、问答、结构化 decision owner/due**。

来源：[VCSUM 论文](https://arxiv.org/abs/2305.05280)、[ACL Anthology](https://aclanthology.org/2023.findings-acl.377/)、[GitHub](https://github.com/hahahawu/VCSum)。

### 2.3 QMSum（英文，query-based 摘要 + 真实 span 标注）

- 232 场会议、1,808 个 query-summary 对，跨 AMI/ICSI/议会等领域（README 当前统计）。
- 每条 specific query 带 **`relevant_text_span`**（会议 transcript 的 utterance 区间），这正好可以用于评测“检索/引用到正确原文片段”。
- 任务：query-based meeting summarization；general query 无 span，specific query 有 span。
- 许可：**MIT**（仓库 LICENSE）。
- 适合：span 级证据召回/precision 指标设计、query-focused 摘要；英文，不是中文；不是短答案 QA。

来源：[QMSum 论文](https://arxiv.org/abs/2104.05938)、[GitHub](https://github.com/Yale-LILY/QMSum)。

### 2.4 AutoMin 2025 / ELITR Minuting Corpus（英文/捷克，结构化纪要事实标准）

- ELITR Minuting Corpus：179 场会议（120 英 / 59 捷），一场会议常有多份人工 minutes；会议时长 10 分钟–2 小时+。
- AutoMin 2025 两个任务：Task A 结构化 minutes 生成（英文/捷克，ELITR+EuroParlMin）；Task B 基于 transcript 的会议 QA（英文/捷克问、英文 transcript）。
- Task A 采用 **Adequacy / Fluency / Grammatical Correctness / Relevance** 四项人工评分；2025 因经费只做自动评测，使用 GPT-4 judge + ROUGE/BERTScore/BARTScore。
- 重要结论（对我们有警示）：AutoMin 2025 报告发现 GPT-4 judge 与人工文档级评分相关性差，且只能稳定区分约 3 个等级；ROUGE 的 pairwise accuracy 反而更容易超过 2/3。
- 许可：ELITR 数据在 LINDAT/CLARIN，使用前按语料页条款；ELITR-Bench 数据需从 GitHub `data.zip` 解压（密码 `utter`），具体再分发/商用需确认。
- 适合：minutes 的字段/结构设计与人工评分 rubric、英文 robustness；**不是中文**，且没有 action owner/due 结构化标注。

来源：[AutoMin 2025](https://ufal.github.io/automin-2025/)、[AutoMin 2025 Findings](https://arxiv.org/abs/2509.13814)、[ELITR Minuting Corpus](http://hdl.handle.net/11234/1-4692)。

### 2.5 ELITR-Bench（英文，会议 QA 标准参考）

- 在 ELITR English 的 dev+test2 共 18 场会议上人工构造 **271 个 question-answer** 对。
- 每题标注 question type（who/what/when/how many）和答案位置（Beginning/Middle/End/Several）；用于长上下文会议助手评测。
- 评测方法：对每个回答用 GPT-4 按 10 分 rubric 与 ground-truth 比较；论文同时用人工 crowdsourcing 验证，发现 GPT-4 judge 与人工相关但分辨力有限。
- 适合：单场会议 QA 的 prompt/评测格式、长上下文鲁棒性；**英文**，没有逐字引用标注（论文自己也把“标注 relevant passage 供 RAG 评测”列为 future work）。
- 2025 AutoMin Task B 直接复用它，是该方向的事实基线之一。

来源：[ELITR-Bench 论文](https://arxiv.org/abs/2403.20262)、[GitHub](https://github.com/utter-project/ELITR-Bench)。

### 2.6 MeetAll / MeetBench-XL（中英双语，新，最接近中文会议 QA）

- 231 场真实会议（约 140 小时），中英双语（Mandarin 70.6%，English 29.4%），1,180 个 human-verified QA turns。
- 基于 AISHELL-4 + CHiME-6 组织；MeetBench 从 Factual Accuracy、User Need、Conciseness、Structure、Completeness 五维评分；有 action-oriented task execution 指标。
- 许可：**代码 Apache 2.0；MeetAll 数据集 CC BY-NC 4.0（非商业研究）**。
- 注意：2026 年新工作（arXiv 2602.03285 / AAAI 2026 workshop），成熟度和可复现性需自行验证；它也没有逐字 citation 标注；行动项不是结构化 owner/due 标注。
- 适合：中文会议问答/human-verified QA 参考、agent 式评测；不能替代我们的内部企业会议标注。

来源：[论文](https://arxiv.org/abs/2602.03285)、[GitHub](https://github.com/huyuelin/MeetBench)、[HuggingFace](https://huggingface.co/datasets/YueLinHu/MeetAll-v2)。

### 2.7 MeeQA（英文，会议内问答）

- 422 场会议 transcript 中提取 **48K QA 对**，问题来自会议中真实提问，且包含“会议中没人回答”的无答案情况；适合评测 answerability/拒答。
- 英文，任务形态偏抽取式 QA；没有引用/结构化纪要。
- 许可/数据使用需在仓库确认（README 未写明统一 license）。

来源：[MeeQA 论文](https://arxiv.org/abs/2305.08502)、[GitHub](https://github.com/reutapel/MeeQA)。

### 2.8 MeetingBank（英文，长会议摘要）

- 1,366 场市政府会议，6 个城市，约 3,579 小时；按 agenda item 分段，每段有 reference summary；官方划分 5,169/861/862 段。
- 任务：meeting segment summarization；有人工评价的 informativeness/factuality/fluency/coherence/redundancy；没有行动项/QA。
- 许可：**CC BY-NC-ND 4.0**——非商业、**禁止演绎**；只能原样评测使用，不能改写/再分发变体，商业使用不要用。
- 适合：长会议摘要鲁棒性（英文）；不适合作为中文主评测集。

来源：[MeetingBank 官网](https://meetingbank.github.io/)、[Dataset](https://meetingbank.github.io/dataset/)、[License](https://meetingbank.github.io/license/)。

### 2.9 AMI / ICSI（英文经典会议语料）

- AMI：137 场会议（约 100 小时），产品设计场景；提供 transcript、summary、topic/action 等人工标注；**annotations 和 signals 采用 CC BY 4.0**（下载页明确）。
- ICSI：学术会议语料，经 LDC 发布（LDC2004S02 / LDC2004T04），许可按 LDC 条款（可能收费/需会员）。
- 适合：经典 baseline、英文 action/summary 对照；规模小、领域老，不能代表中文企业会议。

来源：[AMI Corpus](https://groups.inf.ed.ac.uk/ami/corpus/)、[AMI Download/License](https://groups.inf.ed.ac.uk/ami/download/)、[LDC ICSI](https://catalog.ldc.upenn.edu/LDC2004S02)。

## 3. 和本项目能力的映射

| 本项目能力 | 可复用公开集 | 具体指标 | 差距 |
|---|---|---|---|
| 转写 + 说话人 | AMI、AliMeeting/AISHELL-4 等 | CER/WER、DER | 与纪要质量不直接相关 |
| 六字段纪要（summary/topics/viewpoints/decisions/pending/risks） | VCSUM、MUG、MeetingBank、QMSum、ELITR Minuting | 六字段覆盖率、ROUGE/BERTScore、人工 adequacy/coverage/faithfulness | 没有六字段统一标注；需映射 |
| 原文证据（逐字 quote） | QMSum `relevant_text_span`、VCSUM highlights、MUG key sentences | span Precision/Recall/F1、引用合法率、unsupported claim rate | 无会议级 claim→quote 对齐标准 |
| 行动项（owner/due/status） | MUG AID、AMI 行动标注 | 句子级 AID P/R/F1 | 没有 owner/due/status 标注；结构化字段需内部集 |
| 单场问答 + 引用 | ELITR-Bench、MeeQA、MeetAll、QMSum | QA rubric/准确率、引用 P/R、拒答准确率 | 中文企业域缺失；公开集无逐字引用 |
| 纪要人工评分 | AutoMin rubric、MeetingBank human eval | Adequacy/Fluency/Grammar/Relevance | 需本地化到中文/六字段 |

## 4. 建议的评测方案（按优先级）

### P0：先接公开集做“代理指标”，验证评测管线

1. **MUG AID**：取 Dev/Test1，输入模型对会议的行动项句子检测；输出 positive P/R/F1。把本项目 `pending_items`/`decisions` 映射到“是否行动项”只需句级判断，适合现有确定性 action-item extractor 先跑。
2. **VCSUM**：取 test 的 21 场整体摘要/136 段分段摘要/显著句；计算 ROUGE-1/2/L、BERTScore、highlight span F1；本项目 `summary/topics` 可映射。
3. **QMSum**：取 test 的 query→`relevant_text_span`，评测“检索到的转写片段是否覆盖 gold span”（span recall/precision），用于验证问答/证据检索。
4. **ELITR-Bench**：取 test2 的 130 题，用 10 分 rubric 或本地等价 rubric；先把 QA 评测格式/人工校准跑通（英文，不作为中文质量结论）。

### P1：建内部中文企业 golden set（不可替代）

公开集无法覆盖“六字段 + 逐字证据 + owner/due/status + 带引用问答 + 中文企业语境”，建议：

- 选 10–20 场真实会议（不同时长/类型），按 AutoMin 的 minutes 评估维度 + 本项目六字段设计标注规范；
- 标注：六字段（可空）、每条 evidence 的 `segment_no/quote`、行动项（内容/owner/due/状态候选）、5–10 个 QA（含 2 个不可回答题）；
- 遵守 QMSum/VCSUM 的“span 对齐”思路：evidence 必须能在 transcript 中逐字定位；
- 至少 2 人独立标注并算 IAA（字段 presence Cohen's κ，action item 内容/owner 的 F1，evidence span overlap）；
- 格式沿用当前 `evaluation/golden/*.json`，增加 `source/language/task/evidence_spans/action_items` 字段。

### P2：模型对比与报告

- 固定 Prompt/分块/温度；DeepSeek Flash vs DeepSeek V4 Pro（或下一轮换别的模型），每配置 3 次，报告均值/方差；
- 指标：字段覆盖、summary 质量（ROUGE/BERTScore/人工）、证据 span F1 + 引用合法率、行动项 P/R/F1 + 重复率、QA rubric + 引用 P/R + 拒答准确率、延迟/Token/成本；
- 人工抽检 30–50 条，校准 LLM judge（AutoMin 经验：不要只信 LLM 总分，分辨力有限）。

## 5. 许可与合规注意

| 数据集 | 许可 | 使用注意 |
|---|---|---|
| MUG / AliMeeting4MUG | CC-BY-4.0（ModelScope 元数据） | 需 ModelScope token；保留署名 |
| VCSUM | 仓库 MIT；论文 CC BY-NC-SA 4.0 | 商用/再分发前复核衍生数据条款 |
| QMSum | MIT | 可较自由使用；保留版权声明 |
| ELITR / ELITR-Bench | LINDAT/CLARIN 与数据页条款（研究） | 不默认商用；按语料页确认 |
| AutoMin 2025 | 各数据集条款（ELITR/EuroParlMin） | 共享任务评测协议可参考，数据再分发需确认 |
| MeetingBank | CC BY-NC-ND 4.0 | 非商业、禁止演绎；仅原样评测 |
| AMI | CC BY 4.0（annotations+signals） | 可署名使用；视频分发另有条款 |
| ICSI | LDC 许可 | 可能收费/需会员 |
| MeetAll/MeetBench | 数据集 CC BY-NC 4.0；代码 Apache 2.0 | 非商业；新数据集需复核 |
| MeeQA | 仓库未标明统一 license | 使用前确认 |

## 6. 最终建议

1. **不要等“一个统一标准”**：不存在覆盖本项目全部能力的中文公开 benchmark；公开发表/汇报时应明确“公开集代理指标 + 内部 golden set”。
2. **中文优先接 MUG + VCSUM + MeetAll**：分别覆盖行动项检测、摘要/highlights、会议 QA；三者许可都可研究使用（注意 NC/ND）。
3. **英文方法参照 QMSum + ELITR-Bench + AutoMin/ELITR**：span 检索、QA rubric、minutes 人工评分 rubric 都是现成的事实标准。
4. **内部 golden set 是必选项**：10–20 场真实中文企业会议，按六字段 + 逐字 evidence + 结构化行动项 + 引用问答标注；这是产品可信度的核心资产。
5. **评测指标以“可复核”为中心**：span F1/引用合法率/行动项字段 F1/拒答准确率，而不是只报一个 LLM judge 分数（AutoMin 2025 已证明其分辨力有限）。

---

调研时间：2026-10-31。所有链接为公开页面；数据许可以各数据集官方页面为准，商用前请法务复核。
