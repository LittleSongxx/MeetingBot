"""问答接口：会话管理与逐 token 的 SSE 流。

## 为什么是 SSE 而不是 WebSocket

问答是**单向**的（服务端持续产出、客户端只读），SSE 正好覆盖，且自带断线重连、
能直接走普通 HTTP 与现有鉴权头；WebSocket 需要另建握手、心跳与重连协议，
换来的双向能力这里用不上。

## 反代下的两条硬要求

- `X-Accel-Buffering: no`：nginx 默认会把响应缓冲到一定大小再转发，那样用户
  看到的是"卡住很久然后一次吐出全文"，逐 token 效果整个失效。
- `Cache-Control: no-cache`：中间层缓存流式响应会串答案。
- 15 秒心跳注释行：连接空闲过久会被中间层掐断；注释行（以 `:` 开头）是 SSE
  规范里客户端必须忽略的载荷，因此心跳不会污染消息流。

## 事件类型

`meta`（这条回答的身份）→ `tool_start` / `tool_end`（查了什么）→ `citation`
（可点回的引用，带编号）→ `token`（正文增量）→ `done`（收尾与用量）；
出错则 `error`。**事件语义由 `chat_service.stream_answer` 定义**，
这里只负责包帧、加心跳头与断开收尾——MCP 侧完全不经过这一层。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any, AsyncIterator

from common import providers, qdrant
from common.auth import get_current_user
from common.exception_handler import CustomException
from common.result import Result
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from models import AiModelConfig, ChatSession, User
from pydantic import BaseModel, Field
from services import chat_service

router = APIRouter(prefix="/chat")

# 心跳间隔（秒）。取 15 是反代空闲超时的常见下限（nginx 默认 60s）的安全值
HEARTBEAT_SECONDS = float(os.getenv("CHAT_HEARTBEAT_SECONDS", "15"))
# 断开连接后等待落库收尾的上限：不设上限会把"客户端走了"变成"服务端挂住"
CLOSE_PERSIST_TIMEOUT = 10.0


class SessionPayload(BaseModel):
    meeting_ids: list[int] | None = Field(
        default=None, description="本次对话限定的会议；留空表示当前用户可见的全部会议")


class StreamPayload(BaseModel):
    session_id: int
    question: str


def _session_dict(session: ChatSession) -> dict[str, Any]:
    return {
        "id": session.id, "title": session.title,
        "scope_meeting_ids": session.scope_meeting_ids or [],
        "message_count": session.message_count,
    }


@router.get("/status")
async def status(current_user: User = Depends(get_current_user)):
    """聊天是否可用：模型配好没有、供应商支不支持工具调用、向量库通不通。

    页面据此显示一条横幅，而不是让用户问一句再看到报错。
    """
    config = await AiModelConfig.filter(model_type="AGENT", enabled=True).order_by("-id").first()
    embedding = await AiModelConfig.filter(model_type="EMBEDDING", enabled=True).order_by("-id").first()
    return Result.success({
        "model": config.model_name if config else None,
        "model_configured": config is not None,
        "tool_calling_supported": bool(config) and providers.supports_tool_calling(config.base_url),
        "embedding_configured": embedding is not None,
        "vector_store_reachable": await qdrant.health(),
    })


@router.post("/session")
async def create_session(payload: SessionPayload,
                         current_user: User = Depends(get_current_user)):
    try:
        session = await chat_service.create_session(current_user, meeting_ids=payload.meeting_ids)
    except chat_service.ChatError as error:
        raise CustomException(str(error)) from error
    return Result.success(_session_dict(session))


@router.get("/selectSessionPage")
async def select_session_page(page_num: int = 1, page_size: int = 20,
                              current_user: User = Depends(get_current_user)):
    return Result.success(await chat_service.list_sessions(
        current_user, page_num=page_num, page_size=page_size))


@router.delete("/session")
async def delete_session(session_id: int, current_user: User = Depends(get_current_user)):
    try:
        await chat_service.delete_session(session_id, current_user)
    except chat_service.ChatError as error:
        raise CustomException(str(error)) from error
    return Result.success()


@router.get("/messages")
async def messages(session_id: int, limit: int = 100,
                   current_user: User = Depends(get_current_user)):
    try:
        rows = await chat_service.list_messages(session_id, current_user, limit=limit)
    except chat_service.ChatError as error:
        raise CustomException(str(error)) from error
    return Result.success({"list": rows})


@router.post("/stream")
async def stream(payload: StreamPayload, current_user: User = Depends(get_current_user)):
    """一次问答的 SSE 流。POST 而非 GET：问题可能很长，不该塞进查询串。"""
    try:
        session = await chat_service.get_session(payload.session_id, current_user)
    except chat_service.ChatError as error:
        raise CustomException(str(error)) from error
    source = chat_service.stream_answer(session, payload.question, user=current_user)
    return StreamingResponse(
        _sse_frames(source),
        media_type="text/event-stream",
        headers={
            # 关掉反向代理的响应缓冲，否则逐 token 会退化成"憋一大段再吐"
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


async def _sse_frames(source: AsyncIterator[tuple[str, dict[str, Any]]],
                      *, heartbeat: float = HEARTBEAT_SECONDS) -> AsyncIterator[str]:
    """把事件流包成 SSE 帧，并在空闲时插入心跳注释行。

    事件生产与帧生成分成两个任务（队列连接）：这样"等下一个事件"可以带超时，
    超时就发心跳——同一个协程里既等事件又等超时是做不到的。
    """
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in source:
                await queue.put(("event", item))
        except asyncio.CancelledError:
            # 取消是**预期路径**：生产端已经在上面的 await 处收到取消并完成收尾
            # （落库那一轮的结果）。这里吞掉它，任务才是"正常结束"——
            # 否则取消会被当成任务的异常结果再抛给等待方，收尾反倒像是失败。
            return
        except Exception as error:  # noqa: BLE001 - 生产端异常要以 error 事件交给用户
            await queue.put(("failure", (str(error) or type(error).__name__)[:500]))
        finally:
            await queue.put(("end", None))

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                # 注释行：SSE 客户端必须忽略，因此心跳不会混进消息
                yield ": ping\n\n"
                continue
            if kind == "end":
                break
            if kind == "failure":
                yield _frame("error", {"message": payload})
                break
            name, data = payload
            yield _frame(name, data)
    finally:
        # 客户端断开时也要让生成器收尾（落库那一轮的结果），否则会话里会留下
        # 一条永远空的助手消息，用户自己都不知道发生过什么
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(task), timeout=CLOSE_PERSIST_TIMEOUT)
        # 双保险：取消可能发生在"生成器正停在一个 yield 上"的时刻，那种情况它收不到
        # 取消异常，只能靠关闭它触发收尾（`stream_answer` 对 GeneratorExit 同样落库）。
        with contextlib.suppress(BaseException):
            await source.aclose()


def _frame(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
