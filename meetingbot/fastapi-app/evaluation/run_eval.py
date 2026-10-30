"""离线评测：读取数据库里的纪要/证据/行动项，计算覆盖率、证据合法率、修改率和行动项召回。

用法（在 backend 容器内执行）：
    docker compose exec -T backend python -m evaluation.run_eval
    docker compose exec -T backend python -m evaluation.run_eval --meeting-id 1 --limit 50

不调用外部模型，零成本；golden 样例放在 evaluation/golden/*.json。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from models import ActionItem, MeetingMinutes, TranscriptSegment
from settings import TORTOISE_ORM
from tortoise import Tortoise

from evaluation.metrics import (
    ARRAY_FIELDS,
    action_match,
    edit_stats,
    evidence_stats,
    field_coverage,
)

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def minutes_payload(minutes: MeetingMinutes) -> dict:
    return {
        "summary": minutes.summary or "",
        "topics": minutes.topics_json or [],
        "viewpoints": minutes.viewpoints_json or [],
        "decisions": minutes.decisions_json or [],
        "pending_items": minutes.pending_items_json or [],
        "risks": minutes.risks_json or [],
    }


async def load_segments(task_id: int) -> dict[int, dict]:
    segments = await TranscriptSegment.filter(task_id=task_id)
    return {
        segment.segment_no: {"content": segment.content, "start_ms": segment.start_ms}
        for segment in segments
    }


def load_golden() -> list[dict]:
    cases = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("cases"), list):
            cases.extend(data["cases"])
    return cases


async def evaluate(limit: int, meeting_id: int | None) -> None:
    query = MeetingMinutes.filter(status__in=["DRAFT", "CONFIRMED"])
    if meeting_id is not None:
        query = query.filter(meeting_id=meeting_id)
    minutes_rows = await query.order_by("-id").limit(limit)

    coverage_totals = {"covered": 0, "total": 0}
    evidence_totals = {"items": 0, "with_evidence": 0, "evidence": 0, "valid": 0}
    edit_rows = []
    payloads = {}
    for minutes in minutes_rows:
        payload = minutes_payload(minutes)
        payloads[minutes.id] = payload
        coverage = field_coverage(payload)
        coverage_totals["covered"] += coverage["covered"]
        coverage_totals["total"] += coverage["total"]
        segments = await load_segments(minutes.source_task_id)
        stats = evidence_stats(payload, segments)
        evidence_totals["items"] += stats["total_items"]
        evidence_totals["with_evidence"] += stats["items_with_evidence"]
        evidence_totals["evidence"] += stats["total_evidence"]
        evidence_totals["valid"] += stats["valid_evidence"]
        edit_rows.append(edit_stats(minutes.ai_draft_json, payload))

    print("# 会议纪要质量评测")
    print()
    print("## 总体指标")
    print()
    print("| 指标 | 数值 |")
    print("|---|---|")
    coverage_ratio = coverage_totals["covered"] / coverage_totals["total"] if coverage_totals["total"] else 0
    print(f"| 纪要数 | {len(minutes_rows)} |")
    print(f"| 六字段覆盖率 | {coverage_ratio:.1%} |")
    item_ratio = evidence_totals["with_evidence"] / evidence_totals["items"] if evidence_totals["items"] else 0
    valid_ratio = evidence_totals["valid"] / evidence_totals["evidence"] if evidence_totals["evidence"] else 0
    print(f"| 有证据的条目占比 | {item_ratio:.1%}（{evidence_totals['with_evidence']}/{evidence_totals['items']}） |")
    print(f"| 证据引用合法率 | {valid_ratio:.1%}（{evidence_totals['valid']}/{evidence_totals['evidence']}） |")
    available = [row for row in edit_rows if row.get("available")]
    if available:
        edit_rate = sum(row["item_edit_rate"] for row in available) / len(available)
        print(f"| 人工修改率（AI 草稿→终稿） | {edit_rate:.1%}（{len(available)} 份有草稿） |")
    else:
        print("| 人工修改率（AI 草稿→终稿） | 暂无 ai_draft_json（老数据或未生成） |")
    print()

    golden = load_golden()
    if golden:
        print("## Golden set 对照")
        print()
        print("| 用例 | 决策命中 | 行动项召回 | 证据片段命中 |")
        print("|---|---|---|---|")
        for case in golden:
            title = case.get("title") or ""
            target = next((m for m in minutes_rows if m.meeting_id == case.get("meeting_id")), None)
            if target is None:
                # 没配 meeting_id 时按会议标题匹配
                from models import Meeting

                meeting = await Meeting.filter(title=title).first()
                target = next((m for m in minutes_rows if meeting and m.meeting_id == meeting.id), None)
            if target is None:
                print(f"| {title} | 未找到纪要 | - | - |")
                continue
            payload = payloads.get(target.id, minutes_payload(target))
            expected_actions = case.get("expected_action_items") or []
            actual_items = await ActionItem.filter(minutes_id=target.id)
            actual_actions = [item.content for item in actual_items]
            action = action_match(expected_actions, actual_actions)
            expected_decisions = case.get("expected_decisions") or []
            actual_decisions = [item.get("content", "") for item in payload.get("decisions") or [] if isinstance(item, dict)]
            decision = action_match(expected_decisions, actual_decisions, threshold=0.5)
            expected_segments = set(case.get("expected_evidence_segment_nos") or [])
            actual_segments = {
                evidence.get("segment_no")
                for field in ARRAY_FIELDS
                for item in payload.get(field) or []
                if isinstance(item, dict)
                for evidence in item.get("evidence") or []
                if isinstance(evidence, dict)
            }
            hit = len(expected_segments & actual_segments) if expected_segments else 0
            print(
                f"| {title} | {decision['matched']}/{decision['expected']} | "
                f"{action['matched']}/{action['expected']} | {hit}/{len(expected_segments)} |"
            )
    print()


async def main() -> None:
    parser = argparse.ArgumentParser(description="离线会议纪要质量评测（零模型调用）")
    parser.add_argument("--limit", type=int, default=20, help="最多评测多少份纪要")
    parser.add_argument("--meeting-id", type=int, default=None, help="只评测指定会议")
    args = parser.parse_args()
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        await evaluate(max(1, min(args.limit, 200)), args.meeting_id)
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    asyncio.run(main())
