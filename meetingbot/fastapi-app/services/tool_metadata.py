"""工具输出契约扩展：系列/状态元数据（确定性、零模型调用）。

按 LEDGER §53 的设计：把"任何会议都成立"的结构化元数据补进工具返回与引用快照，
而不是用提示词让模型多查。开关 `CHAT_TOOL_ENRICHMENT_ENABLED` 控制是否输出
（A/B 对照用；默认关，通过后默认开）。
"""
from __future__ import annotations

import os
from typing import Any, Iterable

from models import Meeting, MeetingSeries

ENV = "CHAT_TOOL_ENRICHMENT_ENABLED"


def enrichment_enabled() -> bool:
    return (os.getenv(ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


async def series_scope_metadata(series_ids: Iterable[int],
                                *, title_limit: int = 8) -> dict[int, dict[str, Any]]:
    """series_id → {series_name, series_meeting_count, series_meeting_titles}。"""
    ids = sorted({int(item) for item in series_ids if item is not None})
    if not ids:
        return {}
    names = {row.id: row.name for row in await MeetingSeries.filter(id__in=ids)}
    meetings = await Meeting.filter(series_id__in=ids).values(
        "id", "title", "series_id", "start_time")
    grouped: dict[int, list[dict[str, Any]]] = {series_id: [] for series_id in ids}
    for row in meetings:
        grouped.setdefault(int(row["series_id"]), []).append(row)
    out: dict[int, dict[str, Any]] = {}
    for series_id in ids:
        rows = sorted(grouped.get(series_id, []), key=lambda item: str(item.get("start_time") or ""))
        out[series_id] = {
            "series_id": series_id,
            "series_name": names.get(series_id) or "",
            "series_meeting_count": len(rows),
            "series_meeting_titles": [
                f"{row['id']}·{row.get('title') or ''}" for row in rows[:title_limit]],
        }
    return out


async def series_metadata_for_meetings(
        meeting_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """meeting_id → series 元数据（含 series_id）；不在系列里返回空。"""
    ids = sorted({int(item) for item in meeting_ids if item is not None})
    if not ids:
        return {}
    rows = await Meeting.filter(id__in=ids).values("id", "series_id")
    by_meeting = {int(row["id"]): row["series_id"] for row in rows}
    series_ids = {int(value) for value in by_meeting.values() if value is not None}
    scope = await series_scope_metadata(series_ids)
    out: dict[int, dict[str, Any]] = {}
    for meeting_id, series_id in by_meeting.items():
        if series_id is None:
            continue
        meta = scope.get(int(series_id))
        if meta:
            out[meeting_id] = meta
    return out
