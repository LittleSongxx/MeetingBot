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
内容变了才重建文档版本。瞬时失败不会自动重试到无限次：任务落 FAILED 后由
`/kb/index` 手动重建，避免 Temporal 重试撞上 FAILED 抢占条件空转。
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable, Sequence

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

from services import hybrid, model_factory, pg_kb, task_queue
from services import kb_chunking as chunking

logger = logging.getLogger("meetingbot.kb")

KIND_KB_INDEX = "kb-index"

SOURCE_TRANSCRIPT = "TRANSCRIPT"
SOURCE_MINUTES = "MINUTES"

# 状态值（与 kb_index_task.status 取值一致）
PENDING, PROCESSING, SUCCEEDED, FAILED = "PENDING", "PROCESSING", "SUCCEEDED", "FAILED"

# 单次写入 PG 向量的条数：分批是为了单条 SQL 参数不要过大，也让失败重试的粒度更细
_UPSERT_BATCH_SIZE = 64

# PG 检索行的**结构版本**。加/改/删 metadata 字段时必须 +1——它进内容指纹
# （见 content_fingerprint），从而让旧切片在下一次索引时自动重建。
# 历史：1 = 最早的一版（无 meeting_start_epoch）；2 = 加会议开始时间（时间过滤用）。
PAYLOAD_SCHEMA_VERSION = 2

# Contextual Retrieval 的上下文生成版本。改提示词/改拼接形态时 +1——它进内容指纹，
# 否则旧库会被判"内容没变"而继续用旧上下文（与 payload 版本同一条理由）。
CONTEXTUAL_VERSION = 1
CONTEXTUAL_CALL_TYPE = "KB_CONTEXTUALIZE"
CONTEXTUAL_SYSTEM_PROMPT = (
    "你是会议知识库的索引助手。给定一场会议的一小段内容，用一句不超过 40 字的中文"
    "说明这段内容在会议中的位置与主题（例如：讨论灰度发布时间与覆盖比例；"
    "张晨说明回滚方案）。只依据给定内容，不补充原文没有的事实、人名、数字或结论。"
)
CONTEXTUAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"context": {"type": "string"}},
    "required": ["context"],
}


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
    # 来源身份（转写任务 id / 纪要 id）：一场会可能有多段录音，引用要能落到**哪一段**，
    # 过滤要能按它收窄（"下午这场会"）；评测侧的检索指标也要靠它把切片映射回录音
    source_id: int | None = None
    # 来源类型（TRANSCRIPT / MINUTES）：读侧按 MySQL 状态兜底过滤时用
    source_type: str | None = None

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
            "source_id": self.source_id,
            "quote": self.text,
        }


# --------------------------------------------------------------------------- #
# 来源装载与指纹
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MeetingMeta:
    """索引时给 Contextual Retrieval 用的会议元数据（一次查询，整批复用）。"""

    title: str
    start_epoch: int | None
    date_text: str | None


async def _embedding_config() -> AiModelConfig:
    config = await AiModelConfig.filter(model_type="EMBEDDING", enabled=True).order_by("-id").first()
    if config is None:
        raise KbError("没有启用中的嵌入模型配置，请先在「AI模型配置」里启用一个 EMBEDDING 用途的配置")
    return config


async def _agent_config() -> AiModelConfig | None:
    """Contextual Retrieval 用问答/便宜模型生成上下文句；没配就退回纯规则上下文。"""
    from services import model_config

    try:
        return await model_config.resolve_route("KB_CONTEXTUALIZE", "AGENT")
    except model_config.ModelConfigError:
        return None


def contextual_enabled() -> bool:
    """Contextual Retrieval 开关。默认**关**：它要重建全部切片并逐块花一次模型调用，
    收益必须先在本语料量出来（LEDGER 任务 3），量到正收益再改默认。"""
    return os.getenv("KB_CONTEXTUAL_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}


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
    chunks = chunking.chunk_transcript(lines, target_chars=chunking.target_chars())
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
                        payload_schema: int = PAYLOAD_SCHEMA_VERSION,
                        embed_scope: str = "") -> str:
    """内容指纹：切片集合或**写入形态**变了就换指纹，据此判断要不要重建文档版本。

    `embed_scope` 覆盖"写到哪里、用什么模型写"——**这两个也必须进指纹**。
    两次踩坑教出来的：
    1. 指纹只覆盖正文时，"给 payload 加字段"不会触发重建 → 旧 point 缺字段 →
       按它过滤会**静默漏掉**数据（不报错、不告警）；
    2. 指纹不含**嵌入模型与索引表/维度**时，换模型/换表之后旧文档被判"内容未变化"
       → 新索引里根本没有它的向量，检索少一整个来源（同样不报错）。
    把结构和作用域并进来之后，任一变化 = 换指纹 = 下次索引自动重建。
    """
    digest = hashlib.sha256()
    digest.update(f"payload-schema:{int(payload_schema)}\x00".encode())
    digest.update(f"embed-scope:{embed_scope}\x00".encode())
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


async def cancel_kb_index(task_id: int) -> None:
    await task_queue.cancel(KIND_KB_INDEX, task_id)


async def recover_kb_index_tasks() -> int:
    """启动兜底：回收崩溃留下的 PROCESSING，并补投 PENDING。

    超过一次 activity 超时仍未完成的 PROCESSING 视为进程已经不在；
    单副本部署下这样做是安全的。多副本时不要依赖这里回收仍在执行的任务。
    """
    stale_before = now() - timedelta(minutes=10)
    await KbIndexTask.filter(
        status=PROCESSING, started_at__lt=stale_before,
    ).update(status=PENDING, stage="上次执行中断，等待重试",
             error_message="检测到未完成的任务，重新入队", finished_at=None)
    recovered = 0
    for task in await KbIndexTask.filter(status=PENDING):
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

        # **索引就绪是基础设施步骤，不挂在"要重嵌"那条路径上**：实测踩过——
        # 它原先只在 `_embed_and_store` 里做，于是"内容未变化，跳过重建"的库
        # （最常见的情形）永远建不出 PG 过滤/向量索引，查询一直是慢查询且不报错。
        # 官方口径也要求"建索引要在写入之前"（过滤感知的 HNSW 边只在建索引后生成）。
        await pg_kb.ensure_schema(model_factory.embedding_dim())

        chunks, text_fingerprint = await _load_source_chunks(task)
        meta = await _meeting_meta(task.meeting_id)
        # Contextual Retrieval：上下文句也进嵌入文本，因此它必须进指纹。
        # 会议标题/日期影响规则上下文，所以把它们的短哈希也并进作用域——
        # 否则改标题后旧库会被判"内容没变"，继续用旧上下文（与 payload 版本同一条理由）。
        contextual = contextual_enabled()
        contextual_config = await _agent_config() if contextual else None
        if contextual and contextual_config is None:
            logger.warning("Contextual Retrieval 已开启但没有 AGENT 模型配置，退回纯规则上下文")
        context_scope = ""
        if contextual:
            meta_digest = hashlib.sha256(
                f"{meta.title}\x00{meta.date_text or ''}".encode("utf-8")).hexdigest()[:12]
            context_scope = (f"|ctx:v{CONTEXTUAL_VERSION}:"
                             f"{contextual_config.model_name if contextual_config else 'rule'}"
                             f":{meta_digest}")
        # 写入形态进指纹：换嵌入模型/维度/collection 都要重建（否则新库里没有这些向量）
        fingerprint = content_fingerprint(
            [chunk.text for chunk in chunks],
            embed_scope=f"{config.model_name}:{model_factory.embedding_dim()}"
                        f"@{pg_kb.table_name()}{context_scope}")
        if not chunks:
            # 来源被清空：不能只把任务标成功，否则旧文档/旧切片仍会作为
            # “当前确认内容”被检索到。
            await _drop_source_documents(task)
            await _finish(task.id, chunk_count=0, embedded_count=0, stage="来源没有可索引的内容")
            return

        existing = await KbDocument.filter(
            source_type=task.source_type, source_id=task.source_id, status=SUCCEEDED,
        ).order_by("-id").first()

        if existing is not None and existing.content_hash == fingerprint:
            # 内容没变：不重嵌、不重写向量。这就是"重试不为已成功的部分重复付费"。
            # BM25 metadata 仍同步一次；如果 PG 表被清空/恢复，下面的 missing 检查
            # 会发现向量缺失并补嵌（不能只因正文指纹相同就假定向量还在）。
            existing_chunks = list(await KbChunk.filter(
                document_id=existing.id).order_by("chunk_no"))
            await _sync_pg_kb(task, existing, existing_chunks, meta)
            await KbIndexTask.filter(id=task.id).update(document_id=existing.id)
            missing = await pg_kb.missing_embeddings([int(chunk.id) for chunk in existing_chunks])
            if missing:
                missing_set = {int(chunk_id) for chunk_id in missing}
                await KbChunk.filter(id__in=missing_set).update(embedded=False)
                for chunk in existing_chunks:
                    if int(chunk.id) in missing_set:
                        chunk.embedded = False
                await _embed_and_store(task, config, existing, existing_chunks, meta,
                                       contextual_config if contextual else None)
                await _finish(task.id, chunk_count=existing.chunk_count,
                              embedded_count=existing.chunk_count,
                              stage="内容未变化，补齐缺失向量", document_id=existing.id)
            else:
                await _finish(task.id, chunk_count=existing.chunk_count,
                              embedded_count=existing.chunk_count, stage="内容未变化，跳过重建")
            await _cleanup_old_documents(task, keep_id=existing.id)
            return

        await KbIndexTask.filter(id=task.id).update(stage="正在切片", progress=15)

        if existing is None:
            document = await KbDocument.create(
                meeting_id=task.meeting_id, source_type=task.source_type, source_id=task.source_id,
                task_id=task.id, authority=await _authority_of(task),
                content_hash=fingerprint, chunk_count=len(chunks),
                embed_model=config.model_name, embed_dim=model_factory.embedding_dim(),
                # 嵌入完成前不能对检索侧可见；否则半成品会被 _hits_by_ids 当成有效文档
                status="BUILDING",
            )
        else:
            # 内容变了：重建同一行（旧切片与其向量先清掉，避免检索到过期版本）
            document = existing
            await _drop_chunks(document.id)
            await KbDocument.filter(id=document.id).update(
                task_id=task.id, content_hash=fingerprint, chunk_count=len(chunks),
                embed_model=config.model_name, embed_dim=model_factory.embedding_dim(),
                status="BUILDING", error_message=None, create_time=now(),
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
        # BM25 metadata 与 dense 向量共用 PG 表：都在这一处同步，避免两条派生链路各写一遍。
        await _sync_pg_kb(task, document, created, meta)

        await KbIndexTask.filter(id=task.id).update(
            chunk_count=len(created), stage=f"正在生成向量（共 {len(created)} 片）", progress=30)

        embedded = await _embed_and_store(task, config, document, created, meta,
                                          contextual_config if contextual else None)
        await _finish(task.id, chunk_count=len(created), embedded_count=embedded,
                      stage="索引完成", document_id=document.id)
        await _cleanup_old_documents(task, keep_id=document.id)
    except KbError as error:
        # 永久性错误（来源不存在 / 状态不对 / 没配嵌入模型）：重试多少次结果都一样，
        # 标成 non_retryable，别让 Temporal 白跑 5 轮又白等退避
        detail = (str(error) or type(error).__name__)[:2000]
        await _mark_failed(kb_task_id, detail)
        raise ApplicationError(detail, non_retryable=True) from error
    except Exception as error:  # noqa: BLE001
        # 失败已经落在任务/文档行上；再抛普通异常只会让 Temporal 重试时撞上
        # status=FAILED 的抢占条件空转。这里明确 non_retryable，让页面走手动重建。
        detail = (str(error) or type(error).__name__)[:2000]
        logger.exception("知识库索引失败（task=%s）", kb_task_id)
        await _mark_failed(kb_task_id, detail)
        raise ApplicationError(detail, non_retryable=True) from error


async def _mark_failed(kb_task_id: int, detail: str) -> None:
    """失败收尾：任务与它产出的文档一起落 FAILED（文档失败原因要留在库里，便于排障）。"""
    await KbIndexTask.filter(id=kb_task_id).update(
        status=FAILED, stage="索引失败", error_message=detail, finished_at=now())
    row = await KbIndexTask.get_or_none(id=kb_task_id)
    if row is not None and row.document_id:
        # 只有 BUILDING 版本才是本次失败产生的半成品；已有 SUCCEEDED 版本在
        # “内容未变化但 PG 瞬时不可用”时不能被降级，否则 PG 恢复后还要重建。
        document = await KbDocument.get_or_none(id=row.document_id)
        if document is not None and document.status == "BUILDING":
            await KbDocument.filter(id=document.id).update(
                status="FAILED", error_message=detail)


async def _load_source_chunks(task: KbIndexTask) -> tuple[list[chunking.Chunk], str]:
    """装载来源切片；第二个返回值是**正文指纹**（调用方再并入写入形态）。"""
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


async def _sync_pg_kb(task: KbIndexTask, document: KbDocument,
                        chunks: Sequence[KbChunk], meta: MeetingMeta) -> int:
    """把切片 metadata 同步到 PG 检索表；向量由 `_embed_and_store` 随后补齐。"""
    if not pg_kb.available():
        raise KbError("PG 检索索引不可用，索引任务失败")
    rows = [{
        "chunk_id": int(chunk.id),
        "meeting_id": int(chunk.meeting_id),
        "source_type": task.source_type,
        "source_id": int(task.source_id),
        "authority": document.authority,
        "chunk_type": chunk.chunk_type,
        "field": chunk.field,
        "item_index": chunk.item_index,
        "speaker": chunk.speaker,
        "start_ms": chunk.start_ms,
        "end_ms": chunk.end_ms,
        "meeting_start_epoch": meta.start_epoch,
        "text": chunk.text,
    } for chunk in chunks]
    return await pg_kb.upsert_chunks(rows)


async def _drop_chunks(document_id: int) -> None:
    """清掉一个文档版本的切片与其 PG 检索行。"""
    ids = await KbChunk.filter(document_id=document_id).values_list("id", flat=True)
    if ids:
        await pg_kb.delete_chunks(list(ids))
    await KbChunk.filter(document_id=document_id).delete()


async def _embed_and_store(task: KbIndexTask, config: AiModelConfig, document: KbDocument,
                           chunks: Sequence[KbChunk], meta: MeetingMeta,
                           contextual_config: AiModelConfig | None = None) -> int:
    """嵌入未完成的切片并写入 PG/pgvector，返回已嵌入的切片数。

    只处理 `embedded=False`：中途失败重跑时，已写成功的部分不重复付费。
    `embed_text` 已落库的切片直接复用——上下文句是花钱生成的，重试不该再付一次。
    """
    await pg_kb.ensure_schema(model_factory.embedding_dim())
    pending = [chunk for chunk in chunks if not chunk.embedded]
    if not pending:
        return len(chunks)

    contextual = contextual_enabled()
    done = len(chunks) - len(pending)
    for start in range(0, len(pending), _UPSERT_BATCH_SIZE):
        batch = pending[start:start + _UPSERT_BATCH_SIZE]
        inputs: list[str] = []
        for chunk in batch:
            embed_text = chunk.embed_text
            if not embed_text:
                if contextual:
                    header = chunking.context_header(
                        meeting_title=meta.title, meeting_date=meta.date_text,
                        source_label=_source_label(task), chunk=chunk)
                    sentence = ""
                    if contextual_config is not None:
                        sentence = await _contextual_sentence(
                            contextual_config, meta, task, chunk, header)
                    embed_text = chunking.embedding_text(header, sentence, chunk.text)
                else:
                    embed_text = chunk.text
                await KbChunk.filter(id=chunk.id).update(embed_text=embed_text)
                chunk.embed_text = embed_text
            inputs.append(embed_text)
        vectors = await model_factory.embed_texts(
            config, inputs, biz_id=task.id, call_type="KB_EMBEDDING")
        if not pg_kb.available():
            raise KbError("PG 检索索引不可用，向量写入失败")
        embeddings = [
            (chunk.id, vector)
            for chunk, vector, _embed_text in zip(batch, vectors, inputs, strict=True)
        ]
        await pg_kb.upsert_embeddings(embeddings)
        await KbChunk.filter(id__in=[c.id for c in batch]).update(embedded=True)
        done += len(batch)
        progress = 30 + int(60 * done / max(1, len(chunks)))
        await KbIndexTask.filter(id=task.id).update(
            progress=progress, embedded_count=done,
            stage=f"正在生成向量（{done}/{len(chunks)}）")
    return done


async def _meeting_meta(meeting_id: int) -> MeetingMeta:
    """会议标题与开始时间：payload 的时间过滤与 Contextual Retrieval 的规则上下文共用。"""
    row = await Meeting.filter(id=meeting_id).first()
    if row is None:
        return MeetingMeta(title="", start_epoch=None, date_text=None)
    title, start_time = row.title, row.start_time
    if start_time is None:
        return MeetingMeta(title=str(title or ""), start_epoch=None, date_text=None)
    local = to_local(start_time)
    return MeetingMeta(title=str(title or ""), start_epoch=int(local.timestamp()),
                       date_text=local.strftime("%Y-%m-%d"))


def _source_label(task: KbIndexTask) -> str:
    """规则上下文里的来源标签：转写用任务 id（两段录音必须能区分），纪要写明权威级。"""
    if task.source_type == SOURCE_TRANSCRIPT:
        return f"录音 {task.source_id}"
    return "人工确认纪要"


def _context_value(payload: dict[str, Any]) -> str:
    """上下文句的形状校验：空句会让"上下文"这一行变成噪声，必须让 re-ask 重来。"""
    if not isinstance(payload, dict):
        raise ValueError("上下文必须是 JSON 对象")
    text = str(payload.get("context") or "").strip().replace("\n", " ")
    if not text:
        raise ValueError("context 不能为空")
    return text[:200]


async def _contextual_sentence(config: AiModelConfig, meta: MeetingMeta,
                               task: KbIndexTask, chunk: KbChunk, header: str) -> str:
    """让问答模型用一句话说明"这段在会里的位置/主题"（Contextual Retrieval）。

    失败**不打断索引**：上下文是增强，退回规则上下文（会议/日期/时间/说话人）
    仍然可用；失败本身由 `invoke_json` 写进 `ai_call_log`（call_type 已登记）。
    """
    user_prompt = (
        f"{header}\n"
        f"会议标题：{meta.title or '未命名会议'}\n"
        f"内容：\n{chunk.text[:1200]}\n\n"
        "请输出 JSON：{\"context\": \"一句话主题\"}。"
    )
    try:
        return await model_factory.invoke_json(
            config, CONTEXTUAL_SYSTEM_PROMPT, user_prompt, CONTEXTUAL_SCHEMA,
            call_type=CONTEXTUAL_CALL_TYPE, biz_id=task.id, label=config.model_name,
            validator=_context_value)
    except Exception:  # noqa: BLE001 - 增强失败退回规则上下文，索引继续
        logger.exception("生成上下文句失败，退回规则上下文（task=%s chunk=%s）",
                         task.id, chunk.id)
        return ""


async def _finish(kb_task_id: int, *, chunk_count: int, embedded_count: int, stage: str,
                  document_id: int | None = None) -> None:
    values: dict[str, Any] = {
        "status": SUCCEEDED, "stage": stage, "progress": 100,
        "chunk_count": chunk_count, "embedded_count": embedded_count,
        "finished_at": now(), "error_message": None,
    }
    if document_id is not None:
        values["document_id"] = document_id
        await KbDocument.filter(id=document_id).update(
            status=SUCCEEDED, chunk_count=chunk_count, error_message=None)
    await KbIndexTask.filter(id=kb_task_id).update(**values)


async def _drop_source_documents(task: KbIndexTask) -> None:
    """清掉一个来源的全部文档版本（来源内容变空时使用）。"""
    documents = await KbDocument.filter(
        source_type=task.source_type, source_id=task.source_id)
    for document in documents:
        await _drop_chunks(document.id)
    # 再按来源清一次：即使 MySQL 切片行已经丢了，也不能把 PG 残留留给下一次检索。
    await pg_kb.delete_by_source(task.source_type, task.source_id)
    await KbDocument.filter(
        source_type=task.source_type, source_id=task.source_id).delete()


async def _cleanup_old_documents(task: KbIndexTask, *, keep_id: int) -> None:
    """成功后清掉同一来源的失败/半成品版本。

    失败版本在读侧本来就会被状态过滤，但它们的切片和派生索引会一直占空间；
    当前版本成功之后，旧版本没有保留价值。
    """
    rows = await KbDocument.filter(
        source_type=task.source_type, source_id=task.source_id,
        status__in=("FAILED", "BUILDING"),
    ).exclude(id=keep_id)
    for document in rows:
        try:
            await _drop_chunks(document.id)
            await KbDocument.filter(id=document.id).delete()
        except Exception:  # noqa: BLE001 - 清理失败不能把已成功的索引改成失败
            logger.exception("清理旧版本文档失败（document=%s）", document.id)


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #

async def _hits_by_ids(scores: dict[int, float]) -> list[Hit]:
    """按 chunk_id 从 MySQL 取回完整 hit。

    BM25 与 dense 都来自 PG 索引，两边只给出 id/score；最终交给生成模型的
    hit 统一从 MySQL 真源组装，避免索引 payload 与业务数据各说各话。
    """
    if not scores:
        return []
    chunks = await KbChunk.filter(id__in=list(scores.keys()))
    document_ids = {int(chunk.document_id) for chunk in chunks}
    documents = await KbDocument.filter(id__in=list(document_ids))
    by_document = {int(doc.id): doc for doc in documents}
    by_chunk = {int(chunk.id): chunk for chunk in chunks}
    hits: list[Hit] = []
    for chunk_id in scores:
        chunk = by_chunk.get(int(chunk_id))
        if chunk is None:
            continue
        document = by_document.get(int(chunk.document_id))
        if document is None or document.status != SUCCEEDED:
            continue
        hits.append(Hit(
            chunk_id=int(chunk.id), score=float(scores[chunk_id]),
            text=chunk.text, meeting_id=int(chunk.meeting_id),
            authority=str(document.authority), chunk_type=chunk.chunk_type,
            field=chunk.field, item_index=chunk.item_index, speaker=chunk.speaker,
            start_ms=chunk.start_ms, end_ms=chunk.end_ms,
            document_id=int(document.id), origin=chunk.origin,
            source_id=int(document.source_id), source_type=document.source_type,
        ))
    return hits


async def _filter_valid_sources(hits: list[Hit]) -> list[Hit]:
    """读侧兜底：派生索引里的旧切片，按 MySQL 当前状态再核一遍。

    outbox/reconcile 是异步的；这里保证即使它落后，已取消确认的纪要、已删除的
    转写也不会被交给模型。source_type 缺失的旧 payload 不在此处误杀。
    """
    document_ids = {int(h.document_id) for h in hits if h.document_id is not None}
    if document_ids:
        existing_documents = {
            int(i) for i in await KbDocument.filter(
                id__in=list(document_ids)).values_list("id", flat=True)
        }
        valid_documents = {
            int(i) for i in await KbDocument.filter(
                id__in=list(document_ids), status=SUCCEEDED).values_list("id", flat=True)
        }
        # 文档行不存在时无法判状态：保留，交给来源状态兜底（兼容历史 payload）。
        hits = [
            hit for hit in hits
            if hit.document_id is None
            or int(hit.document_id) not in existing_documents
            or int(hit.document_id) in valid_documents
        ]
    minutes_ids = {int(h.source_id) for h in hits
                   if h.source_type == SOURCE_MINUTES and h.source_id is not None}
    transcript_ids = {int(h.source_id) for h in hits
                      if h.source_type == SOURCE_TRANSCRIPT and h.source_id is not None}
    valid_minutes: set[int] = set()
    if minutes_ids:
        valid_minutes = {
            int(i) for i in await MeetingMinutes.filter(
                id__in=list(minutes_ids), status="CONFIRMED").values_list("id", flat=True)
        }
    valid_transcripts: set[int] = set()
    if transcript_ids:
        valid_transcripts = {
            int(i) for i in await TranscriptionTask.filter(
                id__in=list(transcript_ids), status="SUCCEEDED").values_list("id", flat=True)
        }
    return [
        hit for hit in hits
        if (hit.source_type == SOURCE_MINUTES and hit.source_id in valid_minutes)
        or (hit.source_type == SOURCE_TRANSCRIPT and hit.source_id in valid_transcripts)
        or hit.source_type not in (SOURCE_MINUTES, SOURCE_TRANSCRIPT)
    ]


async def search_knowledge(
    query: str,
    *,
    allowed_meeting_ids: Sequence[int] | None,
    limit: int = 8,
    authority: Iterable[str] | None = None,
    chunk_type: Iterable[str] | None = None,
    source_ids: Iterable[int] | None = None,
    source_types: Iterable[str] | None = None,
    meeting_time_from: int | None = None,
    meeting_time_to: int | None = None,
    biz_id: int = 0,
) -> list[Hit]:
    """dense + BM25 检索（可选 rerank）。`allowed_meeting_ids` 由权限层给出，
    本函数不做权限判断。

    None 表示管理员（不过滤），空集合表示无可见会议（返回空）——两者语义不同，
    由上层的 access 层负责算清楚，这里只如实执行。

    检索只有一条路径：PostgreSQL 里的 pgvector dense + pg_search BM25，
    应用层 RRF 融合；PG 不可用时本轮没有检索依据，由 no_retrieval 闸如实标注。
    最终 hit 统一从 MySQL 真源组装，避免两份派生 payload 各说各话。

    `biz_id` 只影响 `ai_call_log` 的归属（哪条业务记录引发了这次调用）：
    问答传入那条助手消息的 id，接口预览传 0。0 表示"没有业务归属"，
    观测页据此把它算作平台级调用。
    """
    text = (query or "").strip()
    if not text:
        return []
    config = await _embedding_config()
    # 嵌入供应商故障（余额/配额/网络）不应让整个检索 500：BM25 不依赖查询向量，
    # 仍能给出词面命中。dense=None 表示本轮降级为 BM25-only，由下方分支如实标注。
    dense: list[float] | None
    try:
        vectors = await model_factory.embed_texts(config, [text], biz_id=biz_id,
                                                  call_type="KB_QUERY_EMBED")
        dense = vectors[0]
    except Exception:  # noqa: BLE001 - 单臂（查询嵌入）失败，降级 BM25
        logger.exception("查询嵌入失败，本轮降级为 BM25-only（query=%s）", text[:120])
        dense = None
    candidates = rerank_candidates() if rerank_enabled() else 0
    fetch = max(int(limit), candidates) if candidates else int(limit)
    authority_list = list(authority) if authority is not None else None
    chunk_type_list = list(chunk_type) if chunk_type is not None else None
    source_id_list = list(source_ids) if source_ids is not None else None
    source_type_list = list(source_types) if source_types is not None else None

    dense_raw: list[dict[str, Any]] = []
    bm25_raw: list[pg_kb.PgHit] = []
    if pg_kb.available():
        if dense is not None:
            try:
                dense_raw = await pg_kb.dense_search(
                    dense, limit=fetch,
                    allowed_meeting_ids=allowed_meeting_ids, authority=authority_list,
                    chunk_type=chunk_type_list, source_ids=source_id_list,
                    source_types=source_type_list,
                    meeting_time_from=meeting_time_from, meeting_time_to=meeting_time_to,
                )
            except Exception:  # noqa: BLE001 - 单臂失败不阻断另一臂
                logger.exception("PG dense 检索失败，本轮只用 BM25（query=%s）", text[:120])
        try:
            bm25_raw = await pg_kb.bm25_search(
                text, allowed_meeting_ids=allowed_meeting_ids,
                limit=max(fetch, pg_kb.top_k(fetch)),
                authority=authority_list, chunk_type=chunk_type_list,
                source_ids=source_id_list, source_types=source_type_list,
                meeting_time_from=meeting_time_from, meeting_time_to=meeting_time_to,
            )
        except Exception:  # noqa: BLE001 - 单臂失败不阻断另一臂
            logger.exception("PG BM25 检索失败，本轮只用 dense（query=%s）", text[:120])
    else:
        logger.warning("PG 知识库不可用，本轮没有检索结果（query=%s）", text[:120])
    scores: dict[int, float] = {}
    if dense_raw or bm25_raw:
        fused = hybrid.fuse_dense_and_bm25(
            [int(item["chunk_id"]) for item in dense_raw],
            [int(item.chunk_id) for item in bm25_raw],
            limit=fetch,
        )
        scores = {int(item.chunk_id): item.score for item in fused}
    hits = await _hits_by_ids(scores)
    # 读侧兜底：派生索引可能落后于 MySQL 状态，先按真源再核一遍
    hits = await _filter_valid_sources(hits)
    if rerank_enabled() and len(hits) > 1:
        hits = await _rerank(text, hits, biz_id=biz_id, config=config)
    # 去重放在重排之后、截断之前：相邻窗口会同时被召回并同时被重排选中，
    # 它们占的是同一段话，交两段给生成模型只会让 context_precision 的分母虚高。
    # 保留排序更靠前（重排分数更高）的一条，再按漏斗口径截断。
    hits = _dedupe_overlapping(hits)
    return hits[:min(int(limit), rerank_top_k())]


# --------------------------------------------------------------------------- #
# rerank（专用模型；开关见 KB_RERANK_ENABLED）
# --------------------------------------------------------------------------- #

# 重排的调用类型（新调用类型必须登记 CALL_TYPE_OWNERSHIP，见 observability_service）
RERANK_CALL_TYPE = "KB_RERANK"
# 先召回多少候选再重排。默认与 PG 索引的 PREFETCH_LIMIT 同量级；可用
# `RERANK_CANDIDATES` 覆盖（任务 6 的 24 vs 40 对照就是只改这一个值）。
DEFAULT_RERANK_CANDIDATES = pg_kb.PREFETCH_LIMIT


def rerank_candidates() -> int:
    raw = os.getenv("RERANK_CANDIDATES", str(DEFAULT_RERANK_CANDIDATES)).strip()
    try:
        return max(1, int(raw))
    except ValueError as error:
        raise ValueError("RERANK_CANDIDATES 必须为正整数") from error


def rerank_top_k() -> int:
    """重排后交给生成模型的最大条数（**漏斗的截断口**）。

    行业口径是"召回宽 → 重排 → **截到 top 3–5**"：一次问一两个事实时，8 条里
    必然有一半是同会议同主题但答不了问题的窗口——那正是 context_precision 被拖低的
    唯一原因（截断前实测 33–37%，条数越多越低）。可用 `RERANK_TOP_K` 覆盖。
    """
    raw = os.getenv("RERANK_TOP_K", "5").strip()
    try:
        return max(1, int(raw))
    except ValueError as error:
        raise ValueError("RERANK_TOP_K 必须为正整数") from error


def rerank_enabled() -> bool:
    """rerank 开关。默认**开**：专用重排是 RAG 的标准组件（recall 之后必然要收 precision），
    延迟与成本的代价可测（`ai_call_log` 里有每次的 elapsed 与用量）。
    要关就显式配 KB_RERANK_ENABLED=0。"""
    return os.getenv("KB_RERANK_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


async def _rerank(query: str, hits: list[Hit], *, biz_id: int,
                  config: AiModelConfig) -> list[Hit]:
    """用专用 rerank 模型把候选按"能不能回答问题"重排，返回**完整排序**。

    为什么值得多一次请求：混合检索的 RRF 只看两路排名的融合，分不出"提到了"与"回答了"
    ——实测 15% 的噪声全是**同会议同主题、但答不了该问题**的窗口。

    截断不在这里做：去重必须先看到完整排序（相邻窗口都可能在候选里），
    否则"先截 5 条再去重"会把 top-5 变成 top-4。截断统一在调用方一处完成。

    失败**绝不打断检索**：重排是增强，出错就按原顺序返回（与"索引失败不影响主流程"
    同款判断——增强能力不该有能力把主链路弄挂）。
    """
    texts = [hit.text for hit in hits]
    try:
        ranked = await model_factory.rerank_documents(
            config, query, texts, biz_id=biz_id, top_n=len(texts),
            call_type=RERANK_CALL_TYPE)
    except Exception:  # noqa: BLE001 - 重排失败不该让检索失败
        logger.exception("重排失败，按原顺序返回候选（biz_id=%s）", biz_id)
        return hits
    ordered = [hits[index] for index, _score in ranked if 0 <= index < len(hits)]
    # 模型只回前 top_n 条时，剩下的按原顺序接在后面（它们本来就被召回了）
    seen = {index for index, _score in ranked}
    ordered.extend(hit for index, hit in enumerate(hits) if index not in seen)
    return ordered


# --------------------------------------------------------------------------- #
# 重叠窗口去重（同一段话只交一次）
# --------------------------------------------------------------------------- #

# 两个默认值不是拍出来的：切块是 500 字窗口 + 2 行重叠，实测相邻窗口的公共段
# 约 36–43 秒；30 秒能把它们判成同一段话，又不会把"只是首尾相接"的窗口误合并。
DEFAULT_DEDUPE_OVERLAP_SECONDS = 30
DEFAULT_DEDUPE_OVERLAP_RATIO = 0.25


def dedupe_enabled() -> bool:
    """重叠去重开关。默认**关**——这是量出来的，不是保守。

    在 300×3 的标定产物上做零调用反事实：默认阈值（30 秒、25%）删掉 10 条上下文，
    **10 条全部被判"有用"**；precision 从 81.0% 降到 78.4%，recall@k 不变。
    原因：相邻窗口共享的是一段话，但各自还带着只有自己才有的内容——它们不是
    "同一段话交两遍"，而是"各有一段有用的话"。所以机制保留给确属重复的语料
    （例如重叠比例接近 1 的切法），本语料不默认开。
    """
    return os.getenv("KB_DEDUPE_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}


def dedupe_overlap_seconds() -> int:
    raw = os.getenv("KB_DEDUPE_OVERLAP_SECONDS", str(DEFAULT_DEDUPE_OVERLAP_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("KB_DEDUPE_OVERLAP_SECONDS 必须为非负整数") from error
    return max(0, value)


def dedupe_overlap_ratio() -> float:
    raw = os.getenv("KB_DEDUPE_OVERLAP_RATIO", str(DEFAULT_DEDUPE_OVERLAP_RATIO)).strip()
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError("KB_DEDUPE_OVERLAP_RATIO 必须为 0–1 之间的小数") from error
    if not 0.0 <= value <= 1.0:
        raise ValueError("KB_DEDUPE_OVERLAP_RATIO 必须为 0–1 之间的小数")
    return value


def _window_ms(hit: Hit) -> tuple[int, int] | None:
    if hit.start_ms is None or hit.end_ms is None:
        return None
    start, end = int(hit.start_ms), int(hit.end_ms)
    return (start, end) if end > start else None


def _same_recording(left: Hit, right: Hit) -> bool:
    """两段窗口是否来自同一段录音。

    `source_id` 是转写任务 id：一场会有两段录音时，两条时间轴都从 0 开始，
    只比时间窗会把"上午 51 秒"与"下午 51 秒"误判成同一段话。缺来源身份时
    **不去重**（判不了就不猜，宁可留一条重复，也不要把不同录音合并掉）。
    """
    if left.source_id is None or right.source_id is None:
        return False
    return left.meeting_id == right.meeting_id and int(left.source_id) == int(right.source_id)


def _dedupe_overlapping(hits: list[Hit]) -> list[Hit]:
    """去掉与已保留条目重叠到"同一段话"程度的窗口，保留排序更靠前的。

    只处理带时间轴的转写窗口；纪要条目没有时间轴，不参与。规则是
    **绝对重叠 ≥ 阈值 且 重叠占较短窗口的比例 ≥ 阈值**：前者避免把短窗口的
    轻微重叠当重复，后者避免把两个长窗口在边界上的相接当重复。
    """
    if not dedupe_enabled() or len(hits) < 2:
        return hits
    min_overlap_ms = dedupe_overlap_seconds() * 1000
    min_ratio = dedupe_overlap_ratio()
    kept: list[Hit] = []
    for hit in hits:
        window = _window_ms(hit)
        if window is None:
            kept.append(hit)
            continue
        duplicate = False
        for prior in kept:
            prior_window = _window_ms(prior)
            if prior_window is None or not _same_recording(hit, prior):
                continue
            overlap = min(window[1], prior_window[1]) - max(window[0], prior_window[0])
            if overlap < min_overlap_ms:
                continue
            shorter = min(window[1] - window[0], prior_window[1] - prior_window[0])
            if shorter <= 0 or overlap / shorter < min_ratio:
                continue
            duplicate = True
            break
        if not duplicate:
            kept.append(hit)
    return kept


# --------------------------------------------------------------------------- #
