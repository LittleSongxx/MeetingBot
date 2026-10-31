# 公开评测集 adapter

这里把公开会议数据集转换成统一 case，并提供确定性 baseline 冒烟跑分。**数据不提交到仓库**，按各数据集许可自行下载。

## 支持的数据集与获取方式

| 数据集 | 语言 | 获取 | 许可 |
|---|---|---|---|
| QMSum | 英文 | `git clone https://github.com/Yale-LILY/QMSum` | MIT |
| VCSUM | 中文 | `git clone https://github.com/hahahawu/VCSum` | 仓库 MIT；论文 CC BY-NC-SA 4.0（商用前复核） |
| MUG / AliMeeting4MUG | 中文 | ModelScope：本地 `eval/.env` 设 `MODELSCOPE_TOKEN`（只读），运行 `python -m evaluation.adapters.fetch_mug --out /data/mug` 下载 Dev/Test1 标注 CSV（不含音频） | CC-BY-4.0 |

## 目录与格式

### QMSum

`QMSum/data/ALL/test/*.json` 每个文件一场会议：

```json
{
  "meeting_transcripts": [{"speaker": "...", "content": "..."}],
  "general_query_list": [{"query": "...", "answer": "..."}],
  "specific_query_list": [{"query": "...", "answer": "...", "relevant_text_span": [["137", "150"]]}]
}
```

adapter 把 `meeting_transcripts` 转成句子级 transcript，`relevant_text_span` 按**闭区间**转成 `segment_no` 集合；general query 无 evidence span。

### VCSUM

`vcsum_data/` 目录需要：

- `overall_context.txt`：`{id, av_num, eos_index, context, speaker}`，`context` 是段落→句子嵌套；
- `overall_highlights.txt`：与 context 同构的字符级 0/1；
- `long_test.txt`：整场摘要；
- `short_test.txt`：分段摘要（`discussion`/`agenda`/`highlights`）。

adapter 把 context 展平为句子级 transcript；任一字符被标亮的句子作为 gold evidence segment。

### MUG

支持 ModelScope README 的 JSON 结构（单对象/数组/JSONL）：

```json
{
  "meeting_key": "M0115",
  "sentences": [{"id": 1, "speaker": "no.0", "start_time": "4.636", "end_time": "23.743", "s": "..."}],
  "action_ids": [{"id": 715}],
  "candidate": [{"key_sentence": ["4", "54"]}],
  "topic_segment_ids": [{"id": 79, "candidate": [{"title": "...", "key_sentence": ["4"]}]}]
}
```

`action_ids` 是句级行动项 gold；`key_sentence` 是抽取式摘要/证据 gold。

## 冒烟跑分

在 `meetingbot/fastapi-app` 目录（容器内工作目录 /app）执行：

```bash
# QMSum：单场会议，lexical 检索 baseline
python -m evaluation.public_eval --dataset qmsum \
  --data /data/QMSum/data/ALL/test/Bed003.json --baseline lexical

# VCSUM：整场摘要 lead baseline（不调用模型）
python -m evaluation.public_eval --dataset vcsum \
  --data /data/VCSum/vcsum_data --baseline lead --kind long --limit 26

# MUG：行动项关键词 baseline 或 oracle 管线校验
python -m evaluation.public_eval --dataset mug \
  --data /data/mug/sample.json --baseline keyword
```

baseline 说明：

- `lead`：取前 3 句作为摘要（VCSUM）；MUG 取前 10 句作为关键句；
- `keyword`：MUG 用关键词命中句作为 action item 预测；
- `lexical`：QMSum 用 query 与句子的轻量词重叠取 top-5；
- `oracle`：用 gold 作为预测，**只用于验证 adapter/指标管线**，不能当模型成绩。

输出为 Markdown 表格；加 `--output result.json` 保存原始指标；加 `--predictions predictions.json` 可直接评测
项目/模型输出（按 case id 索引，格式见 README 顶部）。VCSUM 可用 `--tokenizer jieba` 切换中文分词 ROUGE。

## 冒烟结果

2026-10-31 已用 QMSum 真实样本、VCSUM 真实 test 样本和 MUG 官方 Test1/Dev 标注跑通，结果见
[`../reports/2026-10-31-public-smoke.md`](../reports/2026-10-31-public-smoke.md)。

## 指标

- MUG：action item 句级 P/R/F1、关键句 P/R/F1；
- VCSUM：字符级 ROUGE-1/2/L、highlight evidence 句级 F1；
- QMSum：evidence span P/R/F1、答案文本相似度。

这些是**公开集代理指标**；不能替代本项目的六字段/引用/结构化行动项内部 golden set。
