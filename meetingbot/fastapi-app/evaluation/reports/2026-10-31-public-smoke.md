# 公开集 adapter 冒烟评测（2026-10-31）

> 目的：验证 MUG / VCSUM / QMSum adapter 与指标管线；**这不是模型质量结论**，baseline 是确定性规则（lead/keyword/lexical），`oracle` 仅用于证明指标计算正确。
> 公开数据按各数据集许可本地下载，不提交到仓库。

## 1. 数据与样本

| 数据集 | 本轮使用的数据 | 说明 |
|---|---|---|
| QMSum | `data/ALL/test/Bed003.json`（1029 句）、`covid_4.json`（12 个 specific query） | 英文，MIT 许可 |
| VCSUM | `long_test.txt` 26 场 + `short_test.txt` 前 20 段 | 中文，仓库 MIT；论文 CC BY-NC-SA 4.0 |
| MUG | 按 ModelScope README 结构构造的 2 场 synthetic 样本 | 仅验证 adapter/指标；实际 MUG 需 ModelScope token，尚未下载 |

## 2. 结果

### 2.1 QMSum：evidence span 检索 + 答案相似度

| 设置 | 样本数 | evidence precision | evidence recall | evidence F1 | answer similarity |
|---|---|---|---|---|---|
| Bed003 · lexical top-10（优化后） | 6 | 0.1333 | 0.0688 | 0.0867 | 0.0715 |
| Bed003 · lexical top-5（旧） | 6 | 0.2000 | 0.0530 | 0.0814 | 0.0812 |
| Bed003 · oracle | 6 | 1.0 | 1.0 | 1.0 | 1.0 |
| covid_4 · lexical top-10（优化后） | 12 | 0.2250 | 0.1564 | 0.1412 | 0.0122 |
| covid_4 · lexical top-5（旧） | 12 | 0.3167 | 0.0990 | 0.1301 | 0.0175 |

说明：QMSum 的 `relevant_text_span` 已转成闭区间 segment 集合；oracle 全 1 说明 adapter/span 指标口径自洽。lexical baseline 只是轻量词重叠检索，召回低符合预期。

### 2.2 VCSUM：摘要 ROUGE + highlight evidence

| 设置 | 样本数 | rouge1 | rouge2 | rougeL | evidence F1 |
|---|---|---|---|---|---|
| long test · lead baseline | 26 | 0.1976 | 0.0466 | 0.1022 | 0.0 |
| long test · oracle | 26 | 1.0 | 1.0 | 1.0 | 1.0 |
| short test · lead baseline | 20 | 0.2166 | 0.0576 | 0.1246 | 0.0 |

说明：lead baseline 只是取前 3 句，且不预测 evidence，所以 evidence F1=0；oracle 1.0 证明 char-level ROUGE 与 highlight 句级 evidence 计算正确。VCSUM 真实模型摘要应替换 lead baseline 后再比较。

### 2.3 MUG：真实 Test1/Dev action item 句级检测

通过 ModelScope 只读 token 下载官方标注（仅 CSV，不含音频）：
`except_TS_test1.csv` 82 场会议（其中 50 场有 gold action）、`dev.csv` 65 场（55 场有 gold action）。

| 设置 | 样本数 | 有 action 的会议 | action precision | action recall | action F1 | key sentence F1 |
|---|---|---|---|---|---|---|
| test1 · keyword 规则（优化后） | 82 | 50 | 0.0802 | 0.0909 | 0.0740 | 0.0662 |
| test1 · keyword 规则（旧） | 82 | 50 | 0.0334 | 0.2033 | 0.0544 | 0.0662 |
| test1 · oracle | 82 | 50 | 1.0 | 1.0 | 1.0 | 1.0 |
| dev · keyword 规则（优化后） | 65 | 55 | 0.0766 | 0.0909 | 0.0654 | 0.0537 |
| dev · keyword 规则（旧） | 65 | 55 | 0.0405 | 0.3047 | 0.0650 | 0.0537 |

说明：优化后在 Dev/Test1 上比较了 “旧关键词 / 宽关键词 / 动作词 / 动作词+负责人期限” 等规则，选“强动作词或负责人/期限信号”，Test1 F1 从 0.0544 提到 0.0740；这只是规则 baseline，precision 仍低，**不是模型成绩**。oracle 全 1 证明 action gold 与 span 指标口径自洽。只有有 gold action 的会议参与 action 指标平均。

### 2.4 MUG 关键句 ROUGE（按 MUG 论文指标补测）

| 设置 | 样本数 | key sentence span F1 | key ROUGE-1 | key ROUGE-2 | key ROUGE-L |
|---|---|---|---|---|---|
| MUG Test1 · keyword baseline | 82 | 0.0662 | 0.3099 | 0.1639 | 0.1520 |
| MUG 论文 Longformer session ES | - | - | 56.5 / 60.3（avg/best） | 30.3 / 35.9 | 40.8 / 48.1 |

> 注意：这里仍是字符级 ROUGE（中英文混合快速近似），论文是 token 级；数值不能直接作为“差多少”的结论，但至少已补上 ROUGE 维度。

### 2.5 VCSUM 分词口径对齐

| 分词 | ROUGE-1 | ROUGE-2 | ROUGE-L |
|---|---|---|---|
| char（旧） | 0.1976 | 0.0466 | 0.1022 |
| jieba（可选） | 0.1715 | 0.0160 | 0.1233 |

`public_eval` 已新增 `--tokenizer jieba`；jieba 为可选依赖，未安装时自动回退 char。VCSUM 论文的 BART/Pegasus 分段摘要 R-1 约 53~60，**当前 lead baseline 仍远低于模型基线**，这里只证明分词口径可切换。

### 2.6 DeepSeek Flash 在 MUG 全部 action 会议上的直接基线

用 DeepSeek Flash（`deepseek-flash`，json_object，`reasoning_effort=low`）在 MUG Test1 **全部 50 场有 gold action 的会议**上做句级行动项识别（并发 5，原提示词 vs 更严格提示词）：

| 模型/设置 | 样本数 | action precision | action recall | action F1 |
|---|---|---|---|---|
| DeepSeek Flash · 宽松提示（最多 20 句） | 50 | 0.0770 | 0.3454 | 0.1153 |
| **DeepSeek Flash · 严格提示（最多 10 句，要求负责人/期限）** | 50 | **0.1804** | **0.4117** | **0.2243** |
| keyword 规则（同 50 场） | 50 | 0.0802 | 0.0909 | 0.0740 |
| MUG 论文 Longformer baseline | 82 | 60.18 | 66.89 | 63.14 |

说明：严格提示把 F1 从 0.1153 提到 **0.2243**，但仍远低于有监督的 Longformer baseline；这是零样本 LLM 直接标句，不是项目 action item 生成链路。样本仍只有 50 场、单次运行，不能作为最终质量结论。

### 2.7 DeepSeek Flash 在 VCSUM 真实样本上的摘要基线

取 VCSUM `long_test` **全部 26 场**（转写 4k~44k 字符），让 DeepSeek Flash 生成 150~250 字中文摘要，分别用 char/jieba 分词计算 ROUGE：

| 设置 | 样本数 | char R1 | char R2 | char RL | jieba R1 | jieba R2 | jieba RL |
|---|---|---|---|---|---|---|---|
| DeepSeek Flash | 26 | 0.3641 | 0.1336 | 0.1575 | 0.2588 | 0.0342 | 0.1495 |
| lead baseline（同 26 场） | 26 | 0.1976 | 0.0466 | 0.1022 | - | - | - |
| VCSUM 论文分段摘要 baseline | - | 53~60（R1） | 20~26 | 23~34 | - | - | - |

说明：DeepSeek 零样本摘要相对 lead 明显提升（char R1 +0.166），但仍低于 VCSUM 论文的分段摘要 baseline；论文是分段摘要、token 级 ROUGE，整体摘要/char 口径不能直接等同。样本从 5 场扩到 26 场后，趋势更可信，但仍不是论文同任务复现。


## 3. 本轮新增/验证内容

- MUG 真实标注已下载（ModelScope CC-BY-4.0，仅 Dev/Test1 CSV，无音频），`evaluation/adapters/fetch_mug.py` 可复现下载；
- `public_eval` 新增 `--predictions`，可把项目/LLM 输出按 case id 接入，不再只能跑规则 baseline；
- `public_eval` 新增 `--tokenizer char|jieba`（VCSUM ROUGE 分词）和 MUG 关键句 ROUGE 指标；
- 新增内部 golden set 标注规范 [`../golden/SCHEMA.md`](../golden/SCHEMA.md)；
- `evaluation/adapters/mug.py`：MUG JSON/JSONL/官方 CSV → 句级 transcript、action gold、key_sentence gold、topic titles；
- `evaluation/adapters/qmsum.py`：QMSum meeting JSON → 句子级 transcript、query/answer、`relevant_text_span` gold；
- `evaluation/adapters/vcsum.py`：VCSUM `overall_context`/`overall_highlights`/`long|short_test` → 展平 transcript、摘要 gold、字符级 highlight → 句级 evidence gold；
- `evaluation/public_metrics.py`：span P/R/F1、字符级 ROUGE-1/2/L、token 重叠检索、答案相似度；
- `evaluation/public_eval.py`：CLI（`lead`/`keyword`/`lexical`/`oracle`/`--predictions`）和 Markdown/JSON 输出；
- **MUG 50 场 + VCSUM 5 场 DeepSeek Flash 模型基线已跑通**（见 2.6/2.7）；
- 单测覆盖 adapter 归一化与指标，现共 **99 个测试**通过。

## 4. 局限与下一步

1. **MUG 已有规则 + LLM 真实基线**：keyword 规则在真实 Test1/Dev 上 action F1 0.0740/0.0654；DeepSeek Flash 在 Test1 50 场 action 会议上严格提示 F1 **0.2243**，仍远低于论文 Longformer 63.14；下一步扩大重复次数并尝试更强约束/模型。
2. **baseline 不是模型**：QMSum lexical、VCSUM lead、MUG keyword 只用于验证管线；`--predictions` 入口已就绪，下一步把项目真实输出/LLM 预测接进来。
   DeepSeek Flash 已在 MUG 50 场 action 会议和 VCSUM 5 场上试跑；下一步固定 prompt/抽样做 3 次重复，并报告方差。
3. **QMSum span 口径**：当前把 `relevant_text_span` 当闭区间；若后续发现官方评测按半开区间，只需改 adapter 一行。
4. **VCSUM highlight 是字符级**：adapter 转成“句子是否被标亮”的句级 evidence，精度低于原字符级 span；用于冒烟足够，正式评测建议保留 char-level span F1。
5. **不能替代内部 golden set**：公开集没有中文企业会议六字段 + owner/due/status + 引用问答；仍需 10~20 场内部标注。

## 5. 复现

```bash
# 在仓库 meetingbot/fastapi-app 目录（容器内 /app）
python -m evaluation.public_eval --dataset qmsum \
  --data /data/QMSum/data/ALL/test/Bed003.json --baseline lexical
python -m evaluation.public_eval --dataset vcsum \
  --data /data/VCSum/vcsum_data --baseline lead --kind long --limit 26
python -m evaluation.public_eval --dataset mug \
  --data /data/mug/sample.jsonl --baseline keyword
```

数据获取与许可见 [`adapters/README.md`](../adapters/README.md)。
