# 离线质量评测

`evaluation/` 里的脚本用于在没有人工标注平台的情况下，量化纪要生成质量。**不调用外部模型**，只读数据库里的纪要、证据、行动项和 AI 草稿。

## 评测环境

隔离评测栈（独立端口/数据卷/`.env`）见仓库根 [`eval/README.md`](../../../eval/README.md)。
DeepSeek Flash 真实评测报告：[`reports/2026-10-30-deepseek-flash.md`](reports/2026-10-30-deepseek-flash.md)。
公开评测集调研：[`public-datasets-2026-10-31.md`](public-datasets-2026-10-31.md)。
内部 golden set 标注规范：[`golden/SCHEMA.md`](golden/SCHEMA.md)。

## QA 测试集

`golden/qa_demo.json` + `qa_eval.py`：对演示会议跑 8 道可回答 + 2 道不可回答 QA，统计引用 precision/recall/F1、拒答和答案相似度：

```bash
# 在评测栈 backend 容器内（base URL 注意不要带 /api）
python -m evaluation.qa_eval --base-url http://127.0.0.1:9090 \
  --golden evaluation/golden/qa_demo.json
```

> QA 接口同一用户有 3 秒冷却，评测脚本内置 3.2s 间隔；不要连续快速请求，否则会被误判为拒答。
> 产品侧按问题复杂度动态限制引用：单点问题 1 条、多点问题 2~3 条，并按“问题+答案”的字符二元重叠排序；40 题 QA 集已覆盖 10 道多证据 + 16 道不可回答；重复运行会出现指标波动，需 3 次平均看。模型回答第一句出现“未找到/未提及”时强制改判拒答；后文的“材料未提及……”补充说明不误杀。Prompt 要求每句 claim 必须被 citations 直接支持，否则宁可拒答。

## QA claim 审核（可选，默认关闭）

```bash
# 开启生成后 claim 审核：逐条判断 claim 是否有引用支持，删除无依据 claim
QA_CLAIM_AUDIT_ENABLED=1
```

开启后每条 QA 会多一次审核模型调用，成本/延迟上升；评测栈 A/B 见 Agent scorecard。默认关闭。

## QMSum 覆盖反思

```bash
python -m evaluation.qmsum_reflect_eval
```

初答 → 找遗漏 → 合并重答；用于验证多轮反思是否真的提高 coverage（当前结论：没有）。

## QMSum 方面分解回答

```bash
python -m evaluation.qmsum_aspect_eval
```

先把问题拆成 3~6 个方面并逐方面找证据，再生成答案；用于比较 coverage/evidence 的另一条基线。

## QMSum 两阶段回答

```bash
python -m evaluation.qmsum_twostage_eval
```

先抽取 12~15 条要点与证据，再基于要点生成答案；用于对比 coverage 与 evidence F1 的取舍。

## QMSum 要点式回答

```bash
python -m evaluation.qmsum_points_eval
```

要求模型用最多 `QMSUM_MAX_POINTS` 条要点回答（默认 8），对比单段短答案的 keypoint coverage 与 evidence F1。

## QMSum 答案要点覆盖

```bash
python -m evaluation.qmsum_qa_eval
```

同时统计 answered 率、keypoint coverage、evidence F1 和 quote 合法率；脚本读取 `/tmp/qmsum`。

## QA 引用支持度

`qa_support_eval.py` 对 QA 结果逐句判断 cited evidence 是否直接支持 claim：

```bash
python -m evaluation.qa_support_eval --qa-results /tmp/qa_eval.json
```

输出 claim_support_rate、fully_supported_rate 和 unsupported samples。

## 质量门禁

```bash
python -m evaluation.quality_gate --qa <qa_eval.json> --support <qa_support.json> \
  --qmsum <qmsum_points.json> --scorecard <agent_scorecard.json>
```

把 QA citation P/R、found/拒答、claim 支持、QMSum coverage/quote、Agent 调用成功率/证据/行动项阈值固化为退出码（0=PASS, 1=FAIL）。

## Agent 产品指标 Scorecard

不跑论文 benchmark，直接统计 Agent 行为：

```bash
# 在评测栈 backend 容器内
python -m evaluation.agent_scorecard --meeting-id 1
# 默认排除演示种子日志/运行；要看全部加 --include-demo
```

指标包括：Agent 运行终态完成率、人工确认率/决策延迟、步骤成功率、模型调用成功率、调用延迟、Token、证据覆盖/合法率、行动项确认/负责人/截止/重复率、QA 调用可靠性。
首份报告：[`reports/2026-10-31-agent-scorecard.md`](reports/2026-10-31-agent-scorecard.md)。

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
