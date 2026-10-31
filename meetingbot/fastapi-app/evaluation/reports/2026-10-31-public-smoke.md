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
| Bed003 · lexical baseline | 6 | 0.2000 | 0.0530 | 0.0814 | 0.0812 |
| Bed003 · oracle | 6 | 1.0 | 1.0 | 1.0 | 1.0 |
| covid_4 · lexical baseline | 12 | 0.3167 | 0.0990 | 0.1301 | 0.0175 |

说明：QMSum 的 `relevant_text_span` 已转成闭区间 segment 集合；oracle 全 1 说明 adapter/span 指标口径自洽。lexical baseline 只是轻量词重叠检索，召回低符合预期。

### 2.2 VCSUM：摘要 ROUGE + highlight evidence

| 设置 | 样本数 | rouge1 | rouge2 | rougeL | evidence F1 |
|---|---|---|---|---|---|
| long test · lead baseline | 26 | 0.1976 | 0.0466 | 0.1022 | 0.0 |
| long test · oracle | 26 | 1.0 | 1.0 | 1.0 | 1.0 |
| short test · lead baseline | 20 | 0.2166 | 0.0576 | 0.1246 | 0.0 |

说明：lead baseline 只是取前 3 句，且不预测 evidence，所以 evidence F1=0；oracle 1.0 证明 char-level ROUGE 与 highlight 句级 evidence 计算正确。VCSUM 真实模型摘要应替换 lead baseline 后再比较。

### 2.3 MUG：action item 句级检测（synthetic fixture）

| 设置 | 样本数 | action precision | action recall | action F1 | key sentence F1 |
|---|---|---|---|---|---|
| keyword baseline | 2 | 1.0 | 1.0 | 1.0 | 0.5357 |
| oracle | 2 | 1.0 | 1.0 | 1.0 | 1.0 |

说明：这是按 MUG README 结构构造的小样本，关键词刚好命中，**不能当作真实 MUG 指标**；下一步用 ModelScope token 下载真实 Dev/Test1 后重跑。

## 3. 本轮新增/验证内容

- `evaluation/adapters/mug.py`：MUG JSON/JSONL → 句级 transcript、action gold、key_sentence gold、topic titles；
- `evaluation/adapters/qmsum.py`：QMSum meeting JSON → 句子级 transcript、query/answer、`relevant_text_span` gold；
- `evaluation/adapters/vcsum.py`：VCSUM `overall_context`/`overall_highlights`/`long|short_test` → 展平 transcript、摘要 gold、字符级 highlight → 句级 evidence gold；
- `evaluation/public_metrics.py`：span P/R/F1、字符级 ROUGE-1/2/L、token 重叠检索、答案相似度；
- `evaluation/public_eval.py`：CLI（`lead`/`keyword`/`lexical`/`oracle` baseline）和 Markdown/JSON 输出；
- 单测覆盖 adapter 归一化与指标，现共 **98 个测试**通过。

## 4. 局限与下一步

1. **MUG 尚未真实下载**：需要 ModelScope 登录 token；下一步下载 Dev/Test1，用真实中文会议跑 action item 句级 P/R/F1。
2. **baseline 不是模型**：QMSum lexical 和 VCSUM lead 只用于验证管线；下一步把项目真实输出/LLM 预测接入 adapter（增加 `predictions` JSON 入口）。
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
