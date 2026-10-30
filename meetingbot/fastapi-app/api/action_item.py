"""会议行动项接口：从已确认纪要生成、确认生效、跟踪完成、导出 CSV。"""

from __future__ import annotations

import csv
from datetime import date
from io import StringIO
from urllib.parse import quote

from common.auth import get_current_user
from common.cooldown import ensure_interval
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from common.times import format_datetime, now
from fastapi import APIRouter, Body, Depends, Query
from models import ActionItem, Meeting, MeetingMinutes, MeetingParticipant, User
from pydantic import BaseModel, Field
from services.action_item_service import generate_from_minutes
from starlette.responses import StreamingResponse
from tortoise.expressions import Q

from api.meeting import get_accessible_meeting
from api.meeting_minutes import get_accessible_minutes

router = APIRouter(prefix="/actionItem")

STATUSES = {"DRAFT", "OPEN", "DONE", "CANCELLED"}


class ActionUpdatePayload(BaseModel):
    id: int
    content: str | None = Field(default=None, max_length=1000)
    owner_user_id: int | None = None
    owner_name: str | None = Field(default=None, max_length=100)
    due_date: date | None = None
    due_text: str | None = Field(default=None, max_length=100)


class ActionAddPayload(BaseModel):
    meeting_id: int
    content: str = Field(min_length=1, max_length=1000)
    owner_user_id: int | None = None
    owner_name: str | None = Field(default=None, max_length=100)
    due_date: date | None = None
    due_text: str | None = Field(default=None, max_length=100)


class ActionStatusPayload(BaseModel):
    status: str


async def _accessible_meeting_ids(current_user: User) -> list[int]:
    if current_user.role == "ADMIN":
        return []
    participant_meeting_ids = await MeetingParticipant.filter(
        user_id=current_user.id
    ).values_list("meeting_id", flat=True)
    return list(
        await Meeting.filter(
            Q(creator_id=current_user.id)
            | Q(host_id=current_user.id)
            | Q(id__in=participant_meeting_ids)
        ).values_list("id", flat=True)
    )


async def _ensure_action_write_permission(item: ActionItem, current_user: User) -> None:
    """行动项的写权限：管理员、该条创建人、来源纪要创建人。"""
    if current_user.role == "ADMIN" or item.created_by == current_user.id:
        return
    if item.minutes_id:
        minutes = await MeetingMinutes.get_or_none(id=item.minutes_id)
        if minutes is not None and minutes.created_by == current_user.id:
            return
    raise CustomException("只有管理员、行动项创建人或纪要创建人可以修改该行动项", "403")


async def _ensure_minutes_permission(minutes: MeetingMinutes, current_user: User) -> None:
    if current_user.role != "ADMIN" and minutes.created_by != current_user.id:
        raise CustomException("只有管理员或纪要创建人可以执行该操作", "403")


async def action_dict(item: ActionItem) -> dict:
    meeting = await Meeting.get_or_none(id=item.meeting_id)
    creator = await User.get_or_none(id=item.created_by)
    confirmer = await User.get_or_none(id=item.confirmed_by) if item.confirmed_by else None
    return {
        "id": item.id,
        "meeting_id": item.meeting_id,
        "meeting_title": meeting.title if meeting else None,
        "minutes_id": item.minutes_id,
        "content": item.content,
        "owner_user_id": item.owner_user_id,
        "owner_name": item.owner_name,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "due_text": item.due_text,
        "status": item.status,
        # MANUAL 表示人工新增，其余是 AI 从纪要提取
        "source_field": item.source_field or "MANUAL",
        "source_label": "手动" if (item.source_field or "MANUAL") == "MANUAL" else "AI 提取",
        "created_by": item.created_by,
        "creator_name": creator.name if creator else None,
        "confirmed_by": item.confirmed_by,
        "confirmer_name": confirmer.name if confirmer else None,
        "confirmed_time": format_datetime(item.confirmed_time),
        "create_time": format_datetime(item.create_time),
        "update_time": format_datetime(item.update_time),
    }


async def _validate_owner(owner_user_id: int | None) -> str | None:
    if owner_user_id is None:
        return None
    user = await User.get_or_none(id=owner_user_id, status="NORMAL")
    if user is None:
        raise CustomException("负责人不存在或账号已停用")
    return user.name


@router.get("/selectPage")
async def select_page(
    meetingId: int | None = None,
    status: str = "",
    ownerName: str = "",
    pageNum: int = Query(1, ge=1),
    pageSize: int = Query(10, ge=1, le=100),
    current_user: User = Depends(get_current_user),
):
    query = ActionItem.all()
    accessible_ids = await _accessible_meeting_ids(current_user)
    if current_user.role != "ADMIN":
        query = query.filter(meeting_id__in=accessible_ids)
    if meetingId is not None:
        await get_accessible_meeting(meetingId, current_user)
        query = query.filter(meeting_id=meetingId)
    if status:
        if status not in STATUSES:
            raise CustomException("行动项状态不正确")
        query = query.filter(status=status)
    if ownerName:
        query = query.filter(owner_name__contains=ownerName)
    rows = await query.order_by("-create_time", "-id").offset((pageNum - 1) * pageSize).limit(pageSize)
    return Result.success(
        PageInfo(total=await query.count(), list=[await action_dict(row) for row in rows])
    )


@router.get("/selectByMinutes/{minutes_id}")
async def select_by_minutes(minutes_id: int, current_user: User = Depends(get_current_user)):
    await get_accessible_minutes(minutes_id, current_user)
    rows = await ActionItem.filter(minutes_id=minutes_id).order_by("id")
    return Result.success([await action_dict(row) for row in rows])


@router.post("/generate/{minutes_id}")
async def generate(minutes_id: int, current_user: User = Depends(get_current_user)):
    minutes = await get_accessible_minutes(minutes_id, current_user)
    if minutes.status != "CONFIRMED":
        raise CustomException("只有已确认的纪要才能生成行动项")
    await _ensure_minutes_permission(minutes, current_user)
    ensure_interval(f"action-generate:{current_user.id}", 3.0)
    return Result.success(await generate_from_minutes(minutes, current_user.id))


@router.put("/confirmBatch")
async def confirm_batch(
    ids: list[int] = Body(...),
    current_user: User = Depends(get_current_user),
):
    if not ids:
        raise CustomException("请选择要确认的行动项")
    items = await ActionItem.filter(id__in=set(ids))
    if len(items) != len(set(ids)):
        raise CustomException("部分行动项不存在")
    for item in items:
        await _ensure_action_write_permission(item, current_user)
    confirmed = await ActionItem.filter(id__in=[item.id for item in items], status="DRAFT").update(
        status="OPEN", confirmed_by=current_user.id, confirmed_time=now()
    )
    return Result.success({"confirmed": int(confirmed or 0)})


@router.put("/update")
async def update(payload: ActionUpdatePayload, current_user: User = Depends(get_current_user)):
    item = await ActionItem.get_or_none(id=payload.id)
    if item is None:
        raise CustomException("行动项不存在")
    await _ensure_action_write_permission(item, current_user)
    values = payload.model_dump(exclude_unset=True, exclude={"id"})
    owner_name = await _validate_owner(values.get("owner_user_id"))
    if owner_name and not values.get("owner_name"):
        values["owner_name"] = owner_name
    if not values:
        raise CustomException("没有需要更新的内容")
    await ActionItem.filter(id=item.id).update(**values)
    return Result.success()


@router.put("/status/{item_id}")
async def update_status(
    item_id: int,
    payload: ActionStatusPayload,
    current_user: User = Depends(get_current_user),
):
    if payload.status not in {"OPEN", "DONE", "CANCELLED"}:
        raise CustomException("行动项状态只能是 OPEN、DONE 或 CANCELLED")
    item = await ActionItem.get_or_none(id=item_id)
    if item is None:
        raise CustomException("行动项不存在")
    if item.status not in {"OPEN", "DONE", "CANCELLED"}:
        raise CustomException("只有已生效的行动项可以修改状态，请先确认生效")
    # 负责人可以把自己名下的行动项标记完成/重新打开
    allowed = (
        current_user.role == "ADMIN"
        or item.owner_user_id == current_user.id
        or item.created_by == current_user.id
    )
    if not allowed and item.minutes_id:
        minutes = await MeetingMinutes.get_or_none(id=item.minutes_id)
        allowed = minutes is not None and minutes.created_by == current_user.id
    if not allowed:
        raise CustomException("没有修改该行动项状态的权限", "403")
    await ActionItem.filter(id=item.id).update(status=payload.status)
    return Result.success()


@router.post("/add")
async def add(payload: ActionAddPayload, current_user: User = Depends(get_current_user)):
    meeting = await get_accessible_meeting(payload.meeting_id, current_user)
    if current_user.role != "ADMIN" and meeting.creator_id != current_user.id:
        raise CustomException("只有管理员或会议创建人可以新增行动项", "403")
    owner_name = await _validate_owner(payload.owner_user_id)
    await ActionItem.create(
        meeting_id=meeting.id,
        minutes_id=None,
        content=payload.content.strip(),
        owner_user_id=payload.owner_user_id,
        owner_name=payload.owner_name or owner_name,
        due_date=payload.due_date,
        due_text=payload.due_text,
        status="OPEN",
        source_field="MANUAL",
        source_index=None,
        created_by=current_user.id,
        confirmed_by=current_user.id,
        confirmed_time=now(),
    )
    return Result.success()


@router.delete("/delete/{item_id}")
async def delete(item_id: int, current_user: User = Depends(get_current_user)):
    item = await ActionItem.get_or_none(id=item_id)
    if item is None:
        raise CustomException("行动项不存在")
    await _ensure_action_write_permission(item, current_user)
    await ActionItem.filter(id=item.id).delete()
    return Result.success()


@router.delete("/deleteBatch")
async def delete_batch(
    ids: list[int] = Body(...),
    current_user: User = Depends(get_current_user),
):
    if not ids:
        raise CustomException("请选择要删除的行动项")
    items = await ActionItem.filter(id__in=set(ids))
    if len(items) != len(set(ids)):
        raise CustomException("部分行动项不存在")
    for item in items:
        await _ensure_action_write_permission(item, current_user)
    await ActionItem.filter(id__in=[item.id for item in items]).delete()
    return Result.success()


@router.get("/export")
async def export_csv(
    meetingId: int | None = None,
    current_user: User = Depends(get_current_user),
):
    query = ActionItem.all()
    if current_user.role != "ADMIN":
        query = query.filter(meeting_id__in=await _accessible_meeting_ids(current_user))
    if meetingId is not None:
        await get_accessible_meeting(meetingId, current_user)
        query = query.filter(meeting_id=meetingId)
    rows = await query.order_by("meeting_id", "id")
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["会议主题", "待办内容", "负责人", "截止", "状态", "来源", "创建时间"])
    status_text = {"DRAFT": "待确认", "OPEN": "进行中", "DONE": "已完成", "CANCELLED": "已取消"}
    for item in rows:
        data = await action_dict(item)
        writer.writerow(
            [
                data["meeting_title"] or "",
                data["content"],
                data["owner_name"] or "",
                data["due_date"] or data["due_text"] or "",
                status_text.get(data["status"], data["status"]),
                data["source_label"],
                data["create_time"] or "",
            ]
        )
    filename = f"会议行动项-{meetingId if meetingId is not None else '全部'}.csv"
    return StreamingResponse(
        iter(["\ufeff" + buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
