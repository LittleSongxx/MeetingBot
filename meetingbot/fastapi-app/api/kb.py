"""知识库接口：索引任务的状态与手动触发、检索预览。

自动索引发生在产品已有的状态迁移上（转写成功、纪要确认），这里的接口是补充：
排障时看任务状态、演示时手动触发、开发时试检索。**不重复实现一遍摄入逻辑**——
全部委托给 `services.kb_service`，与自动路径共用同一份实现。
"""

from __future__ import annotations

from typing import Any

from common import qdrant
from common.auth import get_current_user, require_admin
from common.exception_handler import CustomException
from common.result import Result
from fastapi import APIRouter, Depends
from models import KbChunk, KbDocument, KbIndexTask, User
from pydantic import BaseModel, Field
from services import kb_service

from api.meeting import get_accessible_meeting

router = APIRouter(prefix="/kb")


class IndexPayload(BaseModel):
    """手动触发索引：source_type 只允许这两个值，其余由后端推导。"""

    source_type: str = Field(description="TRANSCRIPT 或 MINUTES")
    source_id: int


class SearchPayload(BaseModel):
    query: str
    limit: int = 8
    meeting_ids: list[int] | None = Field(
        default=None, description="限定在这些会议内检索；留空表示当前用户可见的全部会议")
    authority: list[str] | None = Field(
        default=None, description="按权威级过滤：RAW / CONFIRMED")


def _task_dict(task: KbIndexTask) -> dict[str, Any]:
    return {
        "id": task.id, "meeting_id": task.meeting_id,
        "source_type": task.source_type, "source_id": task.source_id,
        "status": task.status, "stage": task.stage, "progress": task.progress,
        "chunk_count": task.chunk_count, "embedded_count": task.embedded_count,
        "document_id": task.document_id, "retry_count": task.retry_count,
        "error_message": task.error_message,
        "started_at": task.started_at, "finished_at": task.finished_at,
        "create_time": task.create_time,
    }


@router.get("/status")
async def status(current_user: User = Depends(get_current_user)):
    """知识库整体状态：够回答"能用吗、为什么没结果"。"""
    from models import AiModelConfig
    embedding = await AiModelConfig.filter(model_type="EMBEDDING", enabled=True).order_by("-id").first()
    return Result.success({
        "vector_store_reachable": await qdrant.health(),
        "collection": qdrant.collection_name(),
        "embedding_model": embedding.model_name if embedding else None,
        "embedding_configured": embedding is not None,
        "document_count": await KbDocument.filter(status="SUCCEEDED").count(),
        "chunk_count": await KbChunk.all().count(),
        "embedded_chunk_count": await KbChunk.filter(embedded=True).count(),
        "pending_task_count": await KbIndexTask.filter(status__in=("PENDING", "PROCESSING")).count(),
    })


@router.get("/selectPage")
async def select_page(page_num: int = 1, page_size: int = 10, meeting_id: int | None = None,
                      status: str = "", current_user: User = Depends(require_admin)):
    """索引任务列表（管理员）。索引是运维可见的后台动作，员工不需要看。"""
    query = KbIndexTask.all()
    if meeting_id:
        query = query.filter(meeting_id=meeting_id)
    if status:
        query = query.filter(status=status)
    total = await query.count()
    rows = await query.order_by("-id").offset((page_num - 1) * page_size).limit(page_size)
    return Result.success({
        "total": total,
        "list": [_task_dict(row) for row in rows],
        "pageNum": page_num,
        "pageSize": page_size,
    })


@router.post("/index")
async def index(payload: IndexPayload, current_user: User = Depends(get_current_user)):
    """手动触发（或重新）索引一个来源。"""
    if payload.source_type not in (kb_service.SOURCE_TRANSCRIPT, kb_service.SOURCE_MINUTES):
        raise CustomException("source_type 只能是 TRANSCRIPT 或 MINUTES")
    meeting_id = await _meeting_of(payload.source_type, payload.source_id)
    # 索引的是会议内容，所以按"能不能看这场会"判权
    await get_accessible_meeting(meeting_id, current_user)
    try:
        task_id = await kb_service.schedule_kb_index(
            payload.source_type, payload.source_id, meeting_id, current_user.id)
    except kb_service.KbError as error:
        raise CustomException(str(error)) from error
    return Result.success({"task_id": task_id})


async def _meeting_of(source_type: str, source_id: int) -> int:
    """从来源反查会议 id：权限判断需要它，也顺手挡掉不存在的来源。"""
    from models import MeetingMinutes, TranscriptionTask

    if source_type == kb_service.SOURCE_TRANSCRIPT:
        row = await TranscriptionTask.get_or_none(id=source_id)
    else:
        row = await MeetingMinutes.get_or_none(id=source_id)
    if row is None:
        raise CustomException("来源不存在")
    return row.meeting_id


@router.post("/search")
async def search(payload: SearchPayload, current_user: User = Depends(get_current_user)):
    """检索预览：给排障与演示用，让"知识库里到底有什么"可被直接看到。

    权限在这里收敛：先算出当前用户可见的会议集合，再交给检索层。
    管理员可见全部（不过滤），员工只可见自己创建/主持/参会的会议。
    """
    allowed = await _visible_meeting_ids(current_user, payload.meeting_ids)
    try:
        hits = await kb_service.search_knowledge(
            payload.query, allowed_meeting_ids=allowed, limit=payload.limit,
            authority=payload.authority)
    except kb_service.KbError as error:
        raise CustomException(str(error)) from error
    except qdrant.QdrantError as error:
        raise CustomException(f"检索失败：{error}") from error
    return Result.success({
        "hits": [{**hit.as_citation(), "score": round(hit.score, 6)} for hit in hits],
        "scope_meeting_count": None if allowed is None else len(allowed),
    })


async def _visible_meeting_ids(current_user: User,
                               requested: list[int] | None) -> list[int] | None:
    """当前用户在检索范围内可见的会议 id 列表。

    返回 None 表示"不过滤"（仅管理员）；返回空列表表示"一个都不可见"——
    这两种情况在检索层是不同语义，不能混（混了就是不越权的反向错误：
    要么管理员看不到东西，要么无权限的用户看到全部）。
    """
    from models import Meeting

    if current_user.role == "ADMIN":
        if not requested:
            return None
        # 管理员指定了范围也照做，但仍要确认这些会议存在（避免拿到不存在的 id 当过滤条件）
        existing = await Meeting.filter(id__in=requested).values_list("id", flat=True)
        return [int(i) for i in existing]

    from models import MeetingParticipant
    from tortoise.expressions import Q

    participant_ids = await MeetingParticipant.filter(
        user_id=current_user.id).values_list("meeting_id", flat=True)
    query = Meeting.filter(
        Q(creator_id=current_user.id)
        | Q(host_id=current_user.id)
        | Q(id__in=list(participant_ids) or [-1])
    )
    if requested:
        query = query.filter(id__in=requested)
    return [int(i) for i in await query.values_list("id", flat=True)]
