"""长期记忆服务：候选生成、人工确认（含冲突替代）、拒绝/过期/编辑与可见性收窄。

三条不变量在这里落地：

1. **人拍板才算数**：`propose_from_minutes` 只写 `PROPOSED`，`query_state` 只读
   `CONFIRMED`；确认动作由系列负责人/管理员（个人偏好只能本人）执行。
2. **不自动把摘要变结论**：候选只从已确认纪要的 `decisions` 条目生成（→ `DECISION`），
   summary 不批量转 `CONCLUSION`——结论要人一条条确认，而不是把整段摘要塞进记忆。
3. **替代而不是并存**：确认时若同 scope/kind 已有相似且活跃的条目，接口返回冲突提示；
   用户带 `supersede_id` 再确认时，旧条置 `SUPERSEDED` 并写 `valid_to`，新条记
   `supersedes_id`，历史不会被抹掉。
"""

from __future__ import annotations

from typing import Any, Mapping

from common import access, textsim
from common.exception_handler import CustomException
from common.times import format_datetime, now
from models import Meeting, MeetingMemory, MeetingMinutes, MeetingSeries, User
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from services import contracts

SCOPE_SERIES = "SERIES"
SCOPE_USER = "USER"
KINDS = ("DECISION", "CONCLUSION", "PREFERENCE")
STATUSES = ("PROPOSED", "CONFIRMED", "REJECTED", "EXPIRED", "SUPERSEDED")
# 对外（工具层）只暴露已确认记忆；PROPOSED 永远只是候选
VISIBLE_STATUS = "CONFIRMED"
# 记忆条目在 citations 里对应纪要的哪个字段：决定/结论都出自 decisions
_FIELD_OF_KIND = {"DECISION": "decisions", "CONCLUSION": "decisions", "PREFERENCE": None}


def _norm(value: Any) -> str:
    return textsim.normalize(value)


def _content_of(item: Any) -> str:
    """从纪要条目里取正文。v1 是裸字符串，v2 是带 content 的映射。"""
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
    """取条目证据的第一条（quote/speaker），没有证据返回 None。"""
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


def memory_dict(memory: MeetingMemory, *, source_meeting_title: str | None = None) -> dict:
    """记忆列表/详情/冲突提示共用的序列化，键名与前端契约一一对应。"""
    return {
        "id": memory.id,
        "scope_type": memory.scope_type,
        "scope_id": memory.scope_id,
        "kind": memory.kind,
        "content": memory.content,
        "status": memory.status,
        "source_type": memory.source_type,
        "source_id": memory.source_id,
        "source_meeting_id": memory.source_meeting_id,
        "source_meeting_title": source_meeting_title,
        "evidence_json": memory.evidence_json,
        "supersedes_id": memory.supersedes_id,
        "valid_from": format_datetime(memory.valid_from),
        "valid_to": format_datetime(memory.valid_to),
        "created_by": memory.created_by,
        "confirmed_by": memory.confirmed_by,
        "confirmed_time": format_datetime(memory.confirmed_time),
        "create_time": format_datetime(memory.create_time),
    }


async def source_meeting_titles(rows: list[MeetingMemory]) -> dict[int, str]:
    meeting_ids = {row.source_meeting_id for row in rows if row.source_meeting_id}
    if not meeting_ids:
        return {}
    return {meeting.id: meeting.title for meeting in await Meeting.filter(id__in=meeting_ids)}


async def get_memory(memory_id: int) -> MeetingMemory:
    memory = await MeetingMemory.get_or_none(id=memory_id)
    if memory is None:
        raise CustomException("记忆条目不存在")
    return memory


async def assert_can_manage(memory: MeetingMemory, user: User) -> None:
    """管理口径：系列记忆=系列负责人/管理员；个人偏好=仅本人（管理员不代确认他人偏好）。"""
    if memory.scope_type == SCOPE_USER:
        if memory.scope_id != user.id:
            raise CustomException("只能管理自己的个人偏好", "403")
        return
    if memory.scope_type == SCOPE_SERIES:
        if user.role == "ADMIN":
            return
        series = await MeetingSeries.get_or_none(id=memory.scope_id)
        if series is None or series.owner_id != user.id:
            raise CustomException("只有系列负责人或管理员可以管理系列记忆", "403")
        return
    raise CustomException("记忆范围取值不合法")


async def _active_similar(memory: MeetingMemory, new_content: str) -> list[MeetingMemory]:
    """同 scope/kind 下与本条相似且仍活跃（CONFIRMED）的记忆。"""
    rows = await MeetingMemory.filter(
        scope_type=memory.scope_type, scope_id=memory.scope_id,
        kind=memory.kind, status=VISIBLE_STATUS,
    ).exclude(id=memory.id)
    target = _norm(new_content)
    if not target:
        return []
    return [
        row for row in rows
        if textsim.is_same_item(target, row.normalized or _norm(row.content))
    ]


async def confirm(memory: MeetingMemory, user: User, *, supersede_id: int | None = None) -> dict:
    """确认一条待确认记忆；发现相似活跃条目时**不确认**，先返回冲突让用户选择。"""
    await assert_can_manage(memory, user)
    if memory.status != "PROPOSED":
        raise CustomException("只有待确认的记忆可以确认")
    conflicts = await _active_similar(memory, memory.content)
    target: MeetingMemory | None = None
    if supersede_id is not None:
        target = await MeetingMemory.get_or_none(id=supersede_id)
        if target is None or target.status != VISIBLE_STATUS:
            raise CustomException("要替代的记忆不存在或已失效")
        if (target.scope_type, target.scope_id, target.kind) != (
                memory.scope_type, memory.scope_id, memory.kind):
            raise CustomException("只能替代同范围同类型的记忆")
    elif conflicts:
        # 有冲突时保持 PROPOSED 不动，把冲突列表**结构化**带回前端：
        # 200 + {confirmed:false, conflicts} 让确认弹窗能渲染「替代哪一条」，
        # 而不是让页面去解析错误文案里的 id（那是最容易漂移的接口）。
        titles = await source_meeting_titles(conflicts)
        return {
            "id": memory.id,
            "status": memory.status,
            "confirmed": False,
            "superseded_id": None,
            "conflicts": [
                memory_dict(row, source_meeting_title=titles.get(row.source_meeting_id))
                for row in conflicts
            ],
        }

    timestamp = now()
    async with in_transaction():
        if target is not None:
            await MeetingMemory.filter(id=target.id).update(
                status="SUPERSEDED", valid_to=timestamp)
        await MeetingMemory.filter(id=memory.id).update(
            status=VISIBLE_STATUS,
            supersedes_id=target.id if target is not None else memory.supersedes_id,
            confirmed_by=user.id,
            confirmed_time=timestamp,
            valid_from=memory.valid_from or timestamp,
        )
    refreshed = await MeetingMemory.get(id=memory.id)
    return {
        "id": refreshed.id,
        "status": refreshed.status,
        "confirmed": True,
        "superseded_id": refreshed.supersedes_id,
        "conflicts": [],
    }


async def reject(memory: MeetingMemory, user: User) -> dict:
    await assert_can_manage(memory, user)
    if memory.status not in {"PROPOSED"}:
        raise CustomException("只有待确认的记忆可以拒绝")
    await MeetingMemory.filter(id=memory.id).update(status="REJECTED")
    refreshed = await MeetingMemory.get(id=memory.id)
    return memory_dict(refreshed)


async def expire(memory: MeetingMemory, user: User) -> dict:
    """把记忆标记为过期：保留正文与来源，只写 valid_to，供历史追溯。"""
    await assert_can_manage(memory, user)
    if memory.status not in {"PROPOSED", VISIBLE_STATUS}:
        raise CustomException("当前状态不能标记过期")
    timestamp = now()
    await MeetingMemory.filter(id=memory.id).update(
        status="EXPIRED", valid_to=timestamp)
    refreshed = await MeetingMemory.get(id=memory.id)
    return memory_dict(refreshed)


async def update_content(memory: MeetingMemory, user: User, content: str) -> dict:
    await assert_can_manage(memory, user)
    if memory.status not in {"PROPOSED", VISIBLE_STATUS}:
        raise CustomException("当前状态不能编辑")
    text = (content or "").strip()
    if not text:
        raise CustomException("记忆内容不能为空")
    await MeetingMemory.filter(id=memory.id).update(
        content=text, normalized=_norm(text))
    refreshed = await MeetingMemory.get(id=memory.id)
    return memory_dict(refreshed)


async def create_preference(user: User, content: str) -> dict:
    """个人偏好手动新增：本人显式录入，直接是 CONFIRMED（不是候选）。

    仍走同一套冲突检测：与已有偏好相似时不落库，返回冲突让用户决定替代。
    """
    text = (content or "").strip()
    if not text:
        raise CustomException("偏好内容不能为空")
    normalized = _norm(text)
    existing = await MeetingMemory.filter(
        scope_type=SCOPE_USER, scope_id=user.id, kind="PREFERENCE", status=VISIBLE_STATUS,
    )
    conflicts = [
        row for row in existing
        if normalized and textsim.is_same_item(normalized, row.normalized or _norm(row.content))
    ]
    if conflicts:
        labels = "；".join(
            f"#{row.id}「{row.content[:40]}」" for row in conflicts[:3])
        raise CustomException(
            f"已存在相似的个人偏好，请先编辑原偏好或标记过期后再新增：{labels}")
    timestamp = now()
    memory = await MeetingMemory.create(
        scope_type=SCOPE_USER, scope_id=user.id, kind="PREFERENCE", content=text,
        normalized=normalized, source_type="MANUAL", source_id=None,
        source_meeting_id=None, evidence_json=None, status=VISIBLE_STATUS,
        created_by=user.id, confirmed_by=user.id, confirmed_time=timestamp,
        valid_from=timestamp,
    )
    return {"id": memory.id, "status": memory.status, "created": True, "conflicts": []}


async def propose_from_minutes(minutes, *, actor_id: int | None = None) -> dict:
    """从已确认纪要的 decisions 生成 PROPOSED 候选（幂等，不碰 summary）。"""
    meeting = await Meeting.get_or_none(id=minutes.meeting_id)
    if meeting is None or meeting.series_id is None:
        # 没有系列就没有归属范围；宁可不生成，也不把系列记忆挂到 scope_id=0 上
        return {"created": 0, "skipped": 0, "ids": []}
    items = contracts.upgrade_output(
        {"decisions": minutes.decisions_json or []}).get("decisions") or []
    # 幂等键只看仍「有效」的候选/已确认条：被拒绝、过期或已被替代的条不再阻止
    # 重建——否则「重新生成候选」先撤再建时，旧 REJECTED 行会把新候选挡掉。
    existing = await MeetingMemory.filter(
        source_type="MINUTES", source_id=minutes.id,
        status__in=["PROPOSED", VISIBLE_STATUS])
    existing_norms = [row.normalized or _norm(row.content) for row in existing]
    created_ids: list[int] = []
    skipped = 0
    for item in items:
        content = _content_of(item)
        normalized = _norm(content)
        if not normalized:
            continue
        if any(normalized == old or (
                old and textsim.is_same_item(normalized, old)) for old in existing_norms):
            skipped += 1
            continue
        memory = await MeetingMemory.create(
            scope_type=SCOPE_SERIES, scope_id=meeting.series_id, kind="DECISION",
            content=content, normalized=normalized, source_type="MINUTES",
            source_id=minutes.id, source_meeting_id=minutes.meeting_id,
            evidence_json=_evidence_of(item), status="PROPOSED",
            created_by=minutes.confirmed_by or actor_id or minutes.created_by,
        )
        created_ids.append(memory.id)
        existing_norms.append(normalized)
    return {"created": len(created_ids), "skipped": skipped, "ids": created_ids}


async def reject_proposed_from_minutes(minutes_id: int) -> int:
    """取消确认/删除纪要时，把该纪要来源的候选置 REJECTED（已确认的保留，待办不动）。"""
    return await MeetingMemory.filter(
        source_type="MINUTES", source_id=minutes_id, status="PROPOSED",
    ).update(status="REJECTED")


async def regenerate_from_minutes(minutes_id: int, *, actor_id: int | None = None) -> dict:
    """管理员「重新生成候选」：先撤掉该纪要来源的旧候选，再按当前内容重建。"""
    row = await MeetingMinutes.get_or_none(id=minutes_id)
    if row is None:
        raise CustomException("会议纪要不存在")
    rejected = await reject_proposed_from_minutes(minutes_id)
    result = await propose_from_minutes(row, actor_id=actor_id)
    result["rejected"] = rejected
    return result


async def list_memories(user: User, *, scope_type: str = "", scope_id: int | None = None,
                        kind: str = "", status: str = "",
                        page_num: int = 1, page_size: int = 10) -> tuple[int, list[dict]]:
    """分页列记忆。可见范围：管理员全量；其他人=自己负责/有可见会议的系列 + 自己的偏好。"""
    visible_series = await access.visible_series_ids(user)
    query = MeetingMemory.all()
    if visible_series is not None:
        conditions = Q(scope_type=SCOPE_USER, scope_id=user.id)
        if visible_series:
            conditions |= Q(scope_type=SCOPE_SERIES, scope_id__in=visible_series)
        query = query.filter(conditions)
    if scope_type:
        query = query.filter(scope_type=scope_type)
    if scope_id is not None:
        query = query.filter(scope_id=scope_id)
    if kind:
        query = query.filter(kind=kind)
    if status:
        query = query.filter(status=status)
    total = await query.count()
    rows = await query.order_by("-create_time", "-id").offset(
        (page_num - 1) * page_size).limit(page_size)
    titles = await source_meeting_titles(rows)
    return total, [
        memory_dict(row, source_meeting_title=titles.get(row.source_meeting_id))
        for row in rows
    ]
