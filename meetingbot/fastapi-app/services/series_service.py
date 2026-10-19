"""会议系列服务：CRUD、可见性收窄与「这场会属于哪个系列」的建议算法。

系列本身**不授予**任何权限：系列可见 = 管理员 / 系列负责人 / 系列下有对该用户可见的会议，
口径统一在 `common.access`，这里不再另写一遍；系列编辑 = 负责人或管理员。

建议算法只读不写：`suggest` 只产出候选与理由，`meeting.series_id` 由人确认后再写，
因此可以在会议尚未归属时反复调用而不产生副作用。四个信号各司其职——标题相似
（复用 `common.textsim`，与评测侧同一份判据）、参会人重叠、主持人、最近会议日期。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from common import access, textsim
from common.exception_handler import CustomException
from common.times import format_datetime, now, to_local
from models import (
    ActionItem,
    Meeting,
    MeetingMemory,
    MeetingParticipant,
    MeetingSeries,
    User,
)

# 周期取值与 series.status 都是封闭集合：前后端与种子数据共用这一份声明
CADENCES = ("WEEKLY", "BIWEEKLY", "MONTHLY", "IRREGULAR")
SERIES_STATUSES = ("ACTIVE", "ARCHIVED")
SUGGEST_LIMIT = 3
# 最近会议日期的衰减窗口：超过 90 天基本不再提供「最近开过」的证据
RECENCY_WINDOW_DAYS = 90.0
# 建议打分权重（四个信号之和为 1）。权重集中在这里，调参不散落在判断分支里
WEIGHT_TITLE = 0.45
WEIGHT_PARTICIPANTS = 0.25
WEIGHT_HOST = 0.20
WEIGHT_RECENCY = 0.10


async def get_series(series_id: int) -> MeetingSeries:
    series = await MeetingSeries.get_or_none(id=series_id)
    if series is None:
        raise CustomException("会议系列不存在")
    return series


async def get_accessible_series(series_id: int, user: User) -> MeetingSeries:
    """按数据权限取系列：看不到的系列统一返回 403，不暴露「存在但无权」。"""
    series = await get_series(series_id)
    if not await access.has_series_access(series, user):
        raise CustomException("没有会议系列访问权限", "403")
    return series


def assert_can_edit(series: MeetingSeries, user: User) -> None:
    """系列编辑口径：负责人或管理员。"""
    if user.role != "ADMIN" and series.owner_id != user.id:
        raise CustomException("只有管理员或系列负责人可以编辑系列", "403")


async def series_metrics(series_ids: list[int]) -> tuple[dict[int, int], dict[int, int], dict[int, int]]:
    """批量统计系列列表的三个计数，避免每行各查一遍（N+1）。"""
    if not series_ids:
        return {}, {}, {}
    meeting_rows = await Meeting.filter(
        series_id__in=series_ids).values_list("series_id", flat=True)
    meeting_count = Counter(int(item) for item in meeting_rows if item is not None)
    action_rows = await ActionItem.filter(
        series_id__in=series_ids, status="OPEN").values_list("series_id", flat=True)
    open_action_count = Counter(int(item) for item in action_rows if item is not None)
    memory_rows = await MeetingMemory.filter(
        scope_type="SERIES", scope_id__in=series_ids, status="CONFIRMED",
    ).values_list("scope_id", flat=True)
    memory_count = Counter(int(item) for item in memory_rows)
    return meeting_count, open_action_count, memory_count


async def owner_names(rows: list[MeetingSeries]) -> dict[int, str]:
    owner_ids = {row.owner_id for row in rows}
    if not owner_ids:
        return {}
    return {user.id: user.name for user in await User.filter(id__in=owner_ids)}


def series_dict(series: MeetingSeries, *, owner_name: str | None = None,
                meeting_count: int = 0, open_action_count: int = 0,
                memory_count: int = 0) -> dict:
    """系列列表/详情共用的序列化。键名与前端契约一一对应。"""
    return {
        "id": series.id,
        "name": series.name,
        "description": series.description,
        "cadence": series.cadence,
        "owner_id": series.owner_id,
        "owner_name": owner_name,
        "status": series.status,
        "meeting_count": meeting_count,
        "open_action_count": open_action_count,
        "memory_count": memory_count,
        "create_time": format_datetime(series.create_time),
        "update_time": format_datetime(series.update_time),
    }


async def list_series(user: User, *, name: str = "", status: str = "", cadence: str = "",
                      page_num: int = 1, page_size: int = 10) -> tuple[int, list[dict]]:
    """分页列系列。可见范围先由 access 收窄，再叠页面筛选条件。"""
    visible = await access.visible_series_ids(user)
    query = MeetingSeries.all()
    if visible is not None:
        # 空列表用 [-1] 兜底：id__in=[] 在部分后端上等价于不过滤，会把无权限变成无限制
        query = query.filter(id__in=visible or [-1])
    if name:
        query = query.filter(name__contains=name)
    if status:
        query = query.filter(status=status)
    if cadence:
        query = query.filter(cadence=cadence)
    total = await query.count()
    rows = await query.order_by("-update_time", "-id").offset(
        (page_num - 1) * page_size).limit(page_size)
    meeting_count, open_action_count, memory_count = await series_metrics([row.id for row in rows])
    names = await owner_names(rows)
    return total, [
        series_dict(row, owner_name=names.get(row.owner_id),
                    meeting_count=meeting_count.get(row.id, 0),
                    open_action_count=open_action_count.get(row.id, 0),
                    memory_count=memory_count.get(row.id, 0))
        for row in rows
    ]


async def detail(series: MeetingSeries) -> dict:
    """系列详情：基础字段 + 会议清单 + 三个计数（含待确认记忆）。"""
    meetings = await Meeting.filter(series_id=series.id).order_by("-start_time", "-id")
    owner = await User.get_or_none(id=series.owner_id)
    open_action_count = await ActionItem.filter(series_id=series.id, status="OPEN").count()
    memory_count = await MeetingMemory.filter(
        scope_type="SERIES", scope_id=series.id, status="CONFIRMED").count()
    pending_memory_count = await MeetingMemory.filter(
        scope_type="SERIES", scope_id=series.id, status="PROPOSED").count()
    data = series_dict(series, owner_name=owner.name if owner else None,
                       meeting_count=len(meetings), open_action_count=open_action_count,
                       memory_count=memory_count)
    data["meetings"] = [
        {"id": meeting.id, "title": meeting.title,
         "start_time": format_datetime(meeting.start_time), "status": meeting.status}
        for meeting in meetings
    ]
    data["pending_memory_count"] = pending_memory_count
    return data


async def options(user: User) -> list[dict]:
    """下拉框选项：只给进行中的系列，按名称稳定排序。"""
    visible = await access.visible_series_ids(user)
    query = MeetingSeries.filter(status="ACTIVE")
    if visible is not None:
        query = query.filter(id__in=visible or [-1])
    rows = await query.order_by("name", "id")
    return [{"id": row.id, "name": row.name} for row in rows]


async def _resolve_owner(user: User, owner_id: int | None) -> tuple[int, str]:
    """系列负责人：留空=当前用户；指定成别人只有管理员可以（避免自封负责人）。"""
    if owner_id is None or owner_id == user.id:
        return user.id, user.name
    if user.role != "ADMIN":
        raise CustomException("只有管理员可以指定系列负责人", "403")
    owner = await User.get_or_none(id=owner_id, status="NORMAL")
    if owner is None:
        raise CustomException("系列负责人不存在或账号已停用")
    return owner.id, owner.name


async def create(user: User, *, name: str | None, description: str | None = None,
                 cadence: str | None = None, owner_id: int | None = None) -> dict:
    series_name = (name or "").strip()
    if not series_name:
        raise CustomException("请输入系列名称")
    series_cadence = (cadence or "IRREGULAR").strip() or "IRREGULAR"
    if series_cadence not in CADENCES:
        raise CustomException("会议周期取值不合法")
    resolved_owner_id, resolved_owner_name = await _resolve_owner(user, owner_id)
    series = await MeetingSeries.create(
        name=series_name, description=description, cadence=series_cadence,
        owner_id=resolved_owner_id, status="ACTIVE")
    return series_dict(series, owner_name=resolved_owner_name)


async def update(series: MeetingSeries, user: User, fields: dict) -> dict:
    """按「显式提交的字段」更新。字段白名单之外一律忽略，owner 不允许经此改动。"""
    assert_can_edit(series, user)
    changes: dict = {}
    if "name" in fields:
        name = (fields.get("name") or "").strip()
        if not name:
            raise CustomException("请输入系列名称")
        changes["name"] = name
    if "description" in fields:
        changes["description"] = fields.get("description")
    if "cadence" in fields:
        cadence = (fields.get("cadence") or "").strip()
        if cadence and cadence not in CADENCES:
            raise CustomException("会议周期取值不合法")
        if cadence:
            changes["cadence"] = cadence
    if "status" in fields:
        status = (fields.get("status") or "").strip()
        if status not in SERIES_STATUSES:
            raise CustomException("系列状态取值不合法")
        changes["status"] = status
    if "owner_id" in fields:
        resolved_owner_id, _ = await _resolve_owner(user, fields.get("owner_id"))
        changes["owner_id"] = resolved_owner_id
    if changes:
        await MeetingSeries.filter(id=series.id).update(**changes)
    refreshed = await MeetingSeries.get(id=series.id)
    owner = await User.get_or_none(id=refreshed.owner_id)
    return series_dict(refreshed, owner_name=owner.name if owner else None)


async def archive(series: MeetingSeries, user: User) -> dict:
    """归档不是删除：历史会议、记忆与待办全部保留，只是不再出现在下拉框。"""
    assert_can_edit(series, user)
    await MeetingSeries.filter(id=series.id).update(status="ARCHIVED")
    refreshed = await MeetingSeries.get(id=series.id)
    owner = await User.get_or_none(id=refreshed.owner_id)
    return series_dict(refreshed, owner_name=owner.name if owner else None)


def _recency_reason(latest: datetime, reference: datetime) -> tuple[float, str | None]:
    """最近一次系列会议的新鲜度打分与理由。未来会议视为最新（排期已定）。"""
    if latest is None:
        return 0.0, None
    days = (reference - to_local(latest)).total_seconds() / 86400.0
    if days < 0:
        return 1.0, f"系列下一场会议安排在 {format_datetime(latest)}"
    score = max(0.0, 1.0 - days / RECENCY_WINDOW_DAYS)
    if score <= 0:
        return 0.0, None
    if days < 1:
        return score, "系列最近一次会议就在今天"
    return score, f"系列最近一次会议在 {int(days)} 天前"


async def suggest(meeting: Meeting, user: User) -> list[dict]:
    """给一场会议推荐 top3 系列。只读：不会写 meeting.series_id。"""
    visible = await access.visible_series_ids(user)
    series_query = MeetingSeries.filter(status="ACTIVE")
    if visible is not None:
        series_query = series_query.filter(id__in=visible or [-1])
    series_rows = await series_query
    if not series_rows:
        return []

    series_ids = [row.id for row in series_rows]
    meetings = await Meeting.filter(series_id__in=series_ids).order_by("-start_time")
    by_series: dict[int, list[Meeting]] = {}
    for row in meetings:
        by_series.setdefault(int(row.series_id), []).append(row)

    # 参会人一次取全：逐系列查会退化成 N+1，系列数量一多页面就卡
    member_map: dict[int, set[int]] = {}
    if meetings:
        for meeting_id, member_id in await MeetingParticipant.filter(
                meeting_id__in=[row.id for row in meetings]).values_list("meeting_id", "user_id"):
            member_map.setdefault(int(meeting_id), set()).add(int(member_id))
    target_members = {int(item) for item in await MeetingParticipant.filter(
        meeting_id=meeting.id).values_list("user_id", flat=True)}

    target_title = textsim.normalize(meeting.title)
    reference = to_local(meeting.start_time) if meeting.start_time else now()
    candidates: list[dict] = []
    for series in series_rows:
        rows = by_series.get(series.id, [])
        reasons: list[str] = []

        best_similarity, best_title = textsim.similarity(
            target_title, textsim.normalize(series.name))
        for row in rows:
            similarity, _ = textsim.similarity(target_title, textsim.normalize(row.title))
            if similarity > best_similarity:
                best_similarity, best_title = similarity, row.title
        if best_similarity >= textsim.SIMILARITY_THRESHOLD and best_title:
            reasons.append(f"标题相似：与「{best_title}」相似度 {best_similarity:.2f}")

        best_overlap, best_pair = 0.0, (0, 0)
        if target_members:
            for row in rows:
                members = member_map.get(row.id, set())
                if not members:
                    continue
                overlap_count = len(target_members & members)
                overlap = overlap_count / len(target_members)
                if overlap > best_overlap:
                    best_overlap, best_pair = overlap, (overlap_count, len(target_members))
        if best_overlap > 0:
            reasons.append(f"参会人重叠 {best_pair[0]}/{best_pair[1]} 人")

        host_match = any(row.host_id == meeting.host_id for row in rows)
        if host_match:
            reasons.append("主持人与系列历史会议相同")

        recency, recency_reason = (0.0, None)
        if rows:
            latest = max(rows, key=lambda row: row.start_time)
            recency, recency_reason = _recency_reason(latest.start_time, reference)
        if recency_reason:
            reasons.append(recency_reason)

        if not reasons:
            continue
        score = (WEIGHT_TITLE * best_similarity
                 + WEIGHT_PARTICIPANTS * best_overlap
                 + WEIGHT_HOST * (1.0 if host_match else 0.0)
                 + WEIGHT_RECENCY * recency)
        if score <= 0:
            continue
        candidates.append({
            "series_id": series.id,
            "name": series.name,
            "score": round(score, 4),
            "reasons": reasons,
        })
    candidates.sort(key=lambda item: (-item["score"], item["series_id"]))
    return candidates[:SUGGEST_LIMIT]


async def assign_meeting(user: User, meeting_id: int, series_id: int | None) -> dict:
    """把一场会议归入/移出系列（不改变会议其它字段与状态）。

    与「编辑会议」分开：会议编辑只允许 SCHEDULED，而归类是会后整理动作，
    FINISHED/IN_PROGRESS 的会议也应该能归档到系列。权限沿用会议写权限
    （管理员/创建人/主持人）+ 目标系列可见性。
    """
    meeting = await Meeting.get_or_none(id=meeting_id)
    if meeting is None:
        raise CustomException("会议不存在")
    if not await access.has_meeting_access(meeting, user):
        raise CustomException("没有会议访问权限", "403")
    if (user.role != "ADMIN" and meeting.creator_id != user.id
            and meeting.host_id != user.id):
        raise CustomException("只有管理员、会议创建人或主持人可以归入系列", "403")
    series = None
    target = int(series_id) if series_id else None
    if target is not None:
        series = await MeetingSeries.get_or_none(id=target)
        if series is None:
            raise CustomException("系列不存在")
        if not await access.has_series_access(series, user):
            raise CustomException("没有该系列的访问权限", "403")
    await Meeting.filter(id=meeting.id).update(series_id=target)
    return {"meeting_id": meeting.id, "series_id": target,
            "series_name": series.name if series else None}


__all__ = [
    "CADENCES", "SERIES_STATUSES", "SUGGEST_LIMIT",
    "archive", "assert_can_edit", "assign_meeting", "create", "detail",
    "get_accessible_series", "get_series", "list_series", "options",
    "series_dict", "suggest", "update",
]
