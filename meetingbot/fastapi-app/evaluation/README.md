# 离线质量评测

`evaluation/` 里的脚本用于在没有人工标注平台的情况下，量化纪要生成质量。**不调用外部模型**，只读数据库里的纪要、证据、行动项和 AI 草稿。

## 评测环境

隔离评测栈（独立端口/数据卷/`.env`）见仓库根 [`eval/README.md`](../../../eval/README.md)。
DeepSeek Flash 真实评测报告：[`reports/2026-10-30-deepseek-flash.md`](reports/2026-10-30-deepseek-flash.md)。
公开评测集调研：[`public-datasets-2026-10-31.md`](public-datasets-2026-10-31.md)。
内部 golden set 标注规范：[`golden/SCHEMA.md`](golden/SCHEMA.md)。

## 公开集 adapter（MUG / VCSUM / QMSum）

公开集不提交到仓库，按各自许可下载后用 adapter 转成统一 case，再跑确定性 baseline：

```bash
# QMSum（英文 QA/证据 span）
python -m evaluation.public_eval --dataset qmsum \
  --data /path/QMSum/data/ALL/test/Bed003.json --baseline lexical
# VCSUM（中文摘要/highlight）
python -m evaluation.public_eval --dataset vcsum \
  --data /path/VCSum/vcsum_data --baseline lead --kind long --limit 26
# MUG（中文 action item / 关键句）
python -m evaluation.public_eval --dataset mug \
  --data /path/mug-sample.json --baseline keyword
```

adapter 目录见 [`adapters/README.md`](adapters/README.md)，公开集选型见
[`public-datasets-2026-10-31.md`](public-datasets-2026-10-31.md)，首轮冒烟结果见
[`reports/2026-10-31-public-smoke.md`](reports/2026-10-31-public-smoke.md)。`baseline` 仅用于验证管线，
`oracle` 是管线自检（不是模型成绩）。

## 运行

```bash
# 在运行中的 backend 容器里执行（仓库根目录）
docker compose exec -T backend python -m evaluation.run_eval
# 只评测某场会议 / 限制数量
docker compose exec -T backend python -m evaluation.run_eval --meeting-id 1 --limit 50
```

## 指标

| 指标 | 含义 |
|---|---|
| 六字段覆盖率 | summary/topics/viewpoints/decisions/pending_items/risks 非空比例 |
| 有证据的条目占比 | 数组条目里带 evidence 的比例 |
| 证据引用合法率 | evidence.quote 能在对应转写片段里逐字找到的比例 |
| 人工修改率 | `ai_draft_json`（AI 原稿）与当前终稿比较：条目改动/新增/删除占比 |
| Golden actions 召回 | 与 `golden/*.json` 里人工标注的行动项按文本相似度贪心匹配 |

## Golden 数据格式

`golden/*.json`：

```json
{
  "cases": [
    {
      "title": "产品发布评审会",
      "meeting_id": 1,
      "expected_decisions": ["..."],
      "expected_action_items": ["..."],
      "expected_evidence_segment_nos": [3, 7, 9]
    }
  ]
}
```

`meeting_id` 可选；没有时按会议标题匹配。新增用例直接放 JSON 文件即可，不需要改代码。

## 定位

这是“质量闸”的起点，不是完整实验平台：先证明指标可计算、可对比（改 Prompt/模型前后跑同一套数），再考虑接入 LLM-as-judge 或更大标注集。修改率本身来自用户的人工确认行为，数据会随使用自然积累。
