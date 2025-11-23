"""长期记忆接口：列表、确认（含冲突替代）、拒绝、过期、编辑与个人偏好新增。

确认接口的冲突语义：同 scope/kind 已有相似活跃条目时**不落库**，返回
`{confirmed: false, conflicts: [...]}`；前端让用户选择替代后带 `supersede_id`
再次确认，旧条置 SUPERSEDED、新条记 supersedes_id。契约其余部分见任务说明。
"""

from common.auth import get_current_user
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from fastapi import APIRouter, Depends
from models import User
from pydantic import BaseModel
from services import memory_service

router = APIRouter(prefix="/memory")


class ConfirmPayload(BaseModel):
    id: int
    # 用户选择替换哪条旧记忆；不传时只做冲突检测
    supersede_id: int | None = None


class IdPayload(BaseModel):
    id: int


class UpdatePayload(BaseModel):
    id: int
    content: str | None = None


class PreferencePayload(BaseModel):
    content: str | None = None


@router.get("/selectPage")
async def select_page(scope_type: str = "", scope_id: int | None = None,
                      kind: str = "", status: str = "",
                      page_num: int = 1, page_size: int = 10,
                      current_user: User = Depends(get_current_user)):
    total, rows = await memory_service.list_memories(
        current_user, scope_type=scope_type, scope_id=scope_id, kind=kind,
        status=status, page_num=page_num, page_size=page_size)
    return Result.success(PageInfo(total=total, list=rows))


@router.post("/confirm")
async def confirm(payload: ConfirmPayload, current_user: User = Depends(get_current_user)):
    memory = await memory_service.get_memory(payload.id)
    return Result.success(await memory_service.confirm(
        memory, current_user, supersede_id=payload.supersede_id))


@router.post("/reject")
async def reject(payload: IdPayload, current_user: User = Depends(get_current_user)):
    memory = await memory_service.get_memory(payload.id)
    return Result.success(await memory_service.reject(memory, current_user))


@router.post("/expire")
async def expire(payload: IdPayload, current_user: User = Depends(get_current_user)):
    memory = await memory_service.get_memory(payload.id)
    return Result.success(await memory_service.expire(memory, current_user))


@router.put("/update")
async def update(payload: UpdatePayload, current_user: User = Depends(get_current_user)):
    if payload.content is None:
        raise CustomException("记忆内容不能为空")
    memory = await memory_service.get_memory(payload.id)
    return Result.success(await memory_service.update_content(
        memory, current_user, payload.content))


@router.post("/preference")
async def preference(payload: PreferencePayload,
                     current_user: User = Depends(get_current_user)):
    if payload.content is None:
        raise CustomException("偏好内容不能为空")
    return Result.success(await memory_service.create_preference(
        current_user, payload.content))
