"""PostgreSQL 知识库索引：BM25（pg_search）+ dense 向量（pgvector）。

定位：**派生索引**。MySQL 的 `kb_chunk` 是真源，这里只存检索需要的列；
丢了可以从 MySQL 重建，不应该被当成业务数据备份。

为什么 BM25 和向量放在同一个 PostgreSQL：
- LangGraph checkpoint 已经需要 PG，知识检索也放这里，少维护一个独立向量库；
- 一张表同时承载词法列和 `vector` 列，过滤条件只写一遍，RRF 两路都能用；
- ParadeDB 的 `pg_search` 负责中文 BM25，pgvector 负责 cosine/HNSW。

回退：PG 不可用时知识检索没有第二套向量库可退；问答仍可运行（无检索依据时
由 no_retrieval 闸如实标注），`/chat/status` 会报告 `pg_kb_reachable=false`。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("meetingbot.pg_kb")

PG_DSN_ENV = "PG_DSN"
TOKENIZER_ENV = "KB_BM25_TOKENIZER"
TOP_K_ENV = "KB_BM25_TOP_K"
EMBEDDING_DIM_ENV = "EMBEDDING_DIM"
DEFAULT_EMBEDDING_DIM = 1024

# 只允许白名单：tokenizer 会进 DDL（不能参数化），不能接受任意字符串
ALLOWED_TOKENIZERS = {"jieba", "chinese_lindera", "chinese_compatible", "ngram"}
DEFAULT_TOKENIZER = "jieba"
SCHEMA = "kb"
TABLE = "chunk_search"
PREFETCH_LIMIT = 24

_pool: AsyncConnectionPool | None = None
_available = False
_dimension: int = DEFAULT_EMBEDDING_DIM


class PgKbError(RuntimeError):
    """与 PG 知识库索引交互失败。调用方按“本轮检索降级”处理。"""


SEARCH_COLUMNS = (
    "chunk_id", "meeting_id", "source_type", "source_id", "authority",
    "chunk_type", "field", "item_index", "speaker", "start_ms", "end_ms",
    "meeting_start_epoch", "text", "embedding",
)

_UPSERT_SQL = f"""
    INSERT INTO {SCHEMA}.{TABLE}
        ({", ".join(SEARCH_COLUMNS)})
    VALUES ({", ".join(["%s"] * len(SEARCH_COLUMNS))})
    ON CONFLICT (chunk_id) DO UPDATE SET
        meeting_id = EXCLUDED.meeting_id,
        source_type = EXCLUDED.source_type,
        source_id = EXCLUDED.source_id,
        authority = EXCLUDED.authority,
        chunk_type = EXCLUDED.chunk_type,
        field = EXCLUDED.field,
        item_index = EXCLUDED.item_index,
        speaker = EXCLUDED.speaker,
        start_ms = EXCLUDED.start_ms,
        end_ms = EXCLUDED.end_ms,
        meeting_start_epoch = EXCLUDED.meeting_start_epoch,
        text = EXCLUDED.text,
        -- 只为 BM25 先写入 metadata 时不要清掉已经生成的向量
        embedding = COALESCE(EXCLUDED.embedding, {SCHEMA}.{TABLE}.embedding)
"""


@dataclass(frozen=True)
class PgHit:
    chunk_id: int
    score: float
    meeting_id: int
    source_type: str
    source_id: int
    authority: str
    chunk_type: str


def dsn() -> str:
    return (os.getenv(PG_DSN_ENV) or "").strip()


def embedding_dim() -> int:
    try:
        value = int(os.getenv(EMBEDDING_DIM_ENV, str(DEFAULT_EMBEDDING_DIM)))
    except ValueError:
        return DEFAULT_EMBEDDING_DIM
    return max(1, value)


def tokenizer() -> str:
    value = (os.getenv(TOKENIZER_ENV) or DEFAULT_TOKENIZER).strip().lower()
    return value if value in ALLOWED_TOKENIZERS else DEFAULT_TOKENIZER


def top_k(default: int = PREFETCH_LIMIT) -> int:
    raw = (os.getenv(TOP_K_ENV) or str(default)).strip()
    try:
        return max(1, min(100, int(raw)))
    except ValueError:
        return default


def available() -> bool:
    return _available and _pool is not None


def _require_available() -> None:
    if not available():
        raise PgKbError("PG 知识库索引不可用")


def table_name() -> str:
    return f"{SCHEMA}.{TABLE}"


def dimension() -> int:
    return _dimension


def _pool_kwargs() -> dict[str, Any]:
    return {"autocommit": True, "row_factory": dict_row}


async def init(conninfo: str | None = None, *, min_size: int = 1,
               max_size: int = 5, dim: int | None = None) -> bool:
    """初始化连接池并建 schema。幂等；PG 不可用只记录日志并返回 False。"""
    global _pool, _available, _dimension
    if _pool is not None:
        return _available
    conninfo = (conninfo or dsn()).strip()
    if not conninfo:
        logger.info("PG_DSN 未配置，知识检索关闭（BM25 + dense 都不可用）")
        return False
    _dimension = int(dim or embedding_dim())
    pool = None
    try:
        pool = AsyncConnectionPool(
            conninfo, min_size=min_size, max_size=max_size,
            kwargs=_pool_kwargs(), open=False,
        )
        await pool.open(wait=True, timeout=10)
        _pool = pool
        await ensure_schema(_dimension)
        _available = True
        logger.info("PG 知识库索引就绪：BM25 tokenizer=%s，vector dim=%s",
                    tokenizer(), _dimension)
    except Exception:  # noqa: BLE001 - 派生索引失败不能阻断主链路
        logger.exception("PG 知识库索引初始化失败，检索不可用")
        _available = False
        if pool is not None:
            await pool.close()
        _pool = None
    return _available


async def close() -> None:
    global _pool, _available
    _available = False
    if _pool is not None:
        await _pool.close()
        _pool = None


def _vector_literal(vector: Sequence[float]) -> str:
    """pgvector 的文本输入格式；参数化后由 PG 做类型转换。"""
    return "[" + ",".join(f"{float(value):.9g}" for value in vector) + "]"


async def ensure_schema(dim: int | None = None) -> None:
    """建扩展/schema/表/BM25 索引/HNSW 索引。DDL 幂等，启动可重复执行。"""
    global _dimension
    if _pool is None:
        raise RuntimeError("PG 知识库连接池未初始化")
    dim = int(dim or _dimension or embedding_dim())
    tok = tokenizer()
    bm25_index = f"""
        CREATE INDEX IF NOT EXISTS chunk_search_bm25
        ON {SCHEMA}.{TABLE}
        USING bm25 (chunk_id, text)
        WITH (
            key_field='chunk_id',
            text_fields='{{"text": {{"tokenizer": {{"type": "{tok}"}}}}}}'
        )
    """
    async with _pool.connection() as conn, conn.cursor() as cur:
        # pg_search 依赖 pgvector；CASCADE 会把 vector 一起装上；显式再建一次更清楚
        await cur.execute("CREATE EXTENSION IF NOT EXISTS pg_search CASCADE")
        await cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        await cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLE} (
                    chunk_id BIGINT PRIMARY KEY,
                    meeting_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    authority TEXT NOT NULL,
                    chunk_type TEXT NOT NULL,
                    field TEXT,
                    item_index INTEGER,
                    speaker TEXT,
                    start_ms BIGINT,
                    end_ms BIGINT,
                    meeting_start_epoch BIGINT,
                    text TEXT NOT NULL,
                    embedding vector({dim})
                )
            """)
        # 旧库（只有 BM25 列）平滑加列；已有向量列时 IF NOT EXISTS 保持原样
        await cur.execute(f"""
                ALTER TABLE {SCHEMA}.{TABLE}
                ADD COLUMN IF NOT EXISTS embedding vector({dim})
            """)
        await cur.execute(f"""
                SELECT format_type(atttypid, atttypmod) AS column_type
                FROM pg_attribute
                WHERE attrelid = '{SCHEMA}.{TABLE}'::regclass
                  AND attname = 'embedding' AND NOT attisdropped
            """)
        current = await cur.fetchone()
        expected = f"vector({dim})"
        if current and str(current["column_type"]) != expected:
            raise PgKbError(
                f"{SCHEMA}.{TABLE}.embedding 是 {current['column_type']}，"
                f"当前 EMBEDDING_DIM 需要 {expected}；换嵌入维度必须重建知识库")
        await cur.execute(f"""
                CREATE INDEX IF NOT EXISTS chunk_search_meeting
                ON {SCHEMA}.{TABLE} (meeting_id)
            """)
        await cur.execute(f"""
                CREATE INDEX IF NOT EXISTS chunk_search_source
                ON {SCHEMA}.{TABLE} (source_type, source_id)
            """)
        await cur.execute(f"""
                CREATE INDEX IF NOT EXISTS chunk_search_authority
                ON {SCHEMA}.{TABLE} (authority)
            """)
        await cur.execute(f"""
                CREATE INDEX IF NOT EXISTS chunk_search_type
                ON {SCHEMA}.{TABLE} (chunk_type)
            """)
        await cur.execute(f"""
                CREATE INDEX IF NOT EXISTS chunk_search_meeting_time
                ON {SCHEMA}.{TABLE} (meeting_start_epoch)
            """)
        await cur.execute(bm25_index)
        await cur.execute(f"""
                CREATE INDEX IF NOT EXISTS chunk_search_embedding_hnsw
                ON {SCHEMA}.{TABLE} USING hnsw (embedding vector_cosine_ops)
            """)
    _dimension = dim


async def upsert_chunks(rows: Sequence[dict[str, Any]]) -> int:
    """按 chunk_id 幂等写入 BM25 metadata；embedding 为空时保留已有向量。"""
    if not rows:
        return 0
    _require_available()
    values = [
        tuple(
            _vector_literal(row["embedding"])
            if column == "embedding" and row.get("embedding") is not None
            else row.get(column)
            for column in SEARCH_COLUMNS
        )
        for row in rows
    ]
    async with _pool.connection() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
        await cur.executemany(_UPSERT_SQL, values)
    return len(values)


async def upsert_embeddings(rows: Sequence[tuple[int, Sequence[float]]]) -> int:
    """补写/覆盖 chunk 的 dense 向量；与 BM25 metadata 共用同一张表。

    必须逐行确认 UPDATE 命中：目标行不存在时不能静默返回成功，否则调用方会把
    `KbChunk.embedded=True` 写下去，状态与实际向量不一致。
    """
    if not rows:
        return 0
    _require_available()
    values = [(int(chunk_id), _vector_literal(vector)) for chunk_id, vector in rows]
    async with _pool.connection() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
        for chunk_id, vector in values:
            await cur.execute(
                f"UPDATE {SCHEMA}.{TABLE} SET embedding = %s::vector "
                "WHERE chunk_id = %s RETURNING chunk_id",
                (vector, chunk_id),
            )
            if await cur.fetchone() is None:
                raise PgKbError(f"PG 检索行不存在，无法写入向量：chunk_id={chunk_id}")
    return len(values)


async def missing_embeddings(chunk_ids: Sequence[int]) -> list[int]:
    """返回这些 chunk 里 PG 仍缺向量的 id；PG 不可用时抛错而不是假装全有。"""
    ids = [int(i) for i in chunk_ids]
    if not ids:
        return []
    _require_available()
    async with _pool.connection() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
        await cur.execute(
            f"SELECT chunk_id FROM {SCHEMA}.{TABLE} "
            "WHERE chunk_id = ANY(%s) AND embedding IS NULL",
            (ids,),
        )
        return [int(row["chunk_id"]) for row in await cur.fetchall()]


async def delete_chunks(chunk_ids: Sequence[int]) -> int:
    if not chunk_ids:
        return 0
    _require_available()
    async with _pool.connection() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
        await cur.execute(
            f"DELETE FROM {SCHEMA}.{TABLE} WHERE chunk_id = ANY(%s)",
            ([int(i) for i in chunk_ids],),
        )
        return cur.rowcount or 0


async def delete_by_source(source_type: str, source_id: int) -> int:
    _require_available()
    async with _pool.connection() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
        await cur.execute(
            f"DELETE FROM {SCHEMA}.{TABLE} WHERE source_type = %s AND source_id = %s",
            (source_type, source_id),
        )
        return cur.rowcount or 0


def _array_filter(column: str, values: Iterable[Any] | None,
                  params: list[Any]) -> str:
    if values is None:
        return ""
    items = list(values)
    if not items:
        return " AND FALSE"
    params.append(items)
    return f" AND {column} = ANY(%s)"


def _filters(params: list[Any], *, allowed_meeting_ids: Sequence[int] | None,
             authority: Iterable[str] | None, chunk_type: Iterable[str] | None,
             source_ids: Iterable[int] | None, source_types: Iterable[str] | None,
             meeting_time_from: int | None, meeting_time_to: int | None) -> str:
    where = [
        _array_filter("meeting_id", allowed_meeting_ids, params),
        _array_filter("authority", authority, params),
        _array_filter("chunk_type", chunk_type, params),
        _array_filter("source_id", source_ids, params),
        _array_filter("source_type", source_types, params),
    ]
    if meeting_time_from is not None:
        where.append("AND meeting_start_epoch >= %s")
        params.append(int(meeting_time_from))
    if meeting_time_to is not None:
        where.append("AND meeting_start_epoch <= %s")
        params.append(int(meeting_time_to))
    return " ".join(where)


async def bm25_search(
    query: str,
    *,
    allowed_meeting_ids: Sequence[int] | None,
    limit: int = PREFETCH_LIMIT,
    authority: Iterable[str] | None = None,
    chunk_type: Iterable[str] | None = None,
    source_ids: Iterable[int] | None = None,
    source_types: Iterable[str] | None = None,
    meeting_time_from: int | None = None,
    meeting_time_to: int | None = None,
) -> list[PgHit]:
    """BM25 检索；与 dense 使用同一组过滤条件。"""
    text = (query or "").strip()
    if not text or not available():
        return []
    params: list[Any] = [text]
    # `|||` = match any term。自然语言问句用 `@@@`（AND）会把“发布前要做什么”
    # 里的每个词都当成必须命中，召回直接塌掉；词法臂要的是任一关键词命中。
    where = ["text ||| %s"]
    filters = _filters(
        params, allowed_meeting_ids=allowed_meeting_ids, authority=authority,
        chunk_type=chunk_type, source_ids=source_ids, source_types=source_types,
        meeting_time_from=meeting_time_from, meeting_time_to=meeting_time_to)
    where.append(filters)
    params.append(max(1, min(100, int(limit))))
    sql = f"""
        SELECT chunk_id, meeting_id, source_type, source_id, authority, chunk_type,
               pdb.score(chunk_id) AS score
        FROM {SCHEMA}.{TABLE}
        WHERE {' '.join(where)}
        ORDER BY score DESC
        LIMIT %s
    """
    async with _pool.connection() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    return [
        PgHit(
            chunk_id=int(row["chunk_id"]),
            score=float(row.get("score") or 0.0),
            meeting_id=int(row["meeting_id"]),
            source_type=str(row["source_type"]),
            source_id=int(row["source_id"]),
            authority=str(row["authority"]),
            chunk_type=str(row["chunk_type"]),
        )
        for row in rows
    ]


async def dense_search(
    dense: Sequence[float],
    *,
    allowed_meeting_ids: Sequence[int] | None,
    limit: int = PREFETCH_LIMIT,
    authority: Iterable[str] | None = None,
    chunk_type: Iterable[str] | None = None,
    source_ids: Iterable[int] | None = None,
    source_types: Iterable[str] | None = None,
    meeting_time_from: int | None = None,
    meeting_time_to: int | None = None,
) -> list[dict[str, Any]]:
    """pgvector cosine 检索；返回 `{chunk_id, score}`，score 为余弦相似度。"""
    if not dense or not available():
        return []
    vector = _vector_literal(dense)
    params: list[Any] = [vector]
    filters = _filters(
        params, allowed_meeting_ids=allowed_meeting_ids, authority=authority,
        chunk_type=chunk_type, source_ids=source_ids, source_types=source_types,
        meeting_time_from=meeting_time_from, meeting_time_to=meeting_time_to)
    params.extend([vector, max(1, min(100, int(limit)))])
    sql = f"""
        SELECT chunk_id, 1 - (embedding <=> %s::vector) AS score
        FROM {SCHEMA}.{TABLE}
        WHERE embedding IS NOT NULL {filters}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    async with _pool.connection() as conn, conn.cursor() as cur:  # type: ignore[union-attr]
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    return [
        {"chunk_id": int(row["chunk_id"]), "score": float(row.get("score") or 0.0)}
        for row in rows
    ]


__all__ = [
    "PgHit", "PgKbError", "available", "init", "close", "ensure_schema",
    "upsert_chunks", "upsert_embeddings", "missing_embeddings",
    "delete_chunks", "delete_by_source",
    "bm25_search", "dense_search", "top_k", "tokenizer", "dsn",
    "embedding_dim", "dimension", "table_name", "PREFETCH_LIMIT",
]
