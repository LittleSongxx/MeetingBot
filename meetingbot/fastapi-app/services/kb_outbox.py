"""知识库索引期望状态（outbox）与后台 reconcile。

核心不变式：**MySQL 是唯一真源，PG 里的 BM25/向量/checkpoint 都是派生或运行时数据。**

状态变化（确认/取消确认/删除/重生成/转写编辑/会议删除）不再直接“尽力而为”
地投一个索引任务，而是先在同一事务里写一行期望状态；后台 worker 按期望状态
维护派生存储。这样即使进程崩在“写状态”和“投任务”之间，重启后也能补上。

读侧还有第二道兜底：`kb_service.search_knowledge` 会按 MySQL 来源状态过滤
最终 hit。outbox 落后时，旧切片也不会被交给模型。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from common.times import now
from models import KbChunk, KbDocument, KbIndexOutbox, KbIndexTask
from tortoise.expressions import F, Q

logger = logging.getLogger("meetingbot.kb_outbox")

STATE_INDEX = "INDEX"
STATE_DELETE = "DELETE"

STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_DONE = "DONE"
STATUS_FAILED = "FAILED"

MAX_ATTEMPTS = 8
POLL_SECONDS_ENV = "KB_OUTBOX_POLL_SECONDS"


def poll_seconds() -> float:
    import os
    raw = (os.getenv(POLL_SECONDS_ENV) or "2").strip()
    try:
        return max(0.5, float(raw))
    except ValueError:
        return 2.0


async def mark_source_state(source_type: str, source_id: int, meeting_id: int,
                            desired_state: str) -> None:
    """写/更新一个来源的期望状态。可在业务事务内调用。"""
    state = STATE_INDEX if str(desired_state).upper() == STATE_INDEX else STATE_DELETE
    existing = await KbIndexOutbox.get_or_none(source_type=source_type, source_id=int(source_id))
    if existing is None:
        try:
            await KbIndexOutbox.create(
                meeting_id=int(meeting_id), source_type=source_type,
                source_id=int(source_id), desired_state=state,
                status=STATUS_PENDING, version=1, available_at=now(),
            )
            return
        except Exception:
            # 并发插入撞唯一键：退回更新路径
            existing = await KbIndexOutbox.get_or_none(
                source_type=source_type, source_id=int(source_id))
            if existing is None:
                raise
    await KbIndexOutbox.filter(id=existing.id).update(
        meeting_id=int(meeting_id), desired_state=state, status=STATUS_PENDING,
        version=F("version") + 1, attempts=0, last_error=None,
        available_at=now(), started_at=None, finished_at=None,
    )


async def _claim(row: KbIndexOutbox) -> bool:
    updated = await KbIndexOutbox.filter(
        id=row.id, status=STATUS_PENDING, version=row.version,
    ).update(status=STATUS_PROCESSING, started_at=now())
    return bool(updated)


async def _finish(row: KbIndexOutbox, *, ok: bool, error: str = "") -> None:
    # 只结束“自己认领的那一版”：处理期间 mark_source_state 可能已写入新版本，
    # 旧 worker 的完成不能把新期望状态覆盖成 DONE/FAILED。
    current = KbIndexOutbox.filter(
        id=row.id, status=STATUS_PROCESSING, version=row.version)
    if ok:
        updated = await current.update(
            status=STATUS_DONE, finished_at=now(), last_error=None, available_at=None)
        if not updated:
            logger.info("outbox %s#%s 已被新版本取代，跳过旧完成写入",
                        row.source_type, row.source_id)
        return
    attempts = int(row.attempts or 0) + 1
    if attempts >= MAX_ATTEMPTS:
        await current.update(
            status=STATUS_FAILED, attempts=attempts, last_error=(error or "")[:2000],
            finished_at=now(), available_at=None)
        return
    await current.update(
        status=STATUS_PENDING, attempts=attempts, last_error=(error or "")[:2000],
        available_at=now() + timedelta(seconds=min(300, 2 ** attempts)),
    )


async def purge_source(source_type: str, source_id: int) -> None:
    """删除一个来源在所有派生存储里的痕迹。可重复执行。"""
    from services import kb_service, pg_kb

    documents = await KbDocument.filter(source_type=source_type, source_id=int(source_id))
    chunk_ids: list[int] = []
    errors: list[str] = []
    for document in documents:
        chunk_ids.extend(await KbChunk.filter(document_id=document.id).values_list("id", flat=True))
    if chunk_ids:
        try:
            await pg_kb.delete_chunks([int(i) for i in chunk_ids])
        except Exception as error:  # noqa: BLE001 - 再按来源删一次兜底
            logger.exception("按 chunk id 删除 PG 索引失败（%s#%s）", source_type, source_id)
            errors.append(f"PG chunk: {error}")
    try:
        # 即使 MySQL 切片行已经丢了，也按来源身份把 PG 残留清掉
        await pg_kb.delete_by_source(source_type, int(source_id))
    except Exception as error:  # noqa: BLE001
        logger.exception("按来源删除 PG 索引失败（%s#%s）", source_type, source_id)
        errors.append(f"PG source: {error}")
    if errors:
        # 派生存储没清干净时不能删 MySQL 真源：否则 outbox 会标 DONE，
        # 旧检索行永久残留且再也没有依据把它找出来。
        raise RuntimeError("派生索引清理失败，保留来源等待重试：" + "；".join(errors))
    if chunk_ids:
        await KbChunk.filter(id__in=chunk_ids).delete()
    await KbDocument.filter(source_type=source_type, source_id=int(source_id)).delete()
    task = await KbIndexTask.get_or_none(source_type=source_type, source_id=int(source_id))
    if task is not None and task.status == STATUS_PROCESSING:
        try:
            await kb_service.cancel_kb_index(task.id)
        except Exception:  # noqa: BLE001
            logger.exception("取消索引任务失败（task=%s）", task.id)
    await KbIndexTask.filter(source_type=source_type, source_id=int(source_id)).delete()


async def _apply(row: KbIndexOutbox) -> None:
    from services import kb_service

    if row.desired_state == STATE_DELETE:
        await purge_source(row.source_type, row.source_id)
        return
    # INDEX：交给原有 Temporal 索引任务；execute_kb_index 会同时写 PG 的 BM25 metadata + 向量
    await kb_service.schedule_kb_index(
        row.source_type, row.source_id, row.meeting_id, initiator_id=0)


async def drain_once(*, limit: int = 20) -> int:
    """处理一批到期 outbox。返回成功处理条数。"""
    now_value = now()
    rows = await KbIndexOutbox.filter(status=STATUS_PENDING).filter(
        Q(available_at__isnull=True) | Q(available_at__lte=now_value)
    ).order_by("id").limit(limit)
    processed = 0
    for row in rows:
        if not await _claim(row):
            continue
        try:
            await _apply(row)
            await _finish(row, ok=True)
            processed += 1
        except Exception as error:  # noqa: BLE001 - 失败退避，不打断其它行
            logger.exception("outbox %s#%s 处理失败", row.source_type, row.source_id)
            await _finish(row, ok=False, error=str(error) or type(error).__name__)
    return processed


async def reconcile_all(*, limit: int = 200) -> int:
    """启动兜底：清掉“来源已不可索引但仍有文档”的孤儿。

    不做全量重建（会重复付费）；只处理明确的 DELETE 语义：
    - 来源行不存在；
    - MINUTES 不是 CONFIRMED；
    - TRANSCRIPT 不是 SUCCEEDED。
    """
    from models import MeetingMinutes, TranscriptionTask

    documents = await KbDocument.all().limit(limit)
    cleaned = 0
    for document in documents:
        keep = False
        if document.source_type == "MINUTES":
            row = await MeetingMinutes.get_or_none(id=document.source_id)
            keep = row is not None and row.status == "CONFIRMED"
        elif document.source_type == "TRANSCRIPT":
            row = await TranscriptionTask.get_or_none(id=document.source_id)
            keep = row is not None and row.status == "SUCCEEDED"
        if not keep:
            await purge_source(document.source_type, document.source_id)
            await KbIndexOutbox.filter(
                source_type=document.source_type, source_id=document.source_id,
            ).update(status=STATUS_DONE, desired_state=STATE_DELETE, finished_at=now())
            cleaned += 1
    return cleaned


async def loop(stop_event: asyncio.Event | None = None) -> None:
    """后台轮询。多个副本同时跑也安全：claim 是条件更新。"""
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            await drain_once()
        except Exception:  # noqa: BLE001
            logger.exception("outbox drain 失败，下一轮重试")
        await asyncio.sleep(poll_seconds())


__all__ = [
    "STATE_INDEX", "STATE_DELETE", "mark_source_state", "drain_once",
    "reconcile_all", "purge_source", "loop",
]
