"""会议待办服务：从已确认纪要生成、系列内去重结转、状态流转事件与手工维护。

待办是**跨会议**的对象：周会里同一条待办会反复出现，所以生成时必须先在同一系列内
按「归一化文本 + 负责人 + 期限」判重（复用 `common.textsim.is_same_item`）。重复出现
不新建行，只把 `last_seen_meeting_id` 推到本次会议并写一条 `CARRY_OVER` 事件——这就是
「结转」：页面能看出这条待办从哪场会一直滚到今天，而不是每次开会都长出一条新待办。

期限只保留原文（`deadline_text`）并尝试解析（`deadline`）：解析不了就只是文本，
不臆造时间。负责人先按参会人姓名匹配到员工，匹配不上才留文本，因此未注册的人也能记待办。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Any, Mapping

from common import access, textsim, timex
from common.exception_handler import CustomException
from common.times import LOCAL_TZ, format_datetime, now, to_local
from models import (
    ActionItem,
    ActionItemEvent,
    Meeting,
    MeetingMinutes,
    MeetingParticipant,
    MeetingSeries,
    TranscriptionTask,
    User,
)

from services import action_validate, contracts

# 待办状态是封闭集合，前端状态下拉框与状态机共用这一份
STATUSES = ("OPEN", "DONE", "BLOCKED", "CANCELLED")
# 「有效待办」的口径：未完成的两种状态；状态查询默认只看它们
OPEN_STATUSES = ("OPEN", "BLOCKED")

_TIME_AFTER = re.compile(r"[ T]?(\d{1,2})[:](\d{2})")
_ISO_DATE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_CN_DATE = re.compile(r"(\d{1,2})月(\d{1,2})日?")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _norm(value: Any) -> str:
    return textsim.normalize(value)


def _content_of(item: Any) -> str:
    if isinstance(item, Mapping):
        value = item.get("content")
        if isinstance(value, Mapping):
            return str(value.get("text") or "").strip()
        return str(value or "").strip()
    return str(item or "").strip()


def _slot_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("text") or "").strip()
    return str(value or "").strip()


def _evidence_of(item: Any) -> dict | None:
    if not isinstance(item, Mapping):
        return None
    evidence = item.get("evidence")
    if isinstance(evidence, Mapping):
        return dict(evidence)
    if isinstance(evidence, list):
        for entry in evidence:
            if isinstance(entry, Mapping):
                return dict(entry)
    return None


def _safe_datetime(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime | None:
    """构造本地时区时间；年月日不合法时返回 None（宁可没有期限也不存一个错日期）。"""
    try:
        return datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def _time_after(text: str, position: int) -> tuple[int, int]:
    match = _TIME_AFTER.search(text[position:position + 16])
    if match is None:
        return 0, 0
    hour, minute = int(match.group(1)), int(match.group(2))
    return (hour, minute) if hour < 24 and minute < 60 else (0, 0)


def parse_deadline(text: str | None, *, reference: datetime | None = None) -> datetime | None:
    """把期限原文解析成截止时间。

    只接受**结构明确**的写法：ISO/`YYYY-MM-DD`、`YYYY年M月D日`、`M月D日`（年份取会议
    所在年），以及能由锚点解出的相对表达（如「下周三」，交给 `common.timex`）。
    解析不出来返回 None —— 期限原文照旧保留，不用会议日期冒充。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    normalized = unicodedata.normalize("NFKC", timex.normalize_digits(raw)).strip()
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed
    except ValueError:
        pass
    ref = to_local(reference) if reference is not None else now()
    match = _ISO_DATE.search(normalized)
    if match:
        return _safe_datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)),
                              *_time_after(normalized, match.end()))
    match = _CN_DATE.search(normalized)
    if match:
        return _safe_datetime(ref.year, int(match.group(1)), int(match.group(2)),
                              *_time_after(normalized, match.end()))
    if reference is not None:
        # 相对表达必须有锚点才解析；normalized 只认完整日期，避免把「本月」猜成某一天
        parsed_time = timex.parse_time(
            raw, reference_date=to_local(reference).date().isoformat())
        if parsed_time.basis in {"stated", "derived"} and _ISO_DAY.fullmatch(
                parsed_time.normalized or ""):
            year, month, day = (int(part) for part in parsed_time.normalized.split("-"))
            return _safe_datetime(year, month, day)
    return None


def _dedupe_key(normalized: str, owner_user_id: int | None, owner_name: str | None,
                deadline: datetime | None) -> str:
    owner = str(owner_user_id) if owner_user_id is not None else _norm(owner_name or "")
    day = to_local(deadline).date().isoformat() if deadline is not None else ""
    return hashlib.sha256(f"{normalized}\x1f{owner}\x1f{day}".encode("utf-8")).hexdigest()[:32]


def _same_deadline(first: datetime | None, second: datetime | None) -> bool:
    if first is None and second is None:
        return True
    if first is None or second is None:
        return False
    return to_local(first).date() == to_local(second).date()


def _owner_key(owner_user_id: int | None, owner_name: str | None) -> Any:
    return owner_user_id if owner_user_id is not None else _norm(owner_name or "")


def _find_duplicate(rows: list[ActionItem], normalized: str, owner_user_id: int | None,
                    owner_name: str | None, deadline: datetime | None) -> ActionItem | None:
    """同系列（无系列时同会议）内判重：文本同一件事 + 负责人相同 + 期限同一天。"""
    wanted_owner = _owner_key(owner_user_id, owner_name)
    for row in rows:
        if row.status == "CANCELLED":
            continue
        if _owner_key(row.owner_user_id, row.owner_name) != wanted_owner:
            continue
        if not _same_deadline(row.deadline, deadline):
            continue
        row_norm = _norm(row.content)
        if row_norm and (normalized == row_norm or textsim.is_same_item(normalized, row_norm)):
            return row
    return None


async def _participants_by_name(meeting: Meeting) -> dict[str, User]:
    """参会人姓名（归一化）→ 用户，供负责人文本匹配。兜底把创建人/主持人算进来。"""
    user_ids = {int(item) for item in await MeetingParticipant.filter(
        meeting_id=meeting.id).values_list("user_id", flat=True)}
    user_ids.update({meeting.creator_id, meeting.host_id})
    users = await User.filter(id__in=user_ids)
    return {_norm(user.name): user for user in users if _norm(user.name)}


def _match_owner(users_by_name: dict[str, User], owner_text: str) -> User | None:
    """负责人文本 → 员工。先精确（归一化后），再双向包含且只命中一个才算。"""
    wanted = _norm(owner_text)
    if not wanted:
        return None
    if wanted in users_by_name:
        return users_by_name[wanted]
    matches = [user for name, user in users_by_name.items()
               if name and (wanted in name or name in wanted)]
    return matches[0] if len(matches) == 1 else None


async def _write_event(item_id: int, event_type: str, from_value: str | None,
                       to_value: str | None, note: str | None, actor_id: int | None,
                       *, actor_fallback: int = 0) -> None:
    await ActionItemEvent.create(
        action_item_id=item_id, event_type=event_type, from_value=from_value,
        to_value=to_value, note=note, actor_id=actor_id or actor_fallback or 0)


async def _series_items(meeting: Meeting) -> list[ActionItem]:
    """同一系列内的全部待办（含已完成的，历史结转要能找到）；无系列时退回本会议。"""
    if meeting.series_id is not None:
        return await ActionItem.filter(
            series_id=meeting.series_id).exclude(status="CANCELLED")
    return await ActionItem.filter(meeting_id=meeting.id).exclude(status="CANCELLED")


async def sync_from_minutes(minutes: MeetingMinutes, *, actor_id: int | None = None) -> dict:
    """从已确认纪要的 pending_items 生成待办（幂等：重复调用只补结转事件）。"""
    if minutes.status != "CONFIRMED":
        return {"created": 0, "carried": 0, "skipped": 0}
    meeting = await Meeting.get_or_none(id=minutes.meeting_id)
    if meeting is None:
        return {"created": 0, "carried": 0, "skipped": 0}
    items = contracts.upgrade_output(
        {"pending_items": minutes.pending_items_json or []}).get("pending_items") or []
    # 行动项槽位校验（确定性）：负责人/期限必须是转写里真的出现过的东西。
    # 没有转写（老任务/测试）时 fail-open，保持原行为。
    transcript = ""
    if minutes.source_task_id:
        task = await TranscriptionTask.get_or_none(id=minutes.source_task_id)
        transcript = str(getattr(task, "full_text", "") or "") if task is not None else ""
    users_by_name = await _participants_by_name(meeting)
    existing = await _series_items(meeting)
    actor = minutes.confirmed_by or actor_id or minutes.created_by
    created = carried = skipped = 0
    for index, item in enumerate(items):
        content = _content_of(item)
        normalized = _norm(content)
        if not normalized:
            skipped += 1
            continue
        owner_text = _slot_text(item.get("owner")) if isinstance(item, Mapping) else ""
        deadline_text = _slot_text(item.get("deadline")) if isinstance(item, Mapping) else ""
        if transcript:
            check = action_validate.validate_item(item, transcript)
            if check["drop_owner"]:
                owner_text = ""
            if check["drop_deadline"]:
                deadline_text = ""
        matched = _match_owner(users_by_name, owner_text)
        owner_user_id = matched.id if matched is not None else None
        owner_name = matched.name if matched is not None else (owner_text or None)
        deadline = parse_deadline(deadline_text, reference=meeting.start_time)
        duplicate = _find_duplicate(existing, normalized, owner_user_id, owner_name, deadline)
        if duplicate is not None:
            skipped += 1
            if duplicate.last_seen_meeting_id != meeting.id:
                previous = duplicate.last_seen_meeting_id
                await ActionItem.filter(id=duplicate.id).update(
                    last_seen_meeting_id=meeting.id)
                await _write_event(
                    duplicate.id, "CARRY_OVER", str(previous) if previous else None,
                    str(meeting.id), "再次在会议中被提及", actor, actor_fallback=actor)
                duplicate.last_seen_meeting_id = meeting.id
                carried += 1
            continue
        row = await ActionItem.create(
            series_id=meeting.series_id, meeting_id=meeting.id, minutes_id=minutes.id,
            source_field="pending_items", source_index=index, content=content,
            owner_user_id=owner_user_id, owner_name=owner_name,
            deadline=deadline, deadline_text=deadline_text or None, status="OPEN",
            evidence_json=_evidence_of(item),
            dedupe_key=_dedupe_key(normalized, owner_user_id, owner_name, deadline),
            last_seen_meeting_id=meeting.id, created_by=actor,
        )
        existing.append(row)
        await _write_event(row.id, "CREATE", None, "OPEN", "由已确认纪要生成", actor,
                           actor_fallback=actor)
        created += 1
    return {"created": created, "carried": carried, "skipped": skipped}


async def regenerate_from_minutes(minutes_id: int, *, actor_id: int | None = None) -> dict:
    """管理员「重新生成候选」：待办侧是幂等同步，不改动已有待办的状态与负责人。"""
    row = await MeetingMinutes.get_or_none(id=minutes_id)
    if row is None:
        raise CustomException("会议纪要不存在")
    if row.status != "CONFIRMED":
        raise CustomException("只有已确认的纪要可以重新生成候选")
    return await sync_from_minutes(row, actor_id=actor_id)


async def _meeting_of(item: ActionItem) -> Meeting | None:
    return await Meeting.get_or_none(id=item.meeting_id)


async def can_manage(item: ActionItem, user: User) -> bool:
    """管理口径：负责人 / 会议主持人 / 会议创建人 / 系列负责人 / 管理员。"""
    if user.role == "ADMIN":
        return True
    if item.owner_user_id is not None and item.owner_user_id == user.id:
        return True
    meeting = await _meeting_of(item)
    if meeting is not None and user.id in {meeting.creator_id, meeting.host_id}:
        return True
    if item.series_id is not None:
        series = await MeetingSeries.get_or_none(id=item.series_id)
        if series is not None and series.owner_id == user.id:
            return True
    return False


async def assert_can_manage(item: ActionItem, user: User) -> None:
    if not await can_manage(item, user):
        raise CustomException("没有该待办的操作权限", "403")


async def get_item(item_id: int, user: User, *, editable: bool = False) -> ActionItem:
    item = await ActionItem.get_or_none(id=item_id)
    if item is None:
        raise CustomException("待办不存在")
    meeting = await _meeting_of(item)
    if meeting is None or not await access.has_meeting_access(meeting, user):
        raise CustomException("没有待办访问权限", "403")
    if editable:
        await assert_can_manage(item, user)
    return item


async def action_item_dict(item: ActionItem, *, series_name: str | None = None,
                           meeting_title: str | None = None) -> dict:
    return {
        "id": item.id,
        "series_id": item.series_id,
        "series_name": series_name,
        "meeting_id": item.meeting_id,
        "meeting_title": meeting_title,
        "minutes_id": item.minutes_id,
        "content": item.content,
        "owner_user_id": item.owner_user_id,
        "owner_name": item.owner_name,
        "deadline": format_datetime(item.deadline),
        "deadline_text": item.deadline_text,
        "status": item.status,
        "evidence_json": item.evidence_json,
        "last_seen_meeting_id": item.last_seen_meeting_id,
        "create_time": format_datetime(item.create_time),
        "update_time": format_datetime(item.update_time),
    }


async def batch_action_item_dicts(rows: list[ActionItem]) -> list[dict]:
    """列表页批量转换：系列名与会议标题各查一次，消掉逐行 N+1。"""
    series_ids = {row.series_id for row in rows if row.series_id}
    meeting_ids = {row.meeting_id for row in rows if row.meeting_id}
    series_names = {row.id: row.name for row in await MeetingSeries.filter(
        id__in=series_ids)} if series_ids else {}
    titles = {row.id: row.title for row in await Meeting.filter(
        id__in=meeting_ids)} if meeting_ids else {}
    return [
        await action_item_dict(row, series_name=series_names.get(row.series_id),
                               meeting_title=titles.get(row.meeting_id))
        for row in rows
    ]


async def list_action_items(user: User, *, series_id: int | None = None,
                            owner_user_id: int | None = None, status: str = "",
                            overdue: bool = False, page_num: int = 1,
                            page_size: int = 10) -> tuple[int, list[dict]]:
    query = ActionItem.all()
    if user.role != "ADMIN":
        # 可见性统一走 access：只能看自己可见会议下的待办
        allowed = await access.visible_meeting_ids(user)
        query = query.filter(meeting_id__in=allowed or [-1])
    if series_id is not None:
        query = query.filter(series_id=series_id)
    if owner_user_id is not None:
        query = query.filter(owner_user_id=owner_user_id)
    if status:
        query = query.filter(status=status)
    if overdue:
        query = query.filter(deadline__lt=now(), status__in=OPEN_STATUSES)
    total = await query.count()
    rows = await query.order_by("-create_time", "-id").offset(
        (page_num - 1) * page_size).limit(page_size)
    return total, await batch_action_item_dicts(rows)


async def add_action_item(user: User, *, meeting_id: int, content: str,
                          series_id: int | None = None, owner_user_id: int | None = None,
                          owner_name: str | None = None,
                          deadline_text: str | None = None) -> dict:
    meeting = await Meeting.get_or_none(id=meeting_id)
    if meeting is None:
        raise CustomException("会议不存在")
    if not await access.has_meeting_access(meeting, user):
        raise CustomException("没有会议访问权限", "403")
    text = (content or "").strip()
    if not text:
        raise CustomException("请输入待办内容")
    resolved_series_id = series_id if series_id is not None else meeting.series_id
    if resolved_series_id is not None:
        series = await MeetingSeries.get_or_none(id=resolved_series_id)
        if series is None:
            raise CustomException("会议系列不存在")
        if not await access.has_series_access(series, user):
            raise CustomException("没有会议系列访问权限", "403")
    if owner_user_id is not None:
        owner = await User.get_or_none(id=owner_user_id)
        if owner is None:
            raise CustomException("负责人不存在")
        owner_name = owner.name
    else:
        owner_name = (owner_name or "").strip() or None
    normalized = _norm(text)
    deadline = parse_deadline(deadline_text, reference=meeting.start_time)
    item = await ActionItem.create(
        series_id=resolved_series_id, meeting_id=meeting.id, minutes_id=None,
        source_field=None, source_index=None, content=text,
        owner_user_id=owner_user_id, owner_name=owner_name,
        deadline=deadline, deadline_text=(deadline_text or "").strip() or None,
        status="OPEN", evidence_json=None,
        dedupe_key=_dedupe_key(normalized, owner_user_id, owner_name, deadline),
        last_seen_meeting_id=meeting.id, created_by=user.id,
    )
    await _write_event(item.id, "CREATE", None, "OPEN", "手工新增", user.id)
    return await action_item_dict(item, meeting_title=meeting.title)


async def update_action_item(user: User, item_id: int, fields: dict) -> dict:
    """按显式提交的字段更新。负责人/期限变化各写一条事件，正文变化不造事件类型。"""
    item = await get_item(item_id, user, editable=True)
    changes: dict = {}
    events: list[tuple[str, str | None, str | None, str]] = []
    if "content" in fields:
        text = (fields.get("content") or "").strip()
        if not text:
            raise CustomException("待办内容不能为空")
        changes["content"] = text
    if "owner_user_id" in fields:
        owner_id = fields.get("owner_user_id")
        new_name = None
        if owner_id is not None:
            owner = await User.get_or_none(id=owner_id)
            if owner is None:
                raise CustomException("负责人不存在")
            new_name = owner.name
        events.append(("OWNER", item.owner_name, new_name or "", "变更负责人"))
        changes["owner_user_id"] = owner_id
        changes["owner_name"] = new_name
    if "deadline" in fields:
        raw_deadline = fields.get("deadline")
        deadline = None
        if raw_deadline:
            try:
                deadline = datetime.fromisoformat(str(raw_deadline))
            except ValueError as error:
                raise CustomException("截止时间格式不合法") from error
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=LOCAL_TZ)
        events.append(("DEADLINE", format_datetime(item.deadline), format_datetime(deadline),
                       "变更截止时间"))
        changes["deadline"] = deadline
    if "deadline_text" in fields:
        deadline_text = (fields.get("deadline_text") or "").strip() or None
        changes["deadline_text"] = deadline_text
        if "deadline" not in fields:
            meeting = await _meeting_of(item)
            deadline = parse_deadline(
                deadline_text, reference=meeting.start_time if meeting else None)
            events.append(("DEADLINE", format_datetime(item.deadline),
                           format_datetime(deadline), "变更截止时间"))
            changes["deadline"] = deadline
    if "content" in changes or "owner_name" in changes or "deadline" in changes:
        owner_id = changes.get("owner_user_id", item.owner_user_id)
        owner_name = changes.get("owner_name", item.owner_name)
        deadline = changes.get("deadline", item.deadline)
        content = changes.get("content", item.content)
        changes["dedupe_key"] = _dedupe_key(
            _norm(content), owner_id, owner_name, deadline)
    if changes:
        await ActionItem.filter(id=item.id).update(**changes)
        for event_type, from_value, to_value, note in events:
            if from_value == to_value:
                continue
            await _write_event(item.id, event_type, from_value, to_value, note, user.id)
    refreshed = await ActionItem.get(id=item.id)
    meeting = await _meeting_of(refreshed)
    series = await MeetingSeries.get_or_none(id=refreshed.series_id) if refreshed.series_id else None
    return await action_item_dict(
        refreshed, series_name=series.name if series else None,
        meeting_title=meeting.title if meeting else None)


async def update_status(user: User, item_id: int, status: str, note: str | None = None) -> dict:
    if status not in STATUSES:
        raise CustomException("待办状态取值不合法")
    item = await get_item(item_id, user, editable=True)
    if item.status != status:
        await ActionItem.filter(id=item.id, status=item.status).update(status=status)
        await _write_event(item.id, "STATUS", item.status, status, note, user.id)
    refreshed = await ActionItem.get(id=item.id)
    return await action_item_dict(refreshed)


async def list_events(user: User, item_id: int) -> list[dict]:
    item = await get_item(item_id, user)
    rows = await ActionItemEvent.filter(
        action_item_id=item.id).order_by("create_time", "id")
    actor_ids = {row.actor_id for row in rows if row.actor_id}
    names = {user.id: user.name for user in await User.filter(
        id__in=actor_ids)} if actor_ids else {}
    return [
        {
            "id": row.id,
            "action_item_id": row.action_item_id,
            "event_type": row.event_type,
            "from_value": row.from_value,
            "to_value": row.to_value,
            "note": row.note,
            "actor_id": row.actor_id,
            "actor_name": names.get(row.actor_id),
            "create_time": format_datetime(row.create_time),
        }
        for row in rows
    ]


__all__ = [
    "OPEN_STATUSES", "STATUSES", "action_item_dict", "add_action_item",
    "can_manage", "get_item", "list_action_items", "list_events", "parse_deadline",
    "regenerate_from_minutes", "sync_from_minutes", "update_action_item",
    "update_status",
]
