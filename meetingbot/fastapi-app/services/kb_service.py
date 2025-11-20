"""知识库服务：把转写与已确认纪要索引成可检索切片，并提供混合检索。

## 摄入从哪来

索引不是独立的手工动作，而是挂在产品已有的状态迁移上：

- 转写 `SUCCEEDED` → 自动入队索引它的全文（RAW）
- 纪要 `CONFIRMED` → 自动入队索引它的条目（CONFIRMED）

**草稿纪要永远不进库**：这是把"人拍板才算数"这条不变量延伸到知识库——
答案更该引权威条目而不是模型草稿，所以 `index_minutes` 会拒绝非 CONFIRMED 的纪要。

## 幂等与省钱

沿用本仓既有的两层幂等：workflow id 拦"正在跑"，任务行的条件更新拦"重复执行"。
在此之上多一层**内容指纹**：来源内容没变就直接成功返回，不重嵌、不重写向量；
内容变了才重建文档版本。嵌入失败重试时只补 `embedded=0` 的切片——
与纪要的磁盘缓存同款理由：不为已经成功过的部分重复付费。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from common import qdrant
from common.times import now, to_local
from models import (
    AiModelConfig,
    KbChunk,
    KbDocument,
    KbIndexTask,
    Meeting,
    MeetingMinutes,
    TranscriptionTask,
    TranscriptSegment,
    User,
)
from temporalio.exceptions import ApplicationError

from services import kb_chunking as chunking
from services import kb_sparse, model_factory, task_queue

logger = logging.getLogger("meetingbot.kb")

KIND_KB_INDEX = "kb-index"

SOURCE_TRANSCRIPT = "TRANSCRIPT"
SOURCE_MINUTES = "MINUTES"

# 状态值（与 kb_index_task.status 取值一致）
PENDING, PROCESSING, SUCCEEDED, FAILED = "PENDING", "PROCESSING", "SUCCEEDED", "FAILED"

# 单次写入向量库的点数：分批是为了单次请求体不要过大，也让失败重试的粒度更细
_UPSERT_BATCH_SIZE = 64

# 向量库 payload 的**结构版本**。加/改/删 payload 字段时必须 +1——它进内容指纹
# （见 content_fingerprint），从而让旧切片在下一次索引时自动重建。
# 历史：1 = 最早的一版（无 meeting_start_epoch）；2 = 加会议开始时间（时间过滤用）。
PAYLOAD_SCHEMA_VERSION = 2


class KbError(RuntimeError):
    """知识库相关的可读错误（接口层转成页面提示）。"""


@dataclass(frozen=True)
class Hit:
    """一条检索结果。字段够回答"哪场会、谁、什么时候说的、原文是什么"。"""

    chunk_id: int
    score: float
    text: str
    meeting_id: int
    authority: str
    chunk_type: str
    field: str | None
    item_index: int | None
    speaker: str | None
    start_ms: int | None
    end_ms: int | None
    document_id: int | None
    origin: str | None

    def as_citation(self) -> dict[str, Any]:
        """转成给前端/工具用的引用结构（时间戳换算成秒，和转写行格式一致）。"""
        return {
            "chunk_id": self.chunk_id,
            "meeting_id": self.meeting_id,
            "authority": self.authority,
            "chunk_type": self.chunk_type,
            "field": self.field,
            "item_index": self.item_index,
            "speaker": self.speaker,
            "start_second": (self.start_ms or 0) // 1000 if self.start_ms is not None else None,
            "end_second": (self.end_ms or 0) // 1000 if self.end_ms is not None else None,
            "quote": self.text,
        }


# --------------------------------------------------------------------------- #
# 来源装载与指纹
# --------------------------------------------------------------------------- #

async def _embedding_config() -> AiModelConfig:
    config = await AiModelConfig.filter(model_type="EMBEDDING", enabled=True).order_by("-id").first()
    if config is None:
        raise KbError("没有启用中的嵌入模型配置，请先在「AI模型配置」里启用一个 EMBEDDING 用途的配置")
    return config


async def transcript_chunks(task_id: int) -> tuple[list[chunking.Chunk], str]:
    """把一段转写切成窗口，并返回内容指纹。"""
    segments = await TranscriptSegment.filter(task_id=task_id).order_by("segment_no")
    if not segments:
        return [], ""
    # 说话人姓名一次性查出（与 minutes_service.transcript_text 同款做法，不在循环里查库）
    user_ids = {s.speaker_user_id for s in segments if s.speaker_user_id}
    users = {u.id: u.name for u in await User.filter(id__in=user_ids)} if user_ids else {}
    lines = []
    for segment in segments:
        speaker = users.get(segment.speaker_user_id) or segment.speaker_label or "未知说话人"
        lines.append((segment.start_ms, segment.end_ms, speaker, segment.content))
    chunks = chunking.chunk_transcript(lines)
    return chunks, content_fingerprint([c.text for c in chunks])


async def minutes_chunks(minutes_id: int) -> tuple[list[chunking.Chunk], str]:
    """把一份**已确认**纪要切成条目切片。草稿会被拒绝。"""
    minutes = await MeetingMinutes.get_or_none(id=minutes_id)
    if minutes is None:
        raise KbError("纪要不存在")
    if minutes.status != "CONFIRMED":
        raise KbError(f"只有已确认的纪要才能进知识库（当前状态 {minutes.status}）")
    from services import contracts

    items = {name: getattr(minutes, f"{name}_json") or [] for name in contracts.LIST_FIELDS}
    summary = minutes.summary
    # 存储态是 v1/v2 混合，先收敛到当前契约再切块（与展示、导出同一处适配）
    upgraded = contracts.upgrade_output({
        "summary": {"text": summary or "", "basis": "stated" if summary else "not_mentioned"},
        **{name: items[name] for name in contracts.LIST_FIELDS},
    })
    chunks = chunking.chunk_minutes(
        upgraded.get("summary"), upgraded, field_order=contracts.FIELD_ORDER)
    return chunks, content_fingerprint([c.text for c in chunks])


def content_fingerprint(texts: Sequence[str], *,
                        payload_schema: int = PAYLOAD_SCHEMA_VERSION) -> str:
    """内容指纹：切片集合变了就换指纹，据此判断要不要重建文档版本。

    **载荷结构版本也进指纹**，这一条是踩出来的：指纹原先只覆盖切片正文，
    于是"给 payload 加一个字段"不会触发重建——旧 point 上根本没有那个新字段，
    而查询侧一旦按它过滤，旧数据会被**静默漏掉**（过滤看起来生效了，只是少了点数据，
    不报错、不告警）。把结构版本并进来之后，改结构 = 换指纹 = 下次索引自动重建。
    """
    digest = hashlib.sha256()
    digest.update(f"payload-schema:{int(payload_schema)}\x00".encode())
    for text in texts:
        digest.update(text.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 索引执行（Temporal activity 的实体）
# --------------------------------------------------------------------------- #

async def schedule_kb_index(source_type: str, source_id: int, meeting_id: int,
                            initiator_id: int) -> int:
    """建/复用索引任务行并交给 Temporal。返回任务 id。"""
    config = await _embedding_config()
    task = await KbIndexTask.get_or_none(source_type=source_type, source_id=source_id)
    if task is None:
        task = await KbIndexTask.create(
            meeting_id=meeting_id, source_type=source_type, source_id=source_id,
            model_config_id=config.id, initiator_id=initiator_id,
            status=PENDING, stage="等待执行", progress=0,
        )
    elif task.status == PROCESSING:
        return task.id  # 正在跑，不放新任务（activity 里的条件抢占也会兜住）
    else:
        # 重新索引：状态回到 PENDING，清掉错误与结果，等 activity 抢占
        await KbIndexTask.filter(id=task.id).update(
            status=PENDING, stage="等待重新索引", progress=0, error_message=None,
            document_id=None, chunk_count=0, embedded_count=0,
            model_config_id=config.id,
        )
    await task_queue.enqueue(KIND_KB_INDEX, task.id, (task.id,))
    return task.id


def schedule_kb_index_in_background(source_type: str, source_id: int, meeting_id: int,
                                     initiator_id: int) -> None:
    """尽力而为地在后台排一个索引任务。

    **索引失败不能影响转写/纪要主流程**：这两条才是产品的核心链路，知识库是增值能力。
    所以这里不抛异常，失败只记日志；任务行的状态与重试仍然由索引任务自己承担。
    """
    async def _run() -> None:
        try:
            await schedule_kb_index(source_type, source_id, meeting_id, initiator_id)
        except Exception:
            logger.exception("排知识库索引失败（不影响主流程）：%s#%s", source_type, source_id)

    asyncio.get_running_loop().create_task(_run())


async def cancel_kb_index(task_id: int) -> None:
    await task_queue.cancel(KIND_KB_INDEX, task_id)


async def recover_kb_index_tasks() -> int:
    """启动兜底：把"行已写但 workflow 未启动"的窄窗口补上（其余交给 Temporal）。"""
    recovered = 0
    for task in await KbIndexTask.filter(status__in=(PENDING, PROCESSING)):
        await task_queue.enqueue(KIND_KB_INDEX, task.id, (task.id,))
        recovered += 1
    return recovered


async def execute_kb_index(kb_task_id: int) -> None:
    """索引一场转写或一份已确认纪要。可重入：抢不到状态就直接退出。"""
    # 条件抢占：第二个执行者更新 0 行，直接退出（与转写/纪要同款）
    claimed = await KbIndexTask.filter(id=kb_task_id, status=PENDING).update(
        status=PROCESSING, stage="正在准备切片", progress=5,
        started_at=now(), finished_at=None, error_message=None,
    )
    if not claimed:
        return

    try:
        task = await KbIndexTask.get(id=kb_task_id)
        config = await AiModelConfig.get_or_none(id=task.model_config_id, enabled=True)
        if config is None:
            raise KbError("嵌入模型配置不存在或已停用")

        chunks, fingerprint = await _load_source_chunks(task)
        if not chunks:
            await _finish(task.id, chunk_count=0, embedded_count=0, stage="来源没有可索引的内容")
            return

        existing = await KbDocument.filter(
            source_type=task.source_type, source_id=task.source_id, status=SUCCEEDED,
        ).order_by("-id").first()

        if existing is not None and existing.content_hash == fingerprint:
            # 内容没变：不重嵌、不重写向量。这就是"重试不为已成功的部分重复付费"
            await KbIndexTask.filter(id=task.id).update(document_id=existing.id)
            await _finish(task.id, chunk_count=existing.chunk_count,
                          embedded_count=existing.chunk_count, stage="内容未变化，跳过重建")
            return

        await KbIndexTask.filter(id=task.id).update(stage="正在切片", progress=15)

        if existing is None:
            document = await KbDocument.create(
                meeting_id=task.meeting_id, source_type=task.source_type, source_id=task.source_id,
                task_id=task.id, authority=await _authority_of(task),
                content_hash=fingerprint, chunk_count=len(chunks),
                embed_model=config.model_name, embed_dim=model_factory.embedding_dim(),
            )
        else:
            # 内容变了：重建同一行（旧切片与其向量先清掉，避免检索到过期版本）
            document = existing
            await _drop_chunks(document.id)
            await KbDocument.filter(id=document.id).update(
                task_id=task.id, content_hash=fingerprint, chunk_count=len(chunks),
                embed_model=config.model_name, embed_dim=model_factory.embedding_dim(),
                status=SUCCEEDED, error_message=None, create_time=now(),
            )
        await KbIndexTask.filter(id=task.id).update(document_id=document.id)

        rows = [KbChunk(
            document_id=document.id, meeting_id=task.meeting_id, chunk_no=chunk.chunk_no,
            chunk_type=chunk.chunk_type, text=chunk.text, speaker=chunk.speaker,
            start_ms=chunk.start_ms, end_ms=chunk.end_ms, field=chunk.field,
            item_index=chunk.item_index, origin=chunk.origin, evidence_json=chunk.evidence,
            token_count=len(chunk.text), embedded=False,
        ) for chunk in chunks]
        await KbChunk.bulk_create(rows)
        created = await KbChunk.filter(document_id=document.id).order_by("chunk_no")

        await KbIndexTask.filter(id=task.id).update(
            chunk_count=len(created), stage=f"正在生成向量（共 {len(created)} 片）", progress=30)

        embedded = await _embed_and_store(task, config, document, created,
                                          await _meeting_start_epoch(task.meeting_id))
        await _finish(task.id, chunk_count=len(created), embedded_count=embedded,
                      stage="索引完成", document_id=document.id)
    except KbError as error:
        # 永久性错误（来源不存在 / 状态不对 / 没配嵌入模型）：重试多少次结果都一样，
        # 标成 non_retryable，别让 Temporal 白跑 5 轮又白等退避
        detail = (str(error) or type(error).__name__)[:2000]
        await _mark_failed(kb_task_id, detail)
        raise ApplicationError(detail, non_retryable=True) from error
    except Exception as error:  # noqa: BLE001 - 统一落状态后再抛，交由 Temporal 重试
        detail = (str(error) or type(error).__name__)[:2000]
        await _mark_failed(kb_task_id, detail)
        raise


async def _mark_failed(kb_task_id: int, detail: str) -> None:
    """失败收尾：任务与它产出的文档一起落 FAILED（文档失败原因要留在库里，便于排障）。"""
    await KbIndexTask.filter(id=kb_task_id).update(
        status=FAILED, stage="索引失败", error_message=detail, finished_at=now())
    row = await KbIndexTask.get_or_none(id=kb_task_id)
    if row is not None and row.document_id:
        await KbDocument.filter(id=row.document_id).update(status="FAILED", error_message=detail)


async def _load_source_chunks(task: KbIndexTask) -> tuple[list[chunking.Chunk], str]:
    if task.source_type == SOURCE_TRANSCRIPT:
        transcription = await TranscriptionTask.get_or_none(id=task.source_id)
        if transcription is None:
            raise KbError("转写任务不存在")
        if transcription.status != "SUCCEEDED":
            raise KbError(f"只有转写成功的任务才能建索引（当前状态 {transcription.status}）")
        return await transcript_chunks(task.source_id)
    if task.source_type == SOURCE_MINUTES:
        return await minutes_chunks(task.source_id)
    raise KbError(f"未知的来源类型：{task.source_type}")


async def _authority_of(task: KbIndexTask) -> str:
    return chunking.AUTHORITY_CONFIRMED if task.source_type == SOURCE_MINUTES else chunking.AUTHORITY_RAW


async def _drop_chunks(document_id: int) -> None:
    """清掉一个文档版本的切片与其向量。"""
    ids = await KbChunk.filter(document_id=document_id).values_list("id", flat=True)
    if ids:
        await qdrant.delete_points(list(ids))
    await KbChunk.filter(document_id=document_id).delete()


async def _embed_and_store(task: KbIndexTask, config: AiModelConfig, document: KbDocument,
                           chunks: Sequence[KbChunk],
                           meeting_start_epoch: int | None = None) -> int:
    """嵌入未完成的切片并写入向量库，返回已嵌入的切片数。

    只处理 `embedded=False`：中途失败重跑时，已写成功的部分不重复付费。
    """
    await qdrant.ensure_collection(model_factory.embedding_dim())
    pending = [chunk for chunk in chunks if not chunk.embedded]
    if not pending:
        return len(chunks)

    done = len(chunks) - len(pending)
    for start in range(0, len(pending), _UPSERT_BATCH_SIZE):
        batch = pending[start:start + _UPSERT_BATCH_SIZE]
        vectors = await model_factory.embed_texts(
            config, [chunk.text for chunk in batch], biz_id=task.id, call_type="KB_EMBEDDING")
        points = []
        for chunk, vector in zip(batch, vectors, strict=True):
            points.append({
                "id": chunk.id,
                "dense": vector,
                "sparse": kb_sparse.encode(chunk.text),
                "payload": _payload_for(task, document, chunk, meeting_start_epoch),
            })
        await qdrant.upsert_chunks(points)
        await KbChunk.filter(id__in=[c.id for c in batch]).update(embedded=True)
        done += len(batch)
        progress = 30 + int(60 * done / max(1, len(chunks)))
        await KbIndexTask.filter(id=task.id).update(
            progress=progress, embedded_count=done,
            stage=f"正在生成向量（{done}/{len(chunks)}）")
    return done


async def _meeting_start_epoch(meeting_id: int) -> int | None:
    """会议开始时间的 epoch 秒（时间过滤用）。会议不存在或没填开始时间就返回 None。"""
    start_time = await Meeting.filter(id=meeting_id).values_list("start_time", flat=True)
    value = start_time[0] if start_time else None
    return int(to_local(value).timestamp()) if value else None


def _payload_for(task: KbIndexTask, document: KbDocument, chunk: KbChunk,
                 meeting_start_epoch: int | None) -> dict[str, Any]:
    """写进向量库的载荷：检索阶段不回表就能回答"哪场会、谁、什么时候说的"。

    `meeting_id` 与 `authority` 是过滤字段（权限过滤、权威性过滤），必须在这里带上。
    `meeting_start_epoch` 是**会议日期**（不是录音内偏移）："最近七天""上周那场会"
    这类限定按它过滤，缺了它就只能靠语义相似度碰运气（实测：那些限定一律碰不到）。
    """
    return {
        "meeting_id": task.meeting_id,
        "document_id": document.id,
        "authority": document.authority,
        "source_type": task.source_type,
        "source_id": task.source_id,
        "chunk_type": chunk.chunk_type,
        "field": chunk.field,
        "item_index": chunk.item_index,
        "origin": chunk.origin,
        "speaker": chunk.speaker,
        "start_ms": chunk.start_ms,
        "end_ms": chunk.end_ms,
        "meeting_start_epoch": meeting_start_epoch,
        "text": chunk.text,
    }


async def _finish(kb_task_id: int, *, chunk_count: int, embedded_count: int, stage: str,
                  document_id: int | None = None) -> None:
    values: dict[str, Any] = {
        "status": SUCCEEDED, "stage": stage, "progress": 100,
        "chunk_count": chunk_count, "embedded_count": embedded_count,
        "finished_at": now(), "error_message": None,
    }
    if document_id is not None:
        values["document_id"] = document_id
    await KbIndexTask.filter(id=kb_task_id).update(**values)


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #

async def search_knowledge(
    query: str,
    *,
    allowed_meeting_ids: Sequence[int] | None,
    limit: int = 8,
    authority: Iterable[str] | None = None,
    chunk_type: Iterable[str] | None = None,
    meeting_time_from: int | None = None,
    meeting_time_to: int | None = None,
    biz_id: int = 0,
) -> list[Hit]:
    """混合检索（可选 rerank）。`allowed_meeting_ids` 由权限层给出，本函数不做权限判断。

    None 表示管理员（不过滤），空集合表示无可见会议（返回空）——两者语义不同，
    由上层的 access 层负责算清楚，这里只如实执行。

    另外三组过滤都是**业务限定**（切片类型、会议时间范围），与权限一样是"必须满足"：
    它们解决的是"下午这场会""最近七天"这类语义相似度碰不到的限定。
    时间按**会议开始时间**过滤（epoch 秒），不是录音内偏移。

    `biz_id` 只影响 `ai_call_log` 的归属（哪条业务记录引发了这次调用）：
    问答传入那条助手消息的 id，接口预览传 0。0 表示"没有业务归属"，
    观测页据此把它算作平台级调用。
    """
    text = (query or "").strip()
    if not text:
        return []
    config = await _embedding_config()
    vectors = await model_factory.embed_texts(config, [text], biz_id=biz_id,
                                              call_type="KB_QUERY_EMBED")
    dense = vectors[0]
    # rerank 开着时先多召回一批候选，再由模型挑出真正能回答问题的那些
    fetch = max(int(limit), RERANK_CANDIDATES) if rerank_enabled() else int(limit)
    raw = await qdrant.hybrid_search(
        dense=dense, sparse=kb_sparse.encode(text), limit=fetch,
        allowed_meeting_ids=allowed_meeting_ids, authority=authority,
        chunk_type=chunk_type, meeting_time_from=meeting_time_from,
        meeting_time_to=meeting_time_to,
    )
    hits: list[Hit] = []
    for item in raw:
        payload = item["payload"]
        hits.append(Hit(
            chunk_id=item["chunk_id"], score=item["score"],
            text=str(payload.get("text") or ""),
            meeting_id=int(payload.get("meeting_id") or 0),
            authority=str(payload.get("authority") or ""),
            chunk_type=str(payload.get("chunk_type") or ""),
            field=payload.get("field"), item_index=payload.get("item_index"),
            speaker=payload.get("speaker"), start_ms=payload.get("start_ms"),
            end_ms=payload.get("end_ms"), document_id=payload.get("document_id"),
            origin=payload.get("origin"),
        ))
    if rerank_enabled() and len(hits) > 1:
        hits = await _rerank(text, hits, limit=int(limit), biz_id=biz_id)
    return hits


# --------------------------------------------------------------------------- #
# rerank（可选增强，默认关）
# --------------------------------------------------------------------------- #

# rerank 的调用类型（新调用类型必须登记 CALL_TYPE_OWNERSHIP，见 observability_service）
RERANK_CALL_TYPE = "KB_RERANK"
# 先召回多少候选再重排。取与 Qdrant 的 PREFETCH_LIMIT 同量级：再大就超出单次
# 融合的召回深度，重排也就无从挑起了。
RERANK_CANDIDATES = qdrant.PREFETCH_LIMIT

RERANK_SYSTEM = (
    "你在给检索结果排序。给你一个问题与若干候选片段（各带编号）。"
    "按「这段内容能不能回答这个问题」从强到弱排序，最相关的排最前。"
    "只是提到关键词但答不了问题的排在后面。不要解释、不要改写内容。"
)
RERANK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"ranking": {"type": "array", "items": {"type": "integer"}}},
    "required": ["ranking"],
}


def rerank_enabled() -> bool:
    """rerank 开关。**默认关**：新机制默认关是本仓的规矩（未实测就开默认，
    等于把一个没量过的机制塞进主链路），要开就显式配 KB_RERANK_ENABLED=1。"""
    return os.getenv("KB_RERANK_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


async def _chat_config() -> AiModelConfig:
    config = await AiModelConfig.filter(model_type="AGENT", enabled=True).order_by("-id").first()
    if config is None:
        raise KbError("没有启用中的 AGENT 用途模型配置，无法做重排")
    return config


def _ranking_of(parsed: Any, size: int) -> list[int]:
    """把模型给的排序整理成合法的下标序列：去重、剔除越界、补上没提到的。

    **补全而不是丢弃**：模型只回了前几名是常态，剩下那些按原顺序接在后面——
    否则"模型没提"会变成"这些候选被删掉了"，而它们本来是被召回的。
    """
    ranking: list[int] = []
    for item in (parsed or {}).get("ranking") or []:
        try:
            index = int(item) - 1          # 提示词里给的是 1 起的编号
        except (TypeError, ValueError):
            continue
        if 0 <= index < size and index not in ranking:
            ranking.append(index)
    for index in range(size):
        if index not in ranking:
            ranking.append(index)
    return ranking


async def _rerank(query: str, hits: list[Hit], *, limit: int, biz_id: int) -> list[Hit]:
    """用一次模型调用重排候选，取前 limit 条。

    为什么值得多一次调用：RRF 只看两路排名的融合，它分不出"提到了"与"回答了"——
    实测 15% 的噪声全是**同会议同主题、但答不了该问题**的窗口。

    **失败绝不打断检索**：重排是增强，出错就按原顺序返回（与"索引失败不影响主流程"
    同款判断——增强能力不该有能力把主链路弄挂）。
    """
    listing = "\n".join(
        f"[{index}] {hit.text[:400]}" for index, hit in enumerate(hits, start=1))
    user = f"【问题】\n{query}\n\n【候选片段】\n{listing}\n\n请按格式输出 JSON。"
    try:
        config = await _chat_config()
        parsed = await model_factory.invoke_json(
            config, RERANK_SYSTEM, user, RERANK_SCHEMA, RERANK_CALL_TYPE, biz_id, "重排模型")
    except Exception:  # noqa: BLE001 - 重排失败不该让检索失败
        logger.exception("重排失败，按原顺序返回候选（biz_id=%s）", biz_id)
        return hits[:limit]
    return [hits[index] for index in _ranking_of(parsed, len(hits))][:limit]
