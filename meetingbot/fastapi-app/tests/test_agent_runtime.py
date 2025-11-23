"""P1 runtime 组件的零调用单测：RRF、覆盖计划、上下文预算、JUDGE 回退、outbox。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from models import AiModelConfig, KbChunk, KbDocument, KbIndexOutbox
from services import (
    chat_graph,
    chat_turn,
    coverage,
    hybrid,
    kb_outbox,
    model_config,
    pg_kb,
)
from services.agent_runtime import GuardrailChain, ToolRegistry
from services.chat_runtime import ChatRuntime, reset_current, set_current
from services.context_assembler import ContextAssembler
from tortoise import Tortoise


class RrfTest(unittest.TestCase):
    def test_rrf_prefers_document_in_both_lists(self):
        fused = hybrid.rrf_fuse([[1, 2, 3], [3, 4, 1]], limit=4)
        self.assertEqual(fused[0].chunk_id, 1)
        self.assertEqual({item.chunk_id for item in fused[:3]}, {1, 3, 2})

    def test_rrf_is_stable_for_same_input(self):
        first = hybrid.fuse_dense_and_bm25([5, 6], [6, 7])
        second = hybrid.fuse_dense_and_bm25([5, 6], [6, 7])
        self.assertEqual([i.chunk_id for i in first], [i.chunk_id for i in second])


class CoverageTest(unittest.TestCase):
    def test_cross_recording_plan_requires_all_sources(self):
        plan = coverage.build_plan("上午和下午两段分别说了什么", recordings=[3, 4])
        self.assertEqual(plan.intent, "cross_meeting")
        self.assertEqual(plan.required_sources, (3, 4))

    def test_single_recording_question_does_not_require_two_sources(self):
        plan = coverage.build_plan("上周发布定了什么", recordings=[3, 4])
        self.assertFalse(plan.is_cross_source)

    def test_slots_are_inferred_from_question(self):
        plan = coverage.build_plan("谁负责灰度，什么时候完成？")
        self.assertIn("owner", plan.required_slots)
        self.assertIn("deadline", plan.required_slots)

    def test_answer_shape_words_do_not_create_typed_slots(self):
        """“几点/多少”是答案形态，不是契约里的 typed slot；否则永远判缺失。"""
        plan = coverage.build_plan("发布会在几点开始，有几个议题？")
        self.assertEqual(plan.required_slots, ())

    def test_slot_is_covered_if_any_fact_has_a_substantive_basis(self):
        plan = coverage.build_plan("谁负责，什么时候完成？")
        state = coverage.EvidenceState(plan=plan)
        coverage.observe_tool_result(state, "list_facts", {
            "ok": True,
            "facts": [
                {"slot_status": {"owner": {"basis": "stated"}}},
                {"slot_status": {"owner": {"basis": "not_mentioned"}}},
            ],
        })
        self.assertNotIn("owner", state.missing_slots)

    def test_observe_tool_result_and_supplement(self):
        plan = coverage.build_plan("上午和下午两段分别说了什么", recordings=[3, 4])
        state = coverage.EvidenceState(plan=plan)
        self.assertTrue(state.needs_supplement())
        coverage.observe_tool_result(state, "search_knowledge", {
            "ok": True,
            "hits": [{"chunk_type": "TRANSCRIPT_WINDOW", "source_id": 3, "chunk_id": 10}],
        })
        self.assertEqual(state.missing_sources, [4])
        self.assertIn("4", coverage.supplement_hint(state))


class CoverageNodeTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_source_produces_one_hint_then_stops(self):
        plan = coverage.build_plan("上午和下午两段分别说了什么", recordings=[3, 4])
        evidence = coverage.EvidenceState(plan=plan)
        runtime = ChatRuntime(call_model=None, tools=None, evidence=evidence)
        token = set_current(runtime)
        try:
            state = {"messages": [ToolMessage(
                name="search_knowledge", tool_call_id="call-1",
                content=json.dumps({"ok": True, "hits": [{
                    "chunk_type": "TRANSCRIPT_WINDOW", "source_id": 3, "chunk_id": 10,
                }]}),
            )]}
            first = await chat_graph.coverage_node(state)
            self.assertEqual(evidence.missing_sources, [4])
            self.assertIn("4", first["messages"][0].content)
            second = await chat_graph.coverage_node({"messages": state["messages"]})
            self.assertNotIn("messages", second)
        finally:
            reset_current(token)


class ContextAssemblerTest(unittest.TestCase):
    def test_system_and_question_are_always_kept(self):
        assembler = ContextAssembler(limit=80)
        history = [
            SystemMessage("规则"),
            HumanMessage("旧问题" * 20),
            AIMessage("旧回答" * 20),
            HumanMessage("新问题"),
        ]
        messages = assembler.assemble(history, question=HumanMessage("当前问题"))
        self.assertEqual(messages[0].content, "规则")
        self.assertEqual(messages[-1].content, "当前问题")
        self.assertNotIn("旧问题" * 20, [m.content for m in messages])

    def test_newest_message_too_large_stops_instead_of_keeping_older(self):
        assembler = ContextAssembler(limit=60)
        history = [
            SystemMessage("规则"),
            HumanMessage("旧的小消息"),
            HumanMessage("新的太大" * 30),
        ]
        messages = assembler.assemble(history, question=HumanMessage("当前"))
        self.assertNotIn("旧的小消息", [m.content for m in messages])


class ResumeAnswerTest(unittest.TestCase):
    def test_streamed_full_answer_replaces_persisted_prefix(self):
        state = {"messages": [AIMessage(content="完整答案")]}
        self.assertEqual(
            chat_turn._resume_answer_text("前半段", "前半段完整答案", state),
            "前半段完整答案")

    def test_empty_stream_recovers_answer_from_checkpoint(self):
        state = {"messages": [AIMessage(content="checkpoint 里的完整答案")]}
        self.assertEqual(
            chat_turn._resume_answer_text("半段", "", state),
            "checkpoint 里的完整答案")

    def test_empty_stream_without_checkpoint_keeps_persisted_prefix(self):
        self.assertEqual(chat_turn._resume_answer_text("半段", "", {}), "半段")


class RuntimeFacadeTest(unittest.TestCase):
    def test_tool_registry_is_the_five_tool_source(self):
        registry = ToolRegistry()
        self.assertEqual(len(registry.specs), 5)
        self.assertEqual(
            {spec.name for spec in registry.specs},
            {"search_knowledge", "list_facts", "get_minutes", "get_transcript_excerpt",
             "query_meeting_state"},
        )

    def test_guardrail_chain_keeps_annotation_semantics(self):
        flags = GuardrailChain().entity_flags(
            "负责人李婷负责上线", "负责人张晨负责上线")
        self.assertTrue(flags)


class JudgeConfigTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]})
        await Tortoise.generate_schemas()

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_falls_back_to_agent_when_judge_missing(self):
        await AiModelConfig.create(
            name="agent", model_type="AGENT", provider="OPENAI_COMPATIBLE",
            base_url="https://example.com/v1", api_key="k", model_name="m",
            timeout_seconds=60, enabled=True,
        )
        config = await model_config.resolve("JUDGE", fallback="AGENT")
        self.assertEqual(config.model_type, "AGENT")

    async def test_uses_judge_when_present(self):
        await AiModelConfig.create(
            name="agent", model_type="AGENT", provider="OPENAI_COMPATIBLE",
            base_url="https://example.com/v1", api_key="k", model_name="m",
            timeout_seconds=60, enabled=True,
        )
        await AiModelConfig.create(
            name="judge", model_type="JUDGE", provider="OPENAI_COMPATIBLE",
            base_url="https://example.com/v1", api_key="k", model_name="judge-m",
            timeout_seconds=60, enabled=True,
        )
        config = await model_config.resolve("JUDGE", fallback="AGENT")
        self.assertEqual(config.model_type, "JUDGE")


class OutboxTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]})
        await Tortoise.generate_schemas()

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_mark_source_state_upserts_and_bumps_version(self):
        await kb_outbox.mark_source_state("MINUTES", 7, 2, kb_outbox.STATE_INDEX)
        first = await KbIndexOutbox.get(source_type="MINUTES", source_id=7)
        await kb_outbox.mark_source_state("MINUTES", 7, 2, kb_outbox.STATE_DELETE)
        second = await KbIndexOutbox.get(source_type="MINUTES", source_id=7)
        self.assertEqual(second.desired_state, kb_outbox.STATE_DELETE)
        self.assertGreater(second.version, first.version)

    async def test_stale_worker_cannot_overwrite_a_newer_desired_state(self):
        """旧 worker 完成时只能结束自己认领的版本，不能把新 INDEX/DELETE 覆盖成 DONE。"""
        await KbIndexOutbox.create(
            meeting_id=2, source_type="MINUTES", source_id=8,
            desired_state=kb_outbox.STATE_INDEX, status=kb_outbox.STATUS_PROCESSING,
            version=1,
        )
        stale = await KbIndexOutbox.get(source_type="MINUTES", source_id=8)
        await kb_outbox.mark_source_state("MINUTES", 8, 2, kb_outbox.STATE_DELETE)
        await kb_outbox._finish(stale, ok=True)
        current = await KbIndexOutbox.get(source_type="MINUTES", source_id=8)
        self.assertEqual(current.status, kb_outbox.STATUS_PENDING)
        self.assertEqual(current.desired_state, kb_outbox.STATE_DELETE)
        self.assertEqual(current.version, 2)

    async def test_purge_source_clears_both_derived_stores_and_mysql_rows(self):
        document = await KbDocument.create(
            meeting_id=2, source_type="MINUTES", source_id=9, task_id=1,
            authority="CONFIRMED", content_hash="x", chunk_count=1,
            embed_model="m", embed_dim=8,
        )
        await KbChunk.create(
            document_id=document.id, meeting_id=2, chunk_no=1,
            chunk_type="MINUTES_ITEM", text="条目",
        )
        with patch.object(pg_kb, "delete_chunks", new=AsyncMock()) as drop, \
             patch.object(pg_kb, "delete_by_source", new=AsyncMock()) as drop_pg:
            await kb_outbox.purge_source("MINUTES", 9)
        drop.assert_awaited_once()
        drop_pg.assert_awaited_once_with("MINUTES", 9)
        self.assertEqual(await KbDocument.filter(source_type="MINUTES", source_id=9).count(), 0)
        self.assertEqual(await KbChunk.filter(document_id=document.id).count(), 0)

    async def test_purge_keeps_mysql_when_derived_delete_fails(self):
        document = await KbDocument.create(
            meeting_id=2, source_type="MINUTES", source_id=10, task_id=1,
            authority="CONFIRMED", content_hash="x", chunk_count=1,
            embed_model="m", embed_dim=8,
        )
        await KbChunk.create(
            document_id=document.id, meeting_id=2, chunk_no=1,
            chunk_type="MINUTES_ITEM", text="条目",
        )
        with patch.object(pg_kb, "delete_chunks",
                          new=AsyncMock(side_effect=RuntimeError("PG down"))), \
             patch.object(pg_kb, "delete_by_source", new=AsyncMock()), \
             self.assertRaises(RuntimeError):
            await kb_outbox.purge_source("MINUTES", 10)
        self.assertEqual(
            await KbDocument.filter(source_type="MINUTES", source_id=10).count(), 1,
            "派生存储没清干净时不能先删 MySQL 真源")


if __name__ == "__main__":
    unittest.main()
