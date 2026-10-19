"""会议待办接口：列表、手工新增、编辑、状态流转与变更事件。

状态更新权限=负责人/会议主持人/会议创建人/系列负责人/管理员（判定在
`services.action_item_service.can_manage`）；每次状态变化都写一条 action_item_event，
页面上的时间线读的就是它。
"""

from common.auth import get_current_user
from common.result import PageInfo, Result
from fastapi import APIRouter, Depends
from models import User
from pydantic import BaseModel
from services import action_item_service

router = APIRouter(prefix="/action-item")


class AddPayload(BaseModel):
    # 不传时继承会议所属系列
    series_id: int | None = None
    meeting_id: int
    content: str | None = None
    owner_user_id: int | None = None
    owner_name: str | None = None
    deadline_text: str | None = None


class UpdatePayload(BaseModel):
    id: int
    content: str | None = None
    owner_user_id: int | None = None
    deadline: str | None = None
    deadline_text: str | None = None


class StatusPayload(BaseModel):
    id: int
    status: str
    note: str | None = None


def _truthy(value: str) -> bool:
    """查询串里的开关统一口径：前端可能发 true/1/yes/on。"""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@router.get("/selectPage")
async def select_page(series_id: int | None = None, owner_user_id: int | None = None,
                      status: str = "", overdue: str = "",
                      page_num: int = 1, page_size: int = 10,
                      current_user: User = Depends(get_current_user)):
    total, rows = await action_item_service.list_action_items(
        current_user, series_id=series_id, owner_user_id=owner_user_id,
        status=status, overdue=_truthy(overdue),
        page_num=page_num, page_size=page_size)
    return Result.success(PageInfo(total=total, list=rows))


@router.post("/add")
async def add(payload: AddPayload, current_user: User = Depends(get_current_user)):
    return Result.success(await action_item_service.add_action_item(
        current_user, meeting_id=payload.meeting_id, content=payload.content or "",
        series_id=payload.series_id, owner_user_id=payload.owner_user_id,
        owner_name=payload.owner_name, deadline_text=payload.deadline_text))


@router.put("/update")
async def update(payload: UpdatePayload, current_user: User = Depends(get_current_user)):
    fields = payload.model_dump(exclude_unset=True, exclude={"id"})
    return Result.success(await action_item_service.update_action_item(
        current_user, payload.id, fields))


@router.put("/updateStatus")
async def update_status(payload: StatusPayload,
                        current_user: User = Depends(get_current_user)):
    return Result.success(await action_item_service.update_status(
        current_user, payload.id, payload.status, payload.note))


@router.get("/events/{item_id}")
async def events(item_id: int, current_user: User = Depends(get_current_user)):
    return Result.success(await action_item_service.list_events(current_user, item_id))
