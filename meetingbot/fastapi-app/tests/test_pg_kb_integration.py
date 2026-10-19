"""PostgreSQL 知识库实库集成测试：BM25 + pgvector。没有 PG_TEST_DSN 时自动跳过。"""

from __future__ import annotations

import os
import unittest

from services import pg_kb

PG_TEST_DSN = (os.getenv("PG_TEST_DSN") or "").strip()


@unittest.skipUnless(PG_TEST_DSN, "需要 PG_TEST_DSN（ParadeDB PostgreSQL）")
class PgKbIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ok = await pg_kb.init(PG_TEST_DSN)
        if not ok:
            # 已显式给了 DSN 却初始化失败，说明 pg_search/权限/DDL 有问题；
            # 这里不能 skip，否则 CI 会在“根本没测”的情况下变绿。
            self.fail("PG 知识库初始化失败（检查 pg_search/vector/权限/DSN）")

    async def asyncTearDown(self):
        await pg_kb.close()

    async def test_upsert_search_and_delete(self):
        rows = [
            {"chunk_id": 900001, "meeting_id": 9001, "source_type": "TRANSCRIPT",
             "source_id": 9101, "authority": "RAW", "chunk_type": "TRANSCRIPT_WINDOW",
             "field": None, "item_index": None, "speaker": "张晨",
             "start_ms": 0, "end_ms": 1000, "meeting_start_epoch": 1788000000,
             "text": "发布前必须完成回归测试和灰度验证"},
            {"chunk_id": 900002, "meeting_id": 9001, "source_type": "TRANSCRIPT",
             "source_id": 9101, "authority": "RAW", "chunk_type": "TRANSCRIPT_WINDOW",
             "field": None, "item_index": None, "speaker": "李月",
             "start_ms": 1000, "end_ms": 2000, "meeting_start_epoch": 1788000000,
             "text": "报表导出功能延期到下个版本"},
        ]
        await pg_kb.upsert_chunks(rows)
        hits = await pg_kb.bm25_search("发布前要做什么", allowed_meeting_ids=[9001], limit=5)
        self.assertIn(900001, [hit.chunk_id for hit in hits])
        self.assertEqual(await pg_kb.bm25_search("发布前要做什么", allowed_meeting_ids=[9999], limit=5), [])

        deleted = await pg_kb.delete_by_source("TRANSCRIPT", 9101)
        self.assertGreaterEqual(deleted, 2)
        self.assertEqual(await pg_kb.bm25_search("发布前要做什么", allowed_meeting_ids=[9001], limit=5), [])

    async def test_source_type_disambiguates_same_source_id(self):
        """纪要 id 与转写任务 id 各自编号；只按 source_id 过滤会把同号纪要带进来。"""
        rows = [
            {"chunk_id": 900011, "meeting_id": 9002, "source_type": "TRANSCRIPT",
             "source_id": 9201, "authority": "RAW", "chunk_type": "TRANSCRIPT_WINDOW",
             "field": None, "item_index": None, "speaker": "张晨",
             "start_ms": 0, "end_ms": 1000, "meeting_start_epoch": 1788000000,
             "text": "下午这段录音讨论了灰度回滚"},
            {"chunk_id": 900012, "meeting_id": 9002, "source_type": "MINUTES",
             "source_id": 9201, "authority": "CONFIRMED", "chunk_type": "MINUTES_ITEM",
             "field": "decisions", "item_index": 0, "speaker": None,
             "start_ms": None, "end_ms": None, "meeting_start_epoch": 1788000000,
             "text": "下午这段录音讨论了灰度回滚"},
        ]
        await pg_kb.upsert_chunks(rows)
        hits = await pg_kb.bm25_search(
            "灰度回滚", allowed_meeting_ids=[9002], source_ids=[9201],
            source_types=["TRANSCRIPT"], limit=5)
        self.assertEqual([hit.chunk_id for hit in hits], [900011])
        await pg_kb.delete_by_source("TRANSCRIPT", 9201)
        await pg_kb.delete_by_source("MINUTES", 9201)

    async def test_dense_search_uses_pgvector_and_filters(self):
        vec_a = [0.0] * pg_kb.embedding_dim()
        vec_b = [0.0] * pg_kb.embedding_dim()
        vec_a[0] = 1.0
        vec_b[1] = 1.0
        rows = [
            {"chunk_id": 900021, "meeting_id": 9003, "source_type": "TRANSCRIPT",
             "source_id": 9301, "authority": "RAW", "chunk_type": "TRANSCRIPT_WINDOW",
             "field": None, "item_index": None, "speaker": "张晨",
             "start_ms": 0, "end_ms": 1000, "meeting_start_epoch": 1788000000,
             "text": "向量 A", "embedding": vec_a},
            {"chunk_id": 900022, "meeting_id": 9003, "source_type": "TRANSCRIPT",
             "source_id": 9301, "authority": "RAW", "chunk_type": "TRANSCRIPT_WINDOW",
             "field": None, "item_index": None, "speaker": "李月",
             "start_ms": 1000, "end_ms": 2000, "meeting_start_epoch": 1788000000,
             "text": "向量 B", "embedding": vec_b},
        ]
        await pg_kb.upsert_chunks(rows)
        hits = await pg_kb.dense_search(
            vec_a, allowed_meeting_ids=[9003], source_ids=[9301],
            source_types=["TRANSCRIPT"], limit=2)
        self.assertEqual(hits[0]["chunk_id"], 900021)
        self.assertGreater(hits[0]["score"], hits[1]["score"])
        self.assertEqual(await pg_kb.dense_search(
            vec_a, allowed_meeting_ids=[9999], limit=2), [])
        await pg_kb.delete_by_source("TRANSCRIPT", 9301)

    async def test_missing_embeddings_and_targetless_update_are_reported(self):
        vec = [0.0] * pg_kb.embedding_dim()
        vec[0] = 1.0
        rows = [
            {"chunk_id": 900031, "meeting_id": 9004, "source_type": "TRANSCRIPT",
             "source_id": 9401, "authority": "RAW", "chunk_type": "TRANSCRIPT_WINDOW",
             "field": None, "item_index": None, "speaker": "张晨",
             "start_ms": 0, "end_ms": 1000, "meeting_start_epoch": 1788000000,
             "text": "有向量", "embedding": vec},
            {"chunk_id": 900032, "meeting_id": 9004, "source_type": "TRANSCRIPT",
             "source_id": 9401, "authority": "RAW", "chunk_type": "TRANSCRIPT_WINDOW",
             "field": None, "item_index": None, "speaker": "李月",
             "start_ms": 1000, "end_ms": 2000, "meeting_start_epoch": 1788000000,
             "text": "缺向量"},
        ]
        await pg_kb.upsert_chunks(rows)
        self.assertEqual(await pg_kb.missing_embeddings([900031, 900032]), [900032])
        with self.assertRaises(pg_kb.PgKbError):
            await pg_kb.upsert_embeddings([(999999, vec)])
        await pg_kb.upsert_embeddings([(900032, vec)])
        self.assertEqual(await pg_kb.missing_embeddings([900031, 900032]), [])
        await pg_kb.delete_by_source("TRANSCRIPT", 9401)


if __name__ == "__main__":
    unittest.main()
