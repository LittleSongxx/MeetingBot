"""知识库服务的单测：sqlite 内存库 + 打桩的嵌入与向量库，零外部调用。

这里钉的是**幂等与省钱**两组不变量，它们出错不会报错、只会悄悄多花钱或给出过期结果：
- 内容没变 → 不重嵌、不重写向量
- 内容变了 → 重建，且旧切片与旧向量必须清掉（否则检索到过期版本）
- 嵌入中途失败 → 已成功的切片标记保留，重跑只补没嵌入的
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from common.times import LOCAL_TZ
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


class IndexTranscriptTest(_KbBase):
    async def _run_index(self, task_id: int, *, side_effect=None):
        """跑一次索引，返回 (kb_task_id, embed, upsert, delete) 四个 mock。

        入队也一并打桩：`schedule_kb_index` 自己会入队，漏打桩会真的去连 Temporal 并一直等。
        """
        embed = AsyncMock(side_effect=side_effect or self._ok_vectors())
        upsert, delete = AsyncMock(), AsyncMock()
        with patch.object(kb_service.model_factory, "embed_texts", embed), \
             patch.object(kb_service.qdrant, "ensure_collection", AsyncMock()), \
             patch.object(kb_service.qdrant, "upsert_chunks", upsert), \
             patch.object(kb_service.qdrant, "delete_points", delete), \
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

        # 向量库拿到的载荷必须带 meeting_id 与 authority——过滤就靠这两个字段
        written = upsert.await_args.args[0]
        self.assertEqual({p["payload"]["meeting_id"] for p in written}, {self.meeting.id})
        self.assertEqual({p["payload"]["authority"] for p in written}, {"RAW"})
        self.assertTrue(all(p["sparse"]["indices"] for p in written))

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
        # 断言具体类型：嵌入失败原样抛出（可重试类），不会被包成 ApplicationError
        with patch.object(kb_service, "_UPSERT_BATCH_SIZE", 1), self.assertRaises(RuntimeError):
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


    async def test_payload_carries_the_meeting_start_epoch(self):
        """会议开始时间进 payload：时间过滤（"最近七天"）靠它，靠录音内偏移做不到。"""
        task = await self._transcript()
        _, _, upsert, _ = await self._run_index(task.id)
        payload = upsert.await_args.args[0][0]["payload"]
        # 2026-09-10 10:40:00 本地（UTC+8）→ epoch 秒
        self.assertEqual(payload["meeting_start_epoch"],
                         int(datetime(2026, 9, 10, 10, 40, tzinfo=LOCAL_TZ).timestamp()))

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
             patch.object(kb_service.qdrant, "ensure_collection", AsyncMock()), \
             patch.object(kb_service.qdrant, "upsert_chunks", upsert), \
             patch.object(kb_service.qdrant, "delete_points", AsyncMock()), \
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
        self.assertEqual({p["payload"]["authority"] for p in written}, {"CONFIRMED"})
        document = await KbDocument.get(id=kb_task.document_id)
        self.assertEqual(document.authority, kb_service.chunking.AUTHORITY_CONFIRMED)


class SearchKnowledgeTest(_KbBase):
    async def test_empty_query_short_circuits_without_calling_models(self):
        with patch.object(kb_service.model_factory, "embed_texts", AsyncMock()) as embed:
            self.assertEqual(await kb_service.search_knowledge(
                "   ", allowed_meeting_ids=[self.meeting.id]), [])
            self.assertEqual(embed.await_count, 0)

    async def test_hits_are_mapped_from_payload(self):
        payload = {"meeting_id": self.meeting.id, "authority": "CONFIRMED",
                   "chunk_type": "MINUTES_ITEM", "field": "decisions", "item_index": 0,
                   "origin": "stated", "speaker": None, "start_ms": None, "end_ms": None,
                   "document_id": 7, "text": "[decisions] 隐藏报表导出入口"}
        with patch.object(kb_service.model_factory, "embed_texts", AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.qdrant, "hybrid_search",
                          AsyncMock(return_value=[{"chunk_id": 42, "score": 0.5, "payload": payload}])) as search:
            hits = await kb_service.search_knowledge("报表导出", allowed_meeting_ids=None, limit=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].chunk_id, 42)
        citation = hits[0].as_citation()
        self.assertEqual(citation["field"], "decisions")
        self.assertEqual(citation["authority"], "CONFIRMED")
        self.assertIsNone(citation["start_second"])
        # None 表示管理员不过滤，不能在这里被当成"空集合"
        self.assertIsNone(search.await_args.kwargs["allowed_meeting_ids"])

    async def test_empty_permission_set_is_passed_through_unchanged(self):
        with patch.object(kb_service.model_factory, "embed_texts", AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.qdrant, "hybrid_search", AsyncMock(return_value=[])) as search:
            await kb_service.search_knowledge("报表导出", allowed_meeting_ids=[], limit=3)
        self.assertEqual(search.await_args.kwargs["allowed_meeting_ids"], [],
                         "空权限集合必须原样传递，不能变成 None（那会变成越权）")

    async def test_missing_embedding_config_is_a_readable_error(self):
        await AiModelConfig.filter(model_type="EMBEDDING").update(enabled=False)
        with self.assertRaises(kb_service.KbError) as caught:
            await kb_service.search_knowledge("任意问题", allowed_meeting_ids=None)
        self.assertIn("嵌入模型配置", str(caught.exception))

    async def test_filters_are_passed_down_to_the_vector_store(self):
        with patch.object(kb_service.model_factory, "embed_texts", AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.qdrant, "hybrid_search", AsyncMock(return_value=[])) as search:
            await kb_service.search_knowledge(
                "灰度", allowed_meeting_ids=[self.meeting.id], chunk_type=["TRANSCRIPT_WINDOW"],
                meeting_time_from=1_700_000_000, meeting_time_to=1_700_900_000)
        kwargs = search.await_args.kwargs
        self.assertEqual(kwargs["chunk_type"], ["TRANSCRIPT_WINDOW"])
        self.assertEqual(kwargs["meeting_time_from"], 1_700_000_000)
        self.assertEqual(kwargs["meeting_time_to"], 1_700_900_000)

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
    """rerank 是**可选增强**：开关控制、失败不打断检索、排序结果要能覆盖全部候选。"""

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KB_RERANK_ENABLED", None)
            self.assertFalse(kb_service.rerank_enabled())
        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "1"}):
            self.assertTrue(kb_service.rerank_enabled())

    def test_ranking_is_normalized_and_completed(self):
        # 模型回了乱序、越界、漏项：去重、剔除越界、剩下的按原顺序补在后面
        self.assertEqual(kb_service._ranking_of({"ranking": [3, 1, 1, 99]}, 3), [2, 0, 1])
        self.assertEqual(kb_service._ranking_of({"ranking": []}, 2), [0, 1])
        self.assertEqual(kb_service._ranking_of(None, 1), [0])

    async def test_disabled_keeps_the_original_path(self):
        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "0"}), \
             patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.qdrant, "hybrid_search",
                          AsyncMock(return_value=[])) as search, \
             patch.object(kb_service.model_factory, "invoke_json", AsyncMock()) as rerank_call:
            await kb_service.search_knowledge("灰度", allowed_meeting_ids=None, limit=3)
        self.assertEqual(search.await_args.kwargs["limit"], 3, "关掉时召回条数不变")
        self.assertEqual(rerank_call.await_count, 0, "关掉时不该发生重排调用")

    async def test_only_the_head_window_is_sent_to_the_model(self):
        """LLM 重排的可靠区间是小候选集：只把前 `RERANK_WINDOW` 条交给模型。"""
        candidates = [{"chunk_id": index, "score": 0.5,
                       "payload": {"meeting_id": 1, "authority": "RAW", "text": f"第{index}片",
                                   "start_ms": index * 1000, "end_ms": index * 1000 + 500,
                                   "chunk_type": "TRANSCRIPT_WINDOW"}}
                      for index in range(1, kb_service.RERANK_CANDIDATES + 1)]
        captured: dict = {}

        async def fake_invoke(config, system, user, schema, call_type, biz_id, label, validator=None):
            captured["prompt"] = user
            return {"ranking": list(range(kb_service.RERANK_WINDOW, 0, -1))}

        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "1"}), \
             patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.qdrant, "hybrid_search", AsyncMock(return_value=candidates)), \
             patch.object(kb_service, "_chat_config", AsyncMock(return_value=self.config)), \
             patch.object(kb_service.model_factory, "invoke_json", AsyncMock(side_effect=fake_invoke)):
            hits = await kb_service.search_knowledge("灰度", allowed_meeting_ids=None, limit=3)
        self.assertIn(f"共 {kb_service.RERANK_WINDOW} 条", captured["prompt"])
        self.assertNotIn(f"[{kb_service.RERANK_WINDOW + 1}]", captured["prompt"],
                         "窗口之外的候选不该进提示词")
        self.assertEqual([hit.chunk_id for hit in hits],
                         [kb_service.RERANK_WINDOW, kb_service.RERANK_WINDOW - 1,
                          kb_service.RERANK_WINDOW - 2])

    async def test_enabled_fetches_more_candidates_then_cuts_to_limit(self):
        candidates = [{"chunk_id": index, "score": 0.5,
                       "payload": {"meeting_id": 1, "authority": "RAW", "text": f"第{index}片",
                                   "start_ms": index * 1000, "end_ms": index * 1000 + 500,
                                   "chunk_type": "TRANSCRIPT_WINDOW"}}
                      for index in range(1, kb_service.RERANK_CANDIDATES + 1)]
        ranking = {"ranking": list(range(kb_service.RERANK_CANDIDATES, 0, -1))}
        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "1"}), \
             patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.qdrant, "hybrid_search",
                          AsyncMock(return_value=candidates)) as search, \
             patch.object(kb_service, "_chat_config", AsyncMock(return_value=self.config)), \
             patch.object(kb_service.model_factory, "invoke_json",
                          AsyncMock(return_value=ranking)) as rerank_call:
            hits = await kb_service.search_knowledge("灰度", allowed_meeting_ids=None, limit=3)
        self.assertEqual(search.await_args.kwargs["limit"], kb_service.RERANK_CANDIDATES)
        self.assertEqual(rerank_call.await_args.args[4], kb_service.RERANK_CALL_TYPE)
        self.assertEqual(len(hits), 3)
        # 模型给的是**窗口内倒序**：窗口是前 10 条，倒过来最强的是第 10 片
        self.assertEqual([hit.chunk_id for hit in hits],
                         [kb_service.RERANK_WINDOW, kb_service.RERANK_WINDOW - 1,
                          kb_service.RERANK_WINDOW - 2])

    async def test_rerank_failure_falls_back_to_the_original_order(self):
        candidates = [{"chunk_id": index, "score": 0.5,
                       "payload": {"meeting_id": 1, "authority": "RAW", "text": f"第{index}片",
                                   "start_ms": index * 1000, "end_ms": index * 1000 + 500,
                                   "chunk_type": "TRANSCRIPT_WINDOW"}}
                      for index in range(1, 6)]
        with patch.dict(os.environ, {"KB_RERANK_ENABLED": "1"}), \
             patch.object(kb_service.model_factory, "embed_texts",
                          AsyncMock(return_value=[[0.1] * 8])), \
             patch.object(kb_service.qdrant, "hybrid_search", AsyncMock(return_value=candidates)), \
             patch.object(kb_service, "_chat_config", AsyncMock(return_value=self.config)), \
             patch.object(kb_service.model_factory, "invoke_json",
                          AsyncMock(side_effect=RuntimeError("重排模型调用失败"))):
            hits = await kb_service.search_knowledge("灰度", allowed_meeting_ids=None, limit=2)
        self.assertEqual([hit.chunk_id for hit in hits], [1, 2], "重排失败必须退回原顺序")


if __name__ == "__main__":
    unittest.main()


class PayloadIndexTest(IndexTranscriptTest):
    """payload 索引：过滤性能的前提，且**必须在"跳过重建"的那条路径上也建**。

    实测踩过：建索引原先只发生在 `_embed_and_store` 里，而内容未变化的库会在它之前
    就返回——于是最常见的情形（内容没变）永远建不出索引，过滤一直是慢查询且不报错。
    """

    async def test_collection_and_indexes_are_ensured_even_when_content_is_unchanged(self):
        task = await self._transcript()
        await self._run_index(task.id)                     # 第一次：真建
        with patch.object(kb_service.model_factory, "embed_texts", AsyncMock()) as embed, \
             patch.object(kb_service.qdrant, "ensure_collection", AsyncMock()) as ensure, \
             patch.object(kb_service.task_queue, "enqueue", AsyncMock()):
            kb_task_id = await kb_service.schedule_kb_index(
                kb_service.SOURCE_TRANSCRIPT, task.id, self.meeting.id, self.user.id)
            await kb_service.execute_kb_index(kb_task_id)
        self.assertEqual(embed.await_count, 0, "内容未变不该重嵌")
        ensure.assert_awaited(), "但索引就绪这一步必须照跑"
        kb_task = await KbIndexTask.get(id=kb_task_id)
        self.assertIn("内容未变化", kb_task.stage or "")
