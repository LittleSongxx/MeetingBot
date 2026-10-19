"""问答会话 CRUD 与消息展示。"""

from __future__ import annotations

import logging
from typing import Any

from common import access
from common.times import format_datetime, now
from models import ChatMessage, ChatSession, User

from services import (
    chat_graph,
)
from services.chat_prompts import ChatError

logger = logging.getLogger("meetingbot.chat")

def _fmt(value):
    return format_datetime(value)


MESSAGE_USER = "USER"
MESSAGE_ASSISTANT = "ASSISTANT"


SESSION_TITLE_CHARS = 30


async def create_session(user: User, *, meeting_ids: list[int] | None = None,
                         series_ids: list[int] | None = None) -> ChatSession:
    """建会话。范围先过一遍权限：用户无权看的会议/系列不会进范围（静默取交集）。

    `scope_series_ids` 只存系列 id，不展开成会议：系列后续新增会议时，
    权限仍在每轮按当前可见会议实时判定，不会把"建会话时的快照"当成授权。
    """
    scoped = await access.visible_meeting_ids(user, requested=meeting_ids or None)
    scoped_series = await access.visible_series_ids(user, requested=series_ids or None)
    return await ChatSession.create(
        user_id=user.id, title="新对话",
        scope_meeting_ids=[] if scoped is None else sorted(scoped),
        scope_series_ids=[] if scoped_series is None else sorted(scoped_series),
        last_active_time=now(),
    )


async def get_session(session_id: int, user: User) -> ChatSession:
    session = await ChatSession.get_or_none(id=session_id, user_id=user.id, status="ACTIVE")
    if session is None:
        raise ChatError("会话不存在或无权访问")
    return session


async def list_sessions(user: User, *, page_num: int = 1, page_size: int = 20) -> dict[str, Any]:
    query = ChatSession.filter(user_id=user.id, status="ACTIVE")
    total = await query.count()
    rows = await query.order_by("-last_active_time", "-id").offset(
        (page_num - 1) * page_size).limit(page_size)
    return {"total": total, "list": [{
        "id": row.id, "title": row.title, "scope_meeting_ids": row.scope_meeting_ids or [],
        "scope_series_ids": row.scope_series_ids or [],
        "message_count": row.message_count,
        "last_active_time": _fmt(row.last_active_time), "create_time": _fmt(row.create_time),
    } for row in rows], "pageNum": page_num, "pageSize": page_size}


async def delete_session(session_id: int, user: User) -> None:
    """软删：消息保留（问答是有引用与追责价值的记录，不随会话删除一起消失）。"""
    session = await get_session(session_id, user)
    await ChatSession.filter(id=session.id).update(status="DELETED")


async def list_messages(session_id: int, user: User, *, limit: int = 100) -> list[dict[str, Any]]:
    await get_session(session_id, user)
    rows = await ChatMessage.filter(session_id=session_id).order_by("-id").limit(limit)
    return [_message_dict(row) for row in reversed(rows)]


def _message_dict(row: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": row.id, "role": row.role, "content": row.content,
        "stop_reason": row.stop_reason, "error_message": row.error_message,
        "token_count": row.token_count, "create_time": _fmt(row.create_time),
        "citations": (row.citations_json or {}).get("used", []),
        # 检索到但答案没引用的也一并给出：它既是页面"查到了什么"的数据源，
        # 也是评测侧算检索指标（recall@k / MRR）的分母——只给被引用的会把召回算高
        "retrieved": (row.citations_json or {}).get("all", []),
        "tools": row.tools_json or [],
        # 查询规划留痕（没有规划过就是 None）：页面不渲染它，但复盘时要能对上
        "plan": row.plan_json,
        # 这一轮的生命周期与恢复入口（页面据此判断"正在生成/可续传"）
        "run_status": row.run_status,
        "thread_id": row.thread_id,
        "checkpoint_id": row.checkpoint_id,
        "trace_id": row.trace_id,
        "charged_tokens": row.charged_tokens,
    }
    if row.stop_reason and row.stop_reason != chat_graph.STOP_FINISHED:
        payload["notice"] = chat_graph.STOP_NOTICE.get(row.stop_reason, "")
    return payload

