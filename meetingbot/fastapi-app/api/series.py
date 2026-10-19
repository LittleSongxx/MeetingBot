"""会议系列接口：列表/详情/选项/增改/归档/归属建议。

系列可见与编辑权限全部在 `services.series_service` 里判定（口径来自 `common.access`），
这里只做参数收口与 Result 包装。建议接口只读：不会写 meeting.series_id。
"""

from common import access
from common.auth import get_current_user
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from fastapi import APIRouter, Depends
from models import Meeting, User
from pydantic import BaseModel
from services import series_service

router = APIRouter(prefix="/meeting/series")


class SeriesPayload(BaseModel):
    # 编辑时回填；新增时不存在
    id: int | None = None
    name: str | None = None
    description: str | None = None
    cadence: str | None = None
    status: str | None = None
    # 负责人：留空取当前用户；只有管理员可以指定成别人（前端只对管理员展示该下拉）
    owner_id: int | None = None


class IdPayload(BaseModel):
    id: int


class SuggestPayload(BaseModel):
    meeting_id: int


class AssignPayload(BaseModel):
    meeting_id: int
    series_id: int | None = None


@router.get("/selectPage")
async def select_page(name: str = "", status: str = "", cadence: str = "",
                      page_num: int = 1, page_size: int = 10,
                      current_user: User = Depends(get_current_user)):
    total, rows = await series_service.list_series(
        current_user, name=name, status=status, cadence=cadence,
        page_num=page_num, page_size=page_size)
    return Result.success(PageInfo(total=total, list=rows))


@router.get("/selectById/{series_id}")
async def select_by_id(series_id: int, current_user: User = Depends(get_current_user)):
    series = await series_service.get_accessible_series(series_id, current_user)
    return Result.success(await series_service.detail(series))


@router.get("/options")
async def options(current_user: User = Depends(get_current_user)):
    return Result.success(await series_service.options(current_user))


@router.post("/add")
async def add(payload: SeriesPayload, current_user: User = Depends(get_current_user)):
    return Result.success(await series_service.create(
        current_user, name=payload.name, description=payload.description,
        cadence=payload.cadence, owner_id=payload.owner_id))


@router.put("/update")
async def update(payload: SeriesPayload, current_user: User = Depends(get_current_user)):
    if payload.id is None:
        raise CustomException("系列ID不能为空")
    series = await series_service.get_accessible_series(payload.id, current_user)
    fields = payload.model_dump(exclude_unset=True, exclude={"id"})
    return Result.success(await series_service.update(series, current_user, fields))


@router.post("/archive")
async def archive(payload: IdPayload, current_user: User = Depends(get_current_user)):
    series = await series_service.get_accessible_series(payload.id, current_user)
    return Result.success(await series_service.archive(series, current_user))


@router.post("/assign")
async def assign(payload: AssignPayload, current_user: User = Depends(get_current_user)):
    return Result.success(await series_service.assign_meeting(
        current_user, payload.meeting_id, payload.series_id))


@router.post("/suggest")
async def suggest(payload: SuggestPayload, current_user: User = Depends(get_current_user)):
    meeting = await Meeting.get_or_none(id=payload.meeting_id)
    if meeting is None:
        raise CustomException("会议不存在")
    if not await access.has_meeting_access(meeting, current_user):
        raise CustomException("没有会议访问权限", "403")
    return Result.success({"candidates": await series_service.suggest(meeting, current_user)})
