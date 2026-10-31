# 内部 golden set schema（v1）

公开集只覆盖句级 action / 摘要 / span，**不能覆盖本项目的六字段纪要 + 逐字证据 + 结构化行动项 + 带引用问答**。本文件定义内部 golden set 的标注格式，供 10~20 场真实企业会议使用；数据本身不提交到仓库。

## 1. 文件与目录

```text
evaluation/golden/
├── demo_case.json            # 演示用，保持兼容
├── internal_001.json         # 一场会议一个文件（推荐）
└── SCHEMA.md                 # 本文件
```

## 2. 单场会议结构

```json
{
  "id": "internal_001",
  "title": "产品发布评审会",
  "source": "internal",
  "language": "zh",
  "annotators": ["A", "B"],
  "created_at": "2026-10-31",
  "transcript": [
    {"segment_no": 1, "speaker": "张晨", "start_ms": 0, "content": "..."}
  ],
  "minutes": {
    "summary": "...",
    "topics": [
      {"title": "...", "summary": "...", "evidence": [{"segment_no": 1, "quote": "..."}]}
    ],
    "viewpoints": [
      {"speaker": "张涛", "viewpoint": "...", "evidence": [{"segment_no": 2, "quote": "..."}]}
    ],
    "decisions": [
      {"content": "...", "basis": "...", "evidence": [{"segment_no": 3, "quote": "..."}]}
    ],
    "pending_items": [
      {"content": "...", "owner_suggestion": "张涛", "deadline_suggestion": "2026-11-10", "evidence": []}
    ],
    "risks": [
      {"content": "...", "level": "MEDIUM", "suggestion": "...", "evidence": []}
    ]
  },
  "action_items": [
    {
      "content": "评估异步导出的队列与限流方案",
      "owner_name": "张涛",
      "due_date": "2026-11-10",
      "status": "OPEN",
      "evidence_segment_nos": [3]
    }
  ],
  "qa": [
    {
      "question": "报表导出最终决定改成什么方案？",
      "answer": "改为异步导出，先落任务再下载文件。",
      "answerable": true,
      "citations": [
        {"segment_no": 10, "quote": "决定：报表导出改为异步导出……"}
      ]
    },
    {
      "question": "会上有没有讨论服务器采购预算？",
      "answer": "未找到足够依据。",
      "answerable": false,
      "citations": []
    }
  ]
}
```

## 3. 标注规则

- `evidence.quote` 必须能在对应 `transcript[segment_no].content` 中逐字找到（先做标点/空白归一化再匹配）；否则无效。
- 一个条目最多 2 条 evidence；summary 不强制 evidence（允许空）。
- `action_items` 中内容/owner/due 都可有空；`status` 只做候选（OPEN/DRAFT），真实状态由产品流转决定。
- `qa` 至少包含 5 个可回答 + 2 个不可回答；citations 只允许指向 transcript。
- 每场至少 2 人独立标注；争议由第三人或负责人裁决，记录到 `notes`。

## 4. 建议指标与验收线

| 指标 | 合格线（建议） | 良好线（建议） |
|---|---|---|
| 六字段覆盖率 | ≥95% | 100% |
| 证据合法率（quote 可定位） | ≥99% | 100% |
| 有证据条目占比 | ≥95% | 100% |
| 无依据声明率 | <5% | <1% |
| 行动项内容 F1 | ≥0.75 | ≥0.85 |
| 行动项 owner 准确率 | ≥0.80 | ≥0.90 |
| 行动项 due 准确率 | ≥0.70 | ≥0.85 |
| 行动项重复率 | <5% | 0% |
| QA 引用 precision | ≥0.90 | ≥0.95 |
| QA 引用 recall | ≥0.80 | ≥0.90 |
| 不可回答题拒答准确率 | ≥0.90 | 100% |
| 人工 adequacy/coverage（1-5） | ≥3.5 | ≥4.2 |

这些是产品内部验收线，不是公开论文标准；正式上线前需结合业务风险确认。

## 5. 与公开集的映射

| 公开集 | 可复用部分 |
|---|---|
| MUG | action item 句级检测；关键句/证据抽取 |
| VCSUM | 中文摘要、highlight 证据 |
| QMSum | query→evidence span 检索 |
| ELITR-Bench | QA rubric / 长上下文问答 |
| AutoMin | minutes 人工评分 rubric |

公开集用来做代理指标和回归；内部 golden set 用来判断产品是否达标，两者不能互相替代。
