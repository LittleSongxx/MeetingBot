"""在线问答的 LangGraph checkpoint（PostgreSQL）。

与 Temporal 的分工：
- Temporal 管**长任务**的跨进程持久执行（转写/纪要/自检/索引）；
- 这里管**一次问答**的图状态：用户断开、进程重启后，能从最近的 checkpoint
  继续跑，而不是从头再花一遍 token。

Checkpoint 里只放可序列化的图状态；`call_model`/`tools`/`Actor` 等每轮运行对象
不写入 checkpoint（见 chat_graph 的 runtime 注入）。
"""

from __future__ import annotations

import logging
import os

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services import pg_kb

logger = logging.getLogger("meetingbot.chat_checkpoint")

CHECKPOINT_ENABLED_ENV = "CHAT_CHECKPOINT_ENABLED"
CHECKPOINT_SCHEMA = "langgraph"

_pool: AsyncConnectionPool | None = None
_saver: object | None = None


def enabled() -> bool:
    raw = (os.getenv(CHECKPOINT_ENABLED_ENV) or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def available() -> bool:
    return _saver is not None


async def init(conninfo: str | None = None) -> bool:
    """初始化 checkpoint 表。未启用/无 DSN/PG 不可用时返回 False。"""
    global _pool, _saver
    if _saver is not None:
        return True
    if not enabled():
        logger.info("CHAT_CHECKPOINT_ENABLED 未开启，在线问答不启用 checkpoint")
        return False
    dsn = (conninfo or pg_kb.dsn()).strip()
    if not dsn:
        logger.warning("没有 PG_DSN，在线问答 checkpoint 不启用")
        return False
    pool = None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        pool = AsyncConnectionPool(
            dsn, min_size=1, max_size=4, open=False,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                # setup() 与运行时表都放 langgraph schema；public 保留给扩展
                "options": f"-c search_path={CHECKPOINT_SCHEMA},public",
            },
        )
        await pool.open(wait=True, timeout=10)
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(f"CREATE SCHEMA IF NOT EXISTS {CHECKPOINT_SCHEMA}")
        saver = AsyncPostgresSaver(pool)
        try:
            await saver.setup()
        except psycopg.errors.UniqueViolation as error:
            # 两个进程同时启动时都会跑 setup()；迁移表的主键冲突说明表已建好。
            # 只吞这一种冲突，其它 setup 失败仍按失败处理。
            if "checkpoint_migrations" not in str(error):
                raise
            logger.warning("checkpoint 表已初始化，跳过重复 setup")
        _pool, _saver = pool, saver
        logger.info("PostgreSQL checkpoint 就绪（schema=%s）", CHECKPOINT_SCHEMA)
        return True
    except Exception:  # noqa: BLE001 - checkpoint 是增强，失败退回无 checkpoint
        logger.exception("PostgreSQL checkpoint 初始化失败，在线问答 checkpoint 不启用")
        if pool is not None:
            await pool.close()
        _pool, _saver = None, None
        return False


async def close() -> None:
    global _pool, _saver
    _saver = None
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_checkpointer():
    """返回 AsyncPostgresSaver；未启用/初始化失败返回 None。"""
    return _saver


def in_memory_checkpointer():
    """开发/测试用：单进程 checkpoint，不跨重启。"""
    try:
        from langgraph.checkpoint.memory import InMemorySaver
    except ImportError:  # pragma: no cover - 版本差异兜底
        from langgraph.checkpoint.memory import MemorySaver as InMemorySaver
    return InMemorySaver()


__all__ = ["enabled", "available", "init", "close", "get_checkpointer",
           "in_memory_checkpointer"]
