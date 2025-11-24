"""状态查询：把「已确认的长期记忆 + 有效待办」收成一个扁平列表，供问答工具与引用层使用。

这是 `query_meeting_state` 工具的唯一实现，也是 `services/citations.py` 的 `_from_state`
所依赖的数据形状。两条口径必须守住：

- **只返回人工确认过的记忆**（`status=CONFIRMED`）与**有效待办**；`PROPOSED` 候选永远
  不进这里——否则「人拍板才算数」这条不变量在问答链路上就破了。
- **权限只收窄不扩大**：`actor.allowed_meeting_ids`（`None`=管理员不过滤）与
  `access.visible_series_ids` 双重收窄；个人偏好只看得到自己的。

函数签名由 Lead 的工具层直接调用，不要改动顺序；返回条目里**固定给全所有键**
（没有的给 None），引用层按字段名取值，缺键会让它把引用画成死链。
"""

from __future__ import annotations

from typing import Any

from common import access, textsim
from common.times import format_datetime
from models import ActionItem, Meeting, MeetingMemory, MeetingMinutes, MeetingSeries, User
from tortoise.expressions import Q

from services import contracts

# 工具参数里的 kind → 记忆表里的 kind
MEMORY_KIND = {
    "decision": "DECISION",
    "conclusion": "CONCLUSION",
    "preference": "PREFERENCE",
}
# action_item 的状态口径：open=未完成（OPEN/BLOCKED），done=已完成，all=除取消外全部
OPEN_STATUSES = ("OPEN", "BLOCKED")
MAX_LIMIT = 30


def _content_of(item: Any) -> str:
    if isinstance(item, dict):
        value = item.get("content")
        if isinstance(value, dict):
            return str(value.get("text") or "").strip()
        return str(value or "").strip()
    return str(item or "").strip()


def _memory_state(memory: MeetingMemory, *, kind: str, meeting_title: str | None,
                  minutes_id: int | None, field: str | None, item_index: int | None,
                  series_name: str | None, owner_name: str | None = None) -> dict:
    """一条已确认记忆的状态条目。键必须齐（引用层按名取值）。"""
    return {
        "kind": kind,
        "content": memory.content,
        "status": memory.status,
        "source_meeting_id": memory.source_meeting_id,
        "meeting_title": meeting_title,
        "minutes_id": minutes_id,
        "field": field,
        "item_index": item_index,
        "evidence": memory.evidence_json,
        "owner_name": owner_name,
        "deadline_text": None,
        "deadline": None,
        "series_id": memory.scope_id if memory.scope_type == "SERIES" else None,
        "series_name": series_name,
        "state_id": memory.id,
    }


def _action_state(item: ActionItem, *, meeting_title: str | None, series_name: str | None,
                  owner_name: str | None) -> dict:
    """一条待办的状态条目。来源字段/序号让引用能点回纪要里的那一条。"""
    return {
        "kind": "action_item",
        "content": item.content,
        "status": item.status,
        "source_meeting_id": item.meeting_id,
        "meeting_title": meeting_title,
        "minutes_id": item.minutes_id,
        "field": item.source_field or "pending_items",
        "item_index": item.source_index,
        "evidence": item.evidence_json,
        "owner_name": owner_name,
        "deadline_text": item.deadline_text,
        "deadline": format_datetime(item.deadline),
        "series_id": item.series_id,
        "series_name": series_name,
        "state_id": item.id,
    }


async def _series_scope(user: User, series_id: int | None) -> list[int] | None:
    """返回 series_id 过滤范围：None=不过滤（管理员），[]=无权/无可见系列。"""
    if series_id is not None:
        series = await MeetingSeries.get_or_none(id=series_id)
        if series is None or not await access.has_series_access(series, user):
            return []
        return [series.id]
    return await access.visible_series_ids(user)


async def _allowed_meetings(actor: Any, user: User) -> list[int] | None:
    """权限范围：优先用 Actor 预解析结果；允许直接传 User（此时非管理员自行解析）。"""
    if hasattr(actor, "allowed_meeting_ids"):
        return actor.allowed_meeting_ids
    if user.role == "ADMIN":
        return None
    return await access.visible_meeting_ids(user)


def _matches_query(text: str, query: str) -> bool:
    """状态工具的关键词匹配：容忍同义/改写，不要求逐字子串。

    模型常把口语化/缩略的 query 传给状态工具，而条目正文是更完整的表述；
    只做 SQL `contains` 会返回 0 条，模型就会误判“不存在”。这里在条目量很小的
    状态表上做规范化包含 + 身份判据兜底。
    """
    target = textsim.normalize(query)
    content = textsim.normalize(text)
    if not target:
        return True
    if target in content or content in target:
        return True
    if textsim.is_same_item(target, content):
        return True
    grams = textsim.content_grams(query)
    return bool(grams) and textsim.coverage(grams, textsim.content_grams(text)) >= 0.5


def _memory_item_index(minutes: MeetingMinutes | None, content: str) -> int | None:
    """在纪要 decisions 里定位这条记忆对应第几条，用于按 field+item_index 点回原条目。"""
    if minutes is None:
        return None
    decisions = contracts.upgrade_output(
        {"decisions": minutes.decisions_json or []}).get("decisions") or []
    normalized = textsim.normalize(content)
    for index, item in enumerate(decisions):
        candidate = textsim.normalize(_content_of(item))
        if not candidate:
            continue
        if candidate == normalized or textsim.is_same_item(normalized, candidate):
            return index
    return None


async def _enrich_memories(rows: list[MeetingMemory]) -> list[dict]:
    """批量补齐来源会议标题、系列名与纪要条目序号，避免逐行查询。"""
    meeting_ids = {row.source_meeting_id for row in rows if row.source_meeting_id}
    series_ids = {row.scope_id for row in rows if row.scope_type == "SERIES"}
    minutes_ids = {row.source_id for row in rows
                   if row.source_type == "MINUTES" and row.source_id}
    titles = {row.id: row.title for row in await Meeting.filter(
        id__in=meeting_ids)} if meeting_ids else {}
    series_names = {row.id: row.name for row in await MeetingSeries.filter(
        id__in=series_ids)} if series_ids else {}
    user_ids = {row.scope_id for row in rows if row.scope_type == "USER"}
    user_names = {row.id: row.name for row in await User.filter(
        id__in=user_ids)} if user_ids else {}
    minutes_map = {row.id: row for row in await MeetingMinutes.filter(
        id__in=minutes_ids)} if minutes_ids else {}
    out: list[dict] = []
    for memory in rows:
        kind = {value: key for key, value in MEMORY_KIND.items()}[memory.kind]
        minutes_id = memory.source_id if memory.source_type == "MINUTES" else None
        minutes = minutes_map.get(minutes_id) if minutes_id else None
        field = "decisions" if memory.kind in {"DECISION", "CONCLUSION"} else None
        item_index = _memory_item_index(minutes, memory.content) if minutes else None
        out.append(_memory_state(
            memory, kind=kind, meeting_title=titles.get(memory.source_meeting_id),
            minutes_id=minutes_id if minutes is not None else None,
            field=field if minutes is not None else None, item_index=item_index,
            series_name=series_names.get(memory.scope_id)
            if memory.scope_type == "SERIES" else None,
            owner_name=user_names.get(memory.scope_id)
            if memory.scope_type == "USER" else None,
        ))
    return out


async def _query_memories(actor: Any, user: User, kind: str, *,
                          query: str | None = None, series_id: int | None = None,
                          meeting_ids: list[int] | None = None,
                          include_proposed: bool = False,
                          limit: int = 10) -> list[dict]:
    allowed = await _allowed_meetings(actor, user)
    if meeting_ids is not None:
        # 参数只能收窄：与 Actor 的可见集合取交集，空交集保持空（不是"不过滤"）
        requested = {int(item) for item in meeting_ids}
        allowed = sorted(requested if allowed is None else (set(allowed) & requested))
    if kind == "preference":
        # 个人偏好只属于本人，和会议可见性无关
        filter_query = MeetingMemory.filter(
            scope_type="USER", scope_id=user.id, kind="PREFERENCE", status="CONFIRMED")
        rows = await filter_query.order_by("-create_time", "-id").limit(200 if query else limit)
        if query:
            rows = [row for row in rows if _matches_query(row.content, query)][:limit]
        return await _enrich_memories(rows)

    scope = await _series_scope(user, series_id)
    if scope == []:
        return []
    statuses = ("CONFIRMED", "PROPOSED") if include_proposed else ("CONFIRMED",)
    filter_query = MeetingMemory.filter(
        scope_type="SERIES", kind=MEMORY_KIND[kind], status__in=statuses)
    if scope is not None:
        filter_query = filter_query.filter(scope_id__in=scope or [-1])
    if allowed is not None:
        # 记忆可见性最终仍收窄到「可见会议」：手工建的、没有来源会议的条目例外
        filter_query = filter_query.filter(
            Q(source_meeting_id__in=allowed or [-1]) | Q(source_meeting_id__isnull=True))
    rows = await filter_query.order_by("-create_time", "-id").limit(200 if query else limit)
    if query:
        rows = [row for row in rows if _matches_query(row.content, query)][:limit]
    return await _enrich_memories(rows)


async def _query_action_items(actor: Any, user: User, *, query: str | None = None,
                              series_id: int | None = None, status: str = "all",
                              owner: str | None = None,
                              meeting_ids: list[int] | None = None,
                              limit: int = 10) -> list[dict]:
    allowed = await _allowed_meetings(actor, user)
    if meeting_ids is not None:
        requested = {int(item) for item in meeting_ids}
        allowed = sorted(requested if allowed is None else (set(allowed) & requested))
    scope = await _series_scope(user, series_id)
    if scope == []:
        return []
    filter_query = ActionItem.all()
    if scope is not None:
        filter_query = filter_query.filter(series_id__in=scope or [-1])
    if allowed is not None:
        filter_query = filter_query.filter(meeting_id__in=allowed or [-1])
    status_key = (status or "all").strip().lower()
    if status_key == "open":
        filter_query = filter_query.filter(status__in=OPEN_STATUSES)
    elif status_key == "done":
        filter_query = filter_query.filter(status="DONE")
    else:
        filter_query = filter_query.exclude(status="CANCELLED")
    if owner:
        # owner 是人名参数：先按姓名找出用户 id，再与原文负责人名一起匹配
        owner_ids = [row.id for row in await User.filter(name__contains=owner)]
        condition = Q(owner_name__contains=owner)
        if owner_ids:
            condition |= Q(owner_user_id__in=owner_ids)
        filter_query = filter_query.filter(condition)
    rows = await filter_query.order_by("-create_time", "-id").limit(200 if query else limit)
    if query:
        rows = [row for row in rows if _matches_query(row.content, query)][:limit]

    meeting_ids = {row.meeting_id for row in rows}
    series_ids = {row.series_id for row in rows if row.series_id}
    owner_ids = {row.owner_user_id for row in rows if row.owner_user_id}
    titles = {row.id: row.title for row in await Meeting.filter(
        id__in=meeting_ids)} if meeting_ids else {}
    series_names = {row.id: row.name for row in await MeetingSeries.filter(
        id__in=series_ids)} if series_ids else {}
    owner_names = {row.id: row.name for row in await User.filter(
        id__in=owner_ids)} if owner_ids else {}
    return [
        _action_state(
            row, meeting_title=titles.get(row.meeting_id),
            series_name=series_names.get(row.series_id),
            owner_name=owner_names.get(row.owner_user_id) or row.owner_name,
        )
        for row in rows
    ]


async def query_state(actor: Any, kind: str, query: str | None = None,
                      series_id: int | None = None, status: str = "all",
                      owner: str | None = None, limit: int = 10,
                      meeting_ids: list[int] | None = None,
                      include_proposed: bool = False) -> list[dict]:
    """查已确认状态（decision/conclusion/action_item/preference）。

    签名固定：`services/kb_tools.py` 的 `query_meeting_state` 直接按位置/关键字调用。
    """
    user = getattr(actor, "user", actor)
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 10
    size = max(1, min(size, MAX_LIMIT))
    if kind in {"decision", "conclusion", "preference"}:
        return await _query_memories(
            actor, user, kind, query=query, series_id=series_id,
            meeting_ids=meeting_ids, include_proposed=include_proposed, limit=size)
    if kind == "action_item":
        return await _query_action_items(
            actor, user, query=query, series_id=series_id, status=status,
            owner=owner, meeting_ids=meeting_ids, limit=size)
    raise ValueError(f"不支持的状态类型：{kind}")


__all__ = ["MEMORY_KIND", "MAX_LIMIT", "OPEN_STATUSES", "query_state"]
