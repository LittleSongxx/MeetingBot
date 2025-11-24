"""知识库服务的单测：sqlite 内存库 + 打桩的嵌入与向量库，零外部调用。

这里钉的是**幂等与省钱**两组不变量，它们出错不会报错、只会悄悄多花钱或给出过期结果：
- 内容没变 → 不重嵌、不重写向量
- 内容变了 → 重建，且旧切片与旧向量必须清掉（否则检索到过期版本）
- 嵌入中途失败 → 已成功的切片标记保留，重跑只补没嵌入的
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
from services import kb_service
from temporalio.exceptions import ApplicationError
from tortoise import Tortoise


class _KbBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="kb-tester", password="x", name="张晨")
        self.meeting = await Meeting.create(
            meeting_no="MTG-KB-1", title="产品发布评审会议",
            start_time="2026-09-10 10:40:00", end_time="2026-09-10 13:00:00",
            creator_id=self.user.id, host_id=self.user.id, status="FINISHED")
        self.config = await AiModelConfig.create(
            name="本地嵌入", model_type="EMBEDDING", provider="OPENAI_COMPATIBLE",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="unit-test-key", model_name="text-embedding-v4", enabled=True)

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    @staticmethod
    def _ok_vectors():
        """默认嵌入桩：确定性向量，维度随便取（与真实模型无关，只验流程）。"""
        async def _embed(config, texts, **kwargs):
            return [[0.1] * 8] * len(texts)
        return _embed

    async def _transcript(self, *, status: str = "SUCCEEDED", contents=None) -> TranscriptionTask:
        task = await TranscriptionTask.create(
            meeting_id=self.meeting.id, material_id=100, model_config_id=self.config.id,
            initiator_id=self.user.id, status=status, stage="转写完成", progress=100)
        # 默认内容够长，能切出多个窗口（否则只落一批，考不到分批与部分失败）
        contents = contents or [f"第{i}句" + "内容" * 20 + "。" for i in range(12)]
        await TranscriptSegment.bulk_create([
            TranscriptSegment(task_id=task.id, meeting_id=self.meeting.id, segment_no=i,
                              start_ms=i * 4000, end_ms=i * 4000 + 3900, speaker_label="01-0",
                              content=text, original_text=text)
            for i, text in enumerate(contents)])
        return task

    async def _only_task(self) -> KbIndexTask:
        return await KbIndexTask.filter(
            source_type=kb_service.SOURCE_TRANSCRIPT).order_by("-id").first()

    def _search_hit(self, chunk_id: int, score: float = 0.5,
                    *, text: str | None = None) -> kb_service.Hit:
        return kb_service.Hit(
            chunk_id=chunk_id, score=score, text=text or f"第{chunk_id}片",
            meeting_id=self.meeting.id, authority="RAW",
            chunk_type="TRANSCRIPT_WINDOW", field=None, item_index=None,
            speaker=None, start_ms=chunk_id * 1000, end_ms=chunk_id * 1000 + 500,
            document_id=chunk_id, origin=None, source_id=None,
            source_type=None,
        )


class IndexTranscriptTest(_KbBase):
    async def _run_index(self, task_id: int, *, side_effect=None):
        """跑一次索引，返回 (kb_task_id, embed, upsert, delete) 四个 mock。

        入队也一并打桩：`schedule_kb_index` 自己会入队，漏打桩会真的去连 Temporal 并一直等。
        """
        embed = AsyncMock(side_effect=side_effect or self._ok_vectors())
        upsert, delete = AsyncMock(), AsyncMock()
        with patch.object(kb_service.model_factory, "embed_texts", embed), \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "ensure_schema", AsyncMock()), \
             patch.object(kb_service.pg_kb, "upsert_chunks", AsyncMock()), \
             patch.object(kb_service.pg_kb, "missing_embeddings", AsyncMock(return_value=[])), \
             patch.object(kb_service.pg_kb, "upsert_embeddings", upsert), \
             patch.object(kb_service.pg_kb, "delete_chunks", delete), \
             patch.object(kb_service.pg_kb, "delete_by_source", AsyncMock()), \
             patch.object(kb_service.task_queue, "enqueue", AsyncMock()):
            kb_task_id = await kb_service.schedule_kb_index(
                kb_service.SOURCE_TRANSCRIPT, task_id, self.meeting.id, self.user.id)
            await kb_service.execute_kb_index(kb_task_id)
        return kb_task_id, embed, upsert, delete

    async def test_indexes_and_marks_every_chunk_embedded(self):
        task = await self._transcript()
        kb_task_id, embed, upsert, _ = await self._run_index(task.id)

        kb_task = await KbIndexTask.get(id=kb_task_id)
        self.assertEqual(kb_task.status, kb_service.SUCCEEDED)
        self.assertEqual(kb_task.progress, 100)
        self.assertIsNotNone(kb_task.document_id)

        chunks = await KbChunk.filter(document_id=kb_task.document_id)
        self.assertGreater(len(chunks), 0)
        self.assertTrue(all(chunk.embedded for chunk in chunks))
        self.assertEqual(kb_task.chunk_count, len(chunks))
        self.assertEqual(kb_task.embedded_count, len(chunks))
        self.assertGreaterEqual(embed.await_count, 1)

        written = upsert.await_args.args[0]
        self.assertEqual({chunk_id for chunk_id, _vector in written},
                         set(await KbChunk.filter(document_id=kb_task.document_id).values_list("id", flat=True)))
        self.assertTrue(all(len(vector) > 0 for _, vector in written))

    async def test_unchanged_content_does_not_re_embed(self):
        task = await self._transcript()
        _, embed, _, _ = await self._run_index(task.id)
        self.assertGreater(embed.await_count, 0)

        kb_task_id, embed2, upsert2, _ = await self._run_index(task.id)
        kb_task = await KbIndexTask.get(id=kb_task_id)
        self.assertEqual(kb_task.status, kb_service.SUCCEEDED)
        self.assertIn("未变化", kb_task.stage)
        self.assertEqual(embed2.await_count, 0, "内容未变化时不该重复为嵌入付费")
        self.assertEqual(upsert2.await_count, 0, "内容未变化时不该重写向量")

    async def test_unchanged_content_repairs_missing_pg_vectors(self):
        task = await self._transcript()
        kb_task_id, _, _, _ = await self._run_index(task.id)
        document_id = (await KbIndexTask.get(id=kb_task_id)).document_id
        chunk_ids = list(await KbChunk.filter(document_id=document_id)
                         .values_list("id", flat=True))
        embed = AsyncMock(side_effect=self._ok_vectors())
        upsert = AsyncMock()
        with patch.object(kb_service.model_factory, "embed_texts", embed), \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "ensure_schema", AsyncMock()), \
             patch.object(kb_service.pg_kb, "upsert_chunks", AsyncMock()), \
             patch.object(kb_service.pg_kb, "missing_embeddings",
                          AsyncMock(return_value=chunk_ids[:1])), \
             patch.object(kb_service.pg_kb, "upsert_embeddings", upsert), \
             patch.object(kb_service.task_queue, "enqueue", AsyncMock()):
            kb_task_id = await kb_service.schedule_kb_index(
                kb_service.SOURCE_TRANSCRIPT, task.id, self.meeting.id, self.user.id)
            await kb_service.execute_kb_index(kb_task_id)
        self.assertGreater(embed.await_count, 0, "缺向量的来源必须补嵌")
        upsert.assert_awaited()
        kb_task = await KbIndexTask.get(id=kb_task_id)
        self.assertEqual(kb_task.status, kb_service.SUCCEEDED)
        self.assertIn("补齐", kb_task.stage or "")

    async def test_changed_content_rebuilds_and_drops_old_chunks(self):
        task = await self._transcript()
        kb_task_id, _, _, _ = await self._run_index(task.id)
        document_id = (await KbIndexTask.get(id=kb_task_id)).document_id
        old_ids = set(await KbChunk.filter(document_id=document_id).values_list("id", flat=True))
        self.assertTrue(old_ids)

        segment = await TranscriptSegment.filter(task_id=task.id).order_by("segment_no").first()
        await TranscriptSegment.filter(id=segment.id).update(content="改过的一句话内容。")

        kb_task_id2, embed2, _, delete = await self._run_index(task.id)
        self.assertGreater(embed2.await_count, 0, "内容变了必须重新嵌入")
        self.assertEqual(set(delete.await_args.args[0]), old_ids,
                         "旧切片向量必须被删掉，否则会检索到过期版本")
        new_document_id = (await KbIndexTask.get(id=kb_task_id2)).document_id
        new_ids = set(await KbChunk.filter(document_id=new_document_id).values_list("id", flat=True))
        self.assertFalse(new_ids & old_ids)
        self.assertEqual(await KbDocument.filter(source_type="TRANSCRIPT").count(), 1,
                         "同一来源只留一份文档版本")

    async def test_partial_embedding_failure_keeps_progress_and_reports_failure(self):
        task = await self._transcript()
        calls = {"n": 0}

        async def flaky(config, texts, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return [[0.1] * 8] * len(texts)
            raise RuntimeError("嵌入服务 429")

        # 批次压到 1：这样第 1 片成功、第 2 片失败，才真的考到"部分成功"。
        # 失败会落 FAILED 并抛 non_retryable，避免 Temporal 重试撞 FAILED 空转。
        with patch.object(kb_service, "_UPSERT_BATCH_SIZE", 1), \
                self.assertRaises(ApplicationError):
            await self._run_index(task.id, side_effect=flaky)

        kb_task = await self._only_task()
        self.assertEqual(kb_task.status, kb_service.FAILED)
        document = await KbDocument.get(id=kb_task.document_id)
        self.assertEqual(document.status, "FAILED")
        self.assertIn("429", document.error_message or "")
        self.assertGreater(await KbChunk.filter(embedded=True).count(), 0,
                           "已写成功的切片必须保留标记，重跑只补剩下的")
        self.assertGreater(await KbChunk.filter(embedded=False).count(), 0)

    async def test_rejects_transcription_that_has_not_succeeded(self):
        task = await self._transcript(status="PROCESSING")
        with self.assertRaises(Exception) as caught:  # 永久性错误：抛出且标为不可重试
            await self._run_index(task.id)
        self.assertIn("转写成功", str(caught.exception))
        kb_task = await self._only_task()
        self.assertEqual(kb_task.status, kb_service.FAILED)
        self.assertIn("转写成功", kb_task.error_message or "")

    async def test_completed_task_is_not_reprocessed(self):
        task = await self._transcript()
        kb_task_id, _, upsert, _ = await self._run_index(task.id)
        before = upsert.await_count
        # 状态不是 PENDING → 条件抢占拿不到，直接返回（模拟重复投递）
        await kb_service.execute_kb_index(kb_task_id)
        self.assertEqual((await KbIndexTask.get(id=kb_task_id)).status, kb_service.SUCCEEDED)
        self.assertEqual(await KbChunk.filter(embedded=False).count(), 0)
        self.assertGreaterEqual(before, 1)


class IndexMinutesTest(_KbBase):
    async def _minutes(self, *, status: str) -> MeetingMinutes:
        task = await self._transcript()
        return await MeetingMinutes.create(
            meeting_id=self.meeting.id, source_task_id=task.id, model_config_id=self.config.id,
            status=status, created_by=self.user.id, summary="本次会议审定 V2.0 发布范围",
            decisions_json=[{"content": "发布时隐藏报表导出入口", "origin": "stated",
                             "evidence": [{"quote": "先隐藏入口", "speaker": "张涛"}]}],
            topics_json=[], viewpoints_json=[], pending_items_json=[], risks_json=[])

    async def _run(self, minutes_id: int):
        upsert = AsyncMock()
        with patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(side_effect=self._ok_vectors())), \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "ensure_schema", AsyncMock()), \
             patch.object(kb_service.pg_kb, "upsert_chunks", AsyncMock()), \
             patch.object(kb_service.pg_kb, "missing_embeddings", AsyncMock(return_value=[])), \
             patch.object(kb_service.pg_kb, "upsert_embeddings", upsert), \
             patch.object(kb_service.pg_kb, "delete_chunks", AsyncMock()), \
             patch.object(kb_service.pg_kb, "delete_by_source", AsyncMock()), \
             patch.object(kb_service.task_queue, "enqueue", AsyncMock()):
            kb_task_id = await kb_service.schedule_kb_index(
                kb_service.SOURCE_MINUTES, minutes_id, self.meeting.id, self.user.id)
            await kb_service.execute_kb_index(kb_task_id)
        return kb_task_id, upsert

    async def test_draft_minutes_are_refused(self):
        minutes = await self._minutes(status="DRAFT")
        with self.assertRaises(Exception) as caught:
            await self._run(minutes.id)
        self.assertIn("已确认", str(caught.exception))
        self.assertEqual(await KbChunk.all().count(), 0, "草稿纪要一个字都不该进知识库")

    async def test_confirmed_minutes_are_indexed_as_authoritative(self):
        minutes = await self._minutes(status="CONFIRMED")
        kb_task_id, upsert = await self._run(minutes.id)
        kb_task = await KbIndexTask.get(id=kb_task_id)
        self.assertEqual(kb_task.status, kb_service.SUCCEEDED)
        written = upsert.await_args.args[0]
        self.assertTrue(written)
        self.assertTrue(all(len(vector) > 0 for _, vector in written))
        document = await KbDocument.get(id=kb_task.document_id)
        self.assertEqual(document.authority, kb_service.chunking.AUTHORITY_CONFIRMED)


    async def test_empty_source_removes_previous_documents(self):
        minutes = await self._minutes(status="CONFIRMED")
        await self._run(minutes.id)
        self.assertGreater(await KbDocument.filter(source_id=minutes.id).count(), 0)

        await MeetingMinutes.filter(id=minutes.id).update(
            summary="", decisions_json=[], topics_json=[], viewpoints_json=[],
            pending_items_json=[], risks_json=[])
        await self._run(minutes.id)
        self.assertEqual(await KbDocument.filter(source_id=minutes.id).count(), 0)
        self.assertEqual(await KbChunk.filter().count(), 0)


class SearchKnowledgeTest(_KbBase):
    async def test_empty_query_short_circuits_without_calling_models(self):
        with patch.object(kb_service.model_factory, "embed_texts", AsyncMock()) as embed:
            self.assertEqual(await kb_service.search_knowledge(
                "   ", allowed_meeting_ids=[self.meeting.id]), [])
            self.assertEqual(embed.await_count, 0)

    async def test_hits_are_mapped_from_mysql(self):
        minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id, source_task_id=99, model_config_id=self.config.id,
            status="CONFIRMED", created_by=self.user.id, summary="摘要")
        document = await KbDocument.create(
            meeting_id=self.meeting.id, source_type=kb_service.SOURCE_MINUTES,
            source_id=minutes.id, task_id=1, authority="CONFIRMED",
            content_hash="x", chunk_count=1, embed_model="m", embed_dim=8)
        chunk = await KbChunk.create(
            document_id=document.id, meeting_id=self.meeting.id, chunk_no=1,
            chunk_type="MINUTES_ITEM", field="decisions", item_index=0,
            origin="stated", text="[decisions] 隐藏报表导出入口")
        with patch.object(kb_service.model_factory, "embed_texts", AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.pg_kb, "dense_search",
                          AsyncMock(return_value=[{"chunk_id": chunk.id, "score": 0.5}])) as search, \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "bm25_search", AsyncMock(return_value=[])):
            hits = await kb_service.search_knowledge("报表导出", allowed_meeting_ids=None, limit=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].chunk_id, chunk.id)
        citation = hits[0].as_citation()
        self.assertEqual(citation["field"], "decisions")
        self.assertEqual(citation["authority"], "CONFIRMED")
        self.assertIsNone(citation["start_second"])
        self.assertIsNone(search.await_args.kwargs["allowed_meeting_ids"])

    async def test_empty_permission_set_is_passed_through_unchanged(self):
        with patch.object(kb_service.model_factory, "embed_texts", AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.pg_kb, "dense_search", AsyncMock(return_value=[])) as search, \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "bm25_search", AsyncMock(return_value=[])):
            await kb_service.search_knowledge("报表导出", allowed_meeting_ids=[], limit=3)
        self.assertEqual(search.await_args.kwargs["allowed_meeting_ids"], [],
                         "空权限集合必须原样传递，不能变成 None（那会变成越权）")

    async def test_embedding_failure_falls_back_to_bm25(self):
        minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id, source_task_id=100, model_config_id=self.config.id,
            status="CONFIRMED", created_by=self.user.id, summary="摘要")
        document = await KbDocument.create(
            meeting_id=self.meeting.id, source_type=kb_service.SOURCE_MINUTES,
            source_id=minutes.id, task_id=1, authority="CONFIRMED",
            content_hash="y", chunk_count=1, embed_model="m", embed_dim=8)
        chunk = await KbChunk.create(
            document_id=document.id, meeting_id=self.meeting.id, chunk_no=1,
            chunk_type="MINUTES_ITEM", field="decisions", item_index=0,
            origin="stated", text="[decisions] 隐藏报表导出入口")
        with patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(side_effect=RuntimeError("arrears"))), \
             patch.object(kb_service.pg_kb, "dense_search", AsyncMock()) as dense, \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "bm25_search",
                          AsyncMock(return_value=[SimpleNamespace(chunk_id=chunk.id)])), \
             patch.object(kb_service, "rerank_enabled", return_value=False):
            hits = await kb_service.search_knowledge("报表导出", allowed_meeting_ids=None, limit=3)
        self.assertEqual(dense.await_count, 0, "嵌入失败时不应再调 dense")
        self.assertEqual([hit.chunk_id for hit in hits], [chunk.id])

    async def test_relevance_filter_keeps_needed_and_maybe(self):
        hits = [SimpleNamespace(text="a"), SimpleNamespace(text="b"), SimpleNamespace(text="c")]
        config = SimpleNamespace(model_name="m")
        with patch("services.model_config.resolve_route", AsyncMock(return_value=config)), \
             patch.object(kb_service.model_factory, "invoke_json",
                          AsyncMock(return_value={"labels": [
                              {"index": 1, "label": "no"},
                              {"index": 2, "label": "needed"},
                              {"index": 3, "label": "maybe"}]})):
            kept = await kb_service._filter_relevant("q", hits, biz_id=1)
        self.assertEqual([hit.text for hit in kept], ["b", "c"])

    async def test_relevance_filter_keeps_first_when_all_no(self):
        hits = [SimpleNamespace(text="a"), SimpleNamespace(text="b")]
        config = SimpleNamespace(model_name="m")
        with patch("services.model_config.resolve_route", AsyncMock(return_value=config)), \
             patch.object(kb_service.model_factory, "invoke_json",
                          AsyncMock(return_value={"labels": [
                              {"index": 1, "label": "no"},
                              {"index": 2, "label": "no"}]})):
            kept = await kb_service._filter_relevant("q", hits, biz_id=1)
        self.assertEqual([hit.text for hit in kept], ["a"])

    async def test_missing_embedding_config_is_a_readable_error(self):
        await AiModelConfig.filter(model_type="EMBEDDING").update(enabled=False)
        with self.assertRaises(kb_service.KbError) as caught:
            await kb_service.search_knowledge("任意问题", allowed_meeting_ids=None)
        self.assertIn("嵌入模型配置", str(caught.exception))

    async def test_filters_are_passed_down_to_both_retrievers(self):
        with patch.object(kb_service.model_factory, "embed_texts", AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.pg_kb, "dense_search", AsyncMock(return_value=[])) as dense, \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "bm25_search", AsyncMock(return_value=[])) as bm25:
            await kb_service.search_knowledge(
                "灰度", allowed_meeting_ids=[self.meeting.id], chunk_type=["TRANSCRIPT_WINDOW"],
                source_ids=[6], source_types=["TRANSCRIPT"],
                meeting_time_from=1_700_000_000, meeting_time_to=1_700_900_000)
        for kwargs in (dense.await_args.kwargs, bm25.await_args.kwargs):
            self.assertEqual(kwargs["chunk_type"], ["TRANSCRIPT_WINDOW"])
            self.assertEqual(kwargs["source_ids"], [6])
            self.assertEqual(kwargs["source_types"], ["TRANSCRIPT"])
            self.assertEqual(kwargs["meeting_time_from"], 1_700_000_000)
            self.assertEqual(kwargs["meeting_time_to"], 1_700_900_000)

    def test_embedding_scope_is_part_of_the_fingerprint(self):
        """换嵌入模型/维度/collection 都要触发重建——否则新库里没有这些向量，
        而任务会被判"内容未变化，跳过重建"（检索静默少一整个来源）。"""
        texts = ["第一片", "第二片"]
        same = kb_service.content_fingerprint(texts, embed_scope="m1:1024@kb_v1")
        self.assertEqual(same, kb_service.content_fingerprint(texts, embed_scope="m1:1024@kb_v1"))
        for other in ("m2:1024@kb_v1", "m1:1024@kb_v2", "m1:2048@kb_v1"):
            self.assertNotEqual(same, kb_service.content_fingerprint(texts, embed_scope=other),
                                other)

    def test_payload_schema_version_is_part_of_the_fingerprint(self):
        """载荷结构版本进指纹：否则"给 payload 加字段"不会触发重建，
        旧 point 缺字段 → 按它过滤时静默漏数据（这是本仓踩过的坑）。"""
        texts = ["第一片", "第二片"]
        same = kb_service.content_fingerprint(texts)
        self.assertEqual(same, kb_service.content_fingerprint(list(texts)))
        self.assertNotEqual(same, kb_service.content_fingerprint(texts, payload_schema=1))
        self.assertNotEqual(same, kb_service.content_fingerprint(texts, payload_schema=3))


if __name__ == "__main__":
    unittest.main()


class RerankTest(_KbBase):
    """rerank 是**专用模型**（qwen3.7-text-rerank）：开关控制、失败不打断检索。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = patch.object(kb_service, "_hits_by_ids", side_effect=self._map_hits)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KB_RERANK_ENABLED", None)
            self.assertTrue(kb_service.rerank_enabled(), "专用重排是标准组件，默认开")
        for value in ("0", "false", "no", "off"):
            with patch.dict(os.environ, {"KB_RERANK_ENABLED": value}):
                self.assertFalse(kb_service.rerank_enabled(), value)

    def test_candidate_depth_is_configurable(self):
        with patch.dict(os.environ, {"RERANK_CANDIDATES": "40"}):
            self.assertEqual(kb_service.rerank_candidates(), 40)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RERANK_CANDIDATES", None)
            self.assertEqual(kb_service.rerank_candidates(),
                             kb_service.DEFAULT_RERANK_CANDIDATES)

    def _hits(self, count: int) -> list[dict]:
        return [{"chunk_id": index, "score": 1.0 - index / 100}
                for index in range(1, count + 1)]

    async def _map_hits(self, scores: dict[int, float]) -> list[kb_service.Hit]:
        return [self._search_hit(chunk_id, score) for chunk_id, score in scores.items()]

    async def test_disabled_keeps_the_original_path(self):
        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "0"}), \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "bm25_search", AsyncMock(return_value=[])), \
             patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.pg_kb, "dense_search",
                          AsyncMock(return_value=self._hits(5))) as search, \
             patch.object(kb_service.model_factory, "rerank_documents", AsyncMock()) as rerank_call:
            await kb_service.search_knowledge("灰度", allowed_meeting_ids=None, limit=3)
        self.assertEqual(search.await_args.kwargs["limit"], 3, "关掉时召回条数不变")
        self.assertEqual(rerank_call.await_count, 0, "关掉时不该发生重排调用")

    async def test_enabled_reranks_candidates_and_returns_top_limit(self):
        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "1"}), \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "bm25_search", AsyncMock(return_value=[])), \
             patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.pg_kb, "dense_search",
                          AsyncMock(return_value=self._hits(5))) as search, \
             patch.object(kb_service.model_factory, "rerank_documents",
                          AsyncMock(return_value=[(4, 0.9), (2, 0.5), (0, 0.1)])) as rerank_call:
            hits = await kb_service.search_knowledge("灰度", allowed_meeting_ids=None, limit=3)
        # 召回先放大到候选数，再由重排决定顺序
        self.assertEqual(search.await_args.kwargs["limit"], kb_service.rerank_candidates())
        self.assertEqual(rerank_call.await_args.kwargs["call_type"], kb_service.RERANK_CALL_TYPE)
        self.assertEqual([hit.chunk_id for hit in hits], [5, 3, 1])

    async def test_rerank_failure_falls_back_to_the_original_order(self):
        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "1"}), \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "bm25_search", AsyncMock(return_value=[])), \
             patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.pg_kb, "dense_search",
                          AsyncMock(return_value=self._hits(5))), \
             patch.object(kb_service.model_factory, "rerank_documents",
                          AsyncMock(side_effect=RuntimeError("rerank 服务不可达"))):
            hits = await kb_service.search_knowledge("灰度", allowed_meeting_ids=None, limit=2)
        self.assertEqual([hit.chunk_id for hit in hits], [1, 2], "重排失败必须退回原顺序")

    async def test_top_k_caps_what_goes_to_the_model(self):
        """漏斗的截断口：模型要 10 条，也只给最相关的 RERANK_TOP_K 条。"""
        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "1", "RERANK_TOP_K": "3"}), \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "bm25_search", AsyncMock(return_value=[])), \
             patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.pg_kb, "dense_search",
                          AsyncMock(return_value=self._hits(8))), \
             patch.object(kb_service.model_factory, "rerank_documents",
                          AsyncMock(return_value=[(i, 1 - i / 10) for i in range(8)])):
            hits = await kb_service.search_knowledge("灰度", allowed_meeting_ids=None, limit=10)
        self.assertEqual([hit.chunk_id for hit in hits], [1, 2, 3])

    async def test_candidates_missing_from_the_ranking_keep_their_order(self):
        """模型只回前 top_n 条时，剩下的按原顺序接在后面（它们本来就被召回了）。"""
        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "1"}), \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "bm25_search", AsyncMock(return_value=[])), \
             patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.pg_kb, "dense_search",
                          AsyncMock(return_value=self._hits(5))), \
             patch.object(kb_service.model_factory, "rerank_documents",
                          AsyncMock(return_value=[(4, 0.9)])):
            hits = await kb_service.search_knowledge("灰度", allowed_meeting_ids=None, limit=3)
        self.assertEqual([hit.chunk_id for hit in hits], [5, 1, 2])


class OverlapDedupeTest(unittest.TestCase):
    """同一段话只交一次：相邻窗口会同时被召回并同时被重排选中。"""

    @staticmethod
    def _hit(chunk_id: int, start: int, end: int, *, source_id: int = 6,
             score: float = 1.0) -> kb_service.Hit:
        return kb_service.Hit(
            chunk_id=chunk_id, score=score, text=f"窗口{chunk_id}", meeting_id=2,
            authority="RAW", chunk_type="TRANSCRIPT_WINDOW", field=None, item_index=None,
            speaker="张晨", start_ms=start, end_ms=end, document_id=1, origin=None,
            source_id=source_id)

    def test_overlapping_windows_keep_the_higher_ranked_one(self):
        hits = [self._hit(1, 642_000, 763_000), self._hit(2, 720_000, 828_000)]
        with patch.dict(os.environ, {"KB_DEDUPE_ENABLED": "1"}):
            kept = kb_service._dedupe_overlapping(hits)      # noqa: SLF001 - 纯函数，直接测规则
        self.assertEqual([hit.chunk_id for hit in kept], [1])

    def test_a_short_touch_is_not_a_duplicate(self):
        hits = [self._hit(1, 0, 120_000), self._hit(2, 100_000, 220_000)]  # 重叠 20s
        with patch.dict(os.environ, {"KB_DEDUPE_ENABLED": "1"}):
            kept = kb_service._dedupe_overlapping(hits)      # noqa: SLF001
        self.assertEqual([hit.chunk_id for hit in kept], [1, 2])

    def test_windows_from_different_recordings_are_not_deduped(self):
        """两段录音的时间轴都从 0 开始：只比时间窗会把上午/下午的同一秒误合并。"""
        hits = [self._hit(1, 642_000, 763_000, source_id=6),
                self._hit(2, 720_000, 828_000, source_id=7)]
        with patch.dict(os.environ, {"KB_DEDUPE_ENABLED": "1"}):
            kept = kb_service._dedupe_overlapping(hits)      # noqa: SLF001
        self.assertEqual([hit.chunk_id for hit in kept], [1, 2])

    def test_disabled_by_default_after_the_zero_call_counterfactual(self):
        """默认关是量出来的：300×3 下删掉的 10 条全部被判有用，precision 反而降。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KB_DEDUPE_ENABLED", None)
            self.assertFalse(kb_service.dedupe_enabled())

    def test_the_switch_can_turn_it_on(self):
        hits = [self._hit(1, 642_000, 763_000), self._hit(2, 720_000, 828_000)]
        with patch.dict(os.environ, {"KB_DEDUPE_ENABLED": "1"}):
            kept = kb_service._dedupe_overlapping(hits)      # noqa: SLF001
        self.assertEqual([hit.chunk_id for hit in kept], [1])


class OverlapDedupeSearchTest(_KbBase):
    async def test_search_dedupes_after_rerank_and_before_truncation(self):
        raw = [
            {"chunk_id": 1, "score": 0.9, "payload": {
                "meeting_id": 1, "authority": "RAW", "chunk_type": "TRANSCRIPT_WINDOW",
                "text": "第一段", "start_ms": 642_000, "end_ms": 763_000, "source_id": 6}},
            {"chunk_id": 2, "score": 0.8, "payload": {
                "meeting_id": 1, "authority": "RAW", "chunk_type": "TRANSCRIPT_WINDOW",
                "text": "第二段", "start_ms": 720_000, "end_ms": 828_000, "source_id": 6}},
            {"chunk_id": 3, "score": 0.7, "payload": {
                "meeting_id": 1, "authority": "RAW", "chunk_type": "TRANSCRIPT_WINDOW",
                "text": "第三段", "start_ms": 0, "end_ms": 60_000, "source_id": 6}},
        ]
        mapped = [kb_service.Hit(
            chunk_id=item["chunk_id"], score=item["score"], text=item["payload"]["text"],
            meeting_id=1, authority="RAW", chunk_type="TRANSCRIPT_WINDOW",
            field=None, item_index=None, speaker=None,
            start_ms=item["payload"]["start_ms"], end_ms=item["payload"]["end_ms"],
            document_id=item["chunk_id"], origin=None, source_id=6,
            source_type=None,
        ) for item in raw]
        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "1", "RERANK_TOP_K": "3",
                                     "KB_DEDUPE_ENABLED": "1"}), \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "bm25_search", AsyncMock(return_value=[])), \
             patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.pg_kb, "dense_search", AsyncMock(return_value=raw)), \
             patch.object(kb_service, "_hits_by_ids", AsyncMock(return_value=mapped)), \
             patch.object(kb_service.model_factory, "rerank_documents",
                          AsyncMock(return_value=[(0, 0.9), (1, 0.8), (2, 0.7)])):
            hits = await kb_service.search_knowledge("灰度", allowed_meeting_ids=None, limit=3)
        self.assertEqual([hit.chunk_id for hit in hits], [1, 3],
                         "重叠的第二段应被丢掉，而不是把 top-3 变成两条再补一条无关的")


class PgSchemaTest(IndexTranscriptTest):
    """PG 索引表结构：过滤/BM25/HNSW 索引必须在"跳过重建"的路径上也存在。

    实测踩过：建索引原先只发生在 `_embed_and_store` 里，而内容未变化的库会在它之前
    就返回——于是最常见的情形（内容没变）永远建不出索引，过滤一直是慢查询且不报错。
    """

    async def test_collection_and_indexes_are_ensured_even_when_content_is_unchanged(self):
        task = await self._transcript()
        await self._run_index(task.id)                     # 第一次：真建
        with patch.object(kb_service.model_factory, "embed_texts", AsyncMock()) as embed, \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "ensure_schema", AsyncMock()) as ensure, \
             patch.object(kb_service.pg_kb, "upsert_chunks", AsyncMock()), \
             patch.object(kb_service.pg_kb, "missing_embeddings", AsyncMock(return_value=[])), \
             patch.object(kb_service.task_queue, "enqueue", AsyncMock()):
            kb_task_id = await kb_service.schedule_kb_index(
                kb_service.SOURCE_TRANSCRIPT, task.id, self.meeting.id, self.user.id)
            await kb_service.execute_kb_index(kb_task_id)
        self.assertEqual(embed.await_count, 0, "内容未变不该重嵌")
        ensure.assert_awaited(), "但索引就绪这一步必须照跑"
        kb_task = await KbIndexTask.get(id=kb_task_id)
        self.assertIn("内容未变化", kb_task.stage or "")


class ContextualIndexTest(_KbBase):
    """Contextual Retrieval：只改嵌入文本，不改引用正文；失败退回规则上下文。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.agent = await AiModelConfig.create(
            name="问答模型", model_type="AGENT", provider="OPENAI_COMPATIBLE",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="unit-test-key", model_name="qwen-test", enabled=True)

    async def _run(self, task_id: int, *, invoke):
        embed = AsyncMock(side_effect=self._ok_vectors())
        upsert = AsyncMock()
        with patch.dict(os.environ, {"KB_CONTEXTUAL_ENABLED": "1"}), \
             patch.object(kb_service.model_factory, "embed_texts", embed), \
             patch.object(kb_service.model_factory, "invoke_json", invoke), \
             patch.object(kb_service.pg_kb, "available", return_value=True), \
             patch.object(kb_service.pg_kb, "ensure_schema", AsyncMock()), \
             patch.object(kb_service.pg_kb, "upsert_chunks", AsyncMock()), \
             patch.object(kb_service.pg_kb, "missing_embeddings", AsyncMock(return_value=[])), \
             patch.object(kb_service.pg_kb, "upsert_embeddings", upsert), \
             patch.object(kb_service.pg_kb, "delete_chunks", AsyncMock()), \
             patch.object(kb_service.pg_kb, "delete_by_source", AsyncMock()), \
             patch.object(kb_service.task_queue, "enqueue", AsyncMock()):
            kb_task_id = await kb_service.schedule_kb_index(
                kb_service.SOURCE_TRANSCRIPT, task_id, self.meeting.id, self.user.id)
            await kb_service.execute_kb_index(kb_task_id)
        return kb_task_id, embed, upsert

    async def test_embedding_text_gets_header_and_one_sentence_context(self):
        task_id = await self._transcript_id()
        kb_task_id, embed, _ = await self._run(
            task_id, invoke=AsyncMock(return_value="讨论灰度发布与回滚方案"))
        chunks = await KbChunk.filter(document_id=(await KbIndexTask.get(id=kb_task_id)).document_id)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertIsNotNone(chunk.embed_text)
            self.assertTrue(chunk.embed_text.startswith("[会议：产品发布评审会议"))
            self.assertIn("[上下文] 讨论灰度发布与回滚方案", chunk.embed_text)
            self.assertTrue(chunk.embed_text.endswith(chunk.text))
        embedded = embed.await_args.args[1]
        self.assertTrue(all("[上下文]" in text for text in embedded))

    async def test_context_generation_failure_falls_back_to_rule_header(self):
        task_id = await self._transcript_id()
        kb_task_id, _, _ = await self._run(
            task_id, invoke=AsyncMock(side_effect=RuntimeError("模型不可达")))
        kb_task = await KbIndexTask.get(id=kb_task_id)
        self.assertEqual(kb_task.status, kb_service.SUCCEEDED, "增强失败不能弄挂索引")
        chunk = await KbChunk.filter(document_id=kb_task.document_id).first()
        self.assertTrue(chunk.embed_text.startswith("[会议：产品发布评审会议"))
        self.assertNotIn("[上下文]", chunk.embed_text)
        self.assertIn(chunk.text, chunk.embed_text)

    async def _transcript_id(self) -> int:
        task = await self._transcript()
        return task.id
