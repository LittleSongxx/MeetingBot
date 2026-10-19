"""对话 SSE 事件日志（Redis Stream）。

用途只有一个：客户端断线重连时，先把 **已经发过的事件** 按 `Last-Event-ID`
回放，再从 PostgreSQL checkpoint 继续生成。没有它，checkpoint 只能恢复图状态，
不能恢复"用户已经看到/没看到哪些 token"。

设计边界：
- 事件流是**短生命周期的 UI 日志**，不是业务真源；TTL 到期就丢，最终答案仍在
  `chat_message`。
- Redis 不可用时所有操作降级为 no-op；调用方退回"DB 正文前缀 + checkpoint"。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger("meetingbot.chat_events")

_runtime: redis.Redis | None = None


def _url() -> str:
    return (os.getenv("REDIS_URL") or "").strip()


def ttl_seconds() -> int:
    raw = (os.getenv("CHAT_EVENT_TTL_SECONDS") or "3600").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 3600


def _client() -> redis.Redis | None:
    global _runtime
    if _runtime is not None:
        return _runtime
    url = _url()
    if not url:
        return None
    try:
        _runtime = redis.from_url(url, decode_responses=True)
    except Exception:  # noqa: BLE001 - 事件日志是增强
        logger.exception("Redis 事件流初始化失败，SSE 续传降级")
        return None
    return _runtime


def key(message_id: int) -> str:
    return f"chat:events:{int(message_id)}"


async def append(message_id: int, event: str, data: dict[str, Any]) -> str | None:
    """追加一条事件，返回 Redis Stream id；Redis 不可用返回 None。"""
    client = _client()
    if client is None:
        return None
    try:
        event_id = await client.xadd(
            key(message_id),
            {"event": str(event), "data": json.dumps(data, ensure_ascii=False)},
        )
        await client.expire(key(message_id), ttl_seconds())
        return str(event_id)
    except Exception:  # noqa: BLE001
        logger.exception("写 SSE 事件失败（message=%s）", message_id)
        return None


async def read_after(message_id: int, last_event_id: str | None,
                     *, limit: int = 1000) -> list[tuple[str, str, dict[str, Any]]]:
    """读取 last_event_id 之后的事件；无 Redis/无事件返回空。"""
    client = _client()
    if client is None:
        return []
    start = f"({last_event_id}" if last_event_id else "-"
    try:
        rows = await client.xrange(key(message_id), min=start, max="+", count=limit)
    except Exception:  # noqa: BLE001
        logger.exception("读 SSE 事件失败（message=%s）", message_id)
        return []
    events: list[tuple[str, str, dict[str, Any]]] = []
    for event_id, fields in rows:
        try:
            events.append((
                str(event_id),
                str(fields.get("event") or "message"),
                json.loads(str(fields.get("data") or "{}")),
            ))
        except (TypeError, ValueError):
            continue
    return events


def terminal(events: list[tuple[str, str, dict[str, Any]]]) -> bool:
    return any(name in {"done", "error"} for _, name, _ in events)


async def close() -> None:
    global _runtime
    if _runtime is not None:
        await _runtime.aclose()
        _runtime = None


__all__ = ["append", "read_after", "terminal", "key", "close", "ttl_seconds"]
