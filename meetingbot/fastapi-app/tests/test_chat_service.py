"""问答编排的端到端单测：脱网跑通「提问 → 工具 → 引用编号 → 流式回答 → 落库」。

这里钉的是**链路的形状**而不是模型质量：事件顺序、引用编号与答案的对齐、
停止原因与 token 用量的落库、会话记忆的两层（滑窗 + 摘要）与水位线。
模型是脚本化的假模型（走 LangChain 的流式回调体系，因此 token 事件是真实产生的），
检索与向量库全部打桩，零外部调用。
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from models import (
    AiCallLog,
    AiModelConfig,
    ChatMessage,
    ChatSession,
    Meeting,
    User,
)
from pydantic import Field
from services import chat_graph, chat_service, kb_service
from services.kb_service import Hit
from tortoise import Tortoise


class ScriptedChatModel(BaseChatModel):
    """按脚本逐轮回答的假模型：支持流式与工具调用。

    实现的是 `_astream` 而不是普通函数——token 事件必须经 LangChain 的回调体系
    发出去，否则测不出"SSE 能不能拿到 token"这件真事。
    """

    rounds: list = Field(default_factory=list)
    usage: dict = Field(default_factory=lambda: {"input_tokens": 20, "output_tokens": 10,
                                                 "total_tokens": 30})
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001 - 与 LangChain 签名一致
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        raise NotImplementedError("脚本模型只走流式")

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        message = self.rounds[self.cursor]
        self.cursor += 1
        if getattr(message, "tool_calls", None):
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[
                {"name": call["name"], "args": json.dumps(call["args"], ensure_ascii=False),
                 "id": call["id"], "index": index}
                for index, call in enumerate(message.tool_calls)]))
        else:
            text = str(message.content)
            # 切成两段，验证"逐 token 推送"而不是整段一次给
            middle = max(1, len(text) // 2)
            for piece in (text[:middle], text[middle:]):
                if piece:
                    yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=self.usage))


def _tool_call(name: str, args: dict, call_id: str = "call-1") -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class ChatServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="chat-t", password="x", name="张晨")
        self.other = await User.create(username="chat-o", password="x", name="李月")
        self.meeting = await Meeting.create(
            meeting_no="MTG-C1", title="产品发布评审会议",
            start_time="2026-09-10 10:00:00", end_time="2026-09-10 11:00:00",
            creator_id=self.user.id, host_id=self.user.id, status="FINISHED")
        self.config = await AiModelConfig.create(
            name="本地对话", model_type="AGENT", provider="OPENAI_COMPATIBLE",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="unit-test-key", model_name="qwen-test", enabled=True)

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    def _hit(self, chunk_id: int = 501) -> Hit:
        return Hit(chunk_id=chunk_id, score=0.9, text="上线时间定在十月", meeting_id=self.meeting.id,
                   authority="RAW", chunk_type="TRANSCRIPT_WINDOW", field=None, item_index=None,
                   speaker="张晨", start_ms=120_000, end_ms=180_000, document_id=1, origin=None)

    async def _run(self, script: list, *, session: ChatSession | None = None,
                   hits: list[Hit] | None = None, question: str = "上线时间定了吗"):
        """跑一轮问答，返回 (事件列表, 会话)。检索被替换成固定命中。"""
        session = session or await chat_service.create_session(self.user)
        model = ScriptedChatModel(rounds=script)
        search = AsyncMock(return_value=hits if hits is not None else [self._hit()])
        events = []
        with patch.object(kb_service, "search_knowledge", search):
            async for kind, payload in chat_service.stream_answer(
                    session, question, user=self.user, model=model):
                events.append((kind, payload))
        return events, session, search

    async def test_tool_round_trip_yields_protocol_events_in_order(self):
        script = [
            AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "上线时间"})]),
            AIMessage(content="上线时间定在十月[1]。"),
        ]
        events, _, search = await self._run(script)
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[0], "meta")
        self.assertEqual(kinds[-1], "done")
        self.assertIn("tool_start", kinds)
        self.assertIn("tool_end", kinds)
        self.assertIn("citation", kinds)
        self.assertGreater(kinds.count("token"), 1, "token 必须是逐段推送的")
        answer = "".join(payload["text"] for kind, payload in events if kind == "token")
        self.assertEqual(answer, "上线时间定在十月[1]。")
        # 检索被调用且带上了权限集合（本会话没有限定范围 → 用户可见的全部会议）
        self.assertEqual(search.await_args.kwargs["allowed_meeting_ids"], [self.meeting.id])

    async def test_citations_are_numbered_and_unknown_refs_are_recorded(self):
        script = [
            AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "上线"})]),
            AIMessage(content="结论是十月[1]，另外还有[9]这条。" ),
        ]
        events, session, _ = await self._run(script)
        citations = [payload for kind, payload in events if kind == "citation"]
        self.assertEqual(citations[0]["ref"], 1)
        self.assertEqual(citations[0]["chunk_id"], 501)
        self.assertEqual(citations[0]["start_second"], 120)
        done = [payload for kind, payload in events if kind == "done"][0]
        self.assertEqual([item["ref"] for item in done["citations"]], [1])
        self.assertEqual(done["unknown_refs"], [9])

        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        stored = assistant.citations_json
        self.assertEqual([item["ref"] for item in stored["used"]], [1])
        self.assertEqual(stored["unknown_refs"], [9])
        self.assertEqual(len(stored["all"]), 1)
        self.assertEqual(assistant.tools_json[0]["name"], "search_knowledge")
        self.assertEqual(assistant.tools_json[0]["summary"], "1 条切片")
        self.assertTrue(assistant.tools_json[0]["ok"])

    async def test_turn_is_persisted_with_usage_from_call_log(self):
        # 脚本：先直接作答（没检索）→ 被提醒 → 仍然直接作答 → 这一轮判为"没有依据"。
        # 这一条同时钉住了"提醒"与"no_retrieval 标记"两件事。
        script = [AIMessage(content="没有查到相关会议。"), AIMessage(content="确实没有相关资料。")]
        events, session, _ = await self._run(script)
        rows = await ChatMessage.filter(session_id=session.id).order_by("id")
        self.assertEqual([row.role for row in rows], ["USER", "ASSISTANT"])
        self.assertEqual(rows[0].content, "上线时间定了吗")
        self.assertEqual(rows[1].content, "确实没有相关资料。")
        self.assertEqual(rows[1].stop_reason, chat_graph.STOP_NO_RETRIEVAL)
        self.assertIn("没有检索", rows[1].error_message)
        self.assertEqual(rows[1].token_count, 60)  # 两轮调用，每轮 30
        # 记账口径：ai_call_log 的 biz_id 就是这一轮助手消息的 id；
        # 这一轮跑了两次模型调用（首轮 + 提醒后重跑），所以是两行——"一行=一次真实调用"
        logs = await AiCallLog.filter(biz_id=rows[1].id)
        self.assertEqual(len(logs), 2)
        self.assertTrue(all(row.call_type == chat_service.CHAT_CALL_TYPE for row in logs))
        session_row = await ChatSession.get(id=session.id)
        self.assertEqual(session_row.message_count, 2)
        self.assertEqual(session_row.title, "上线时间定了吗")

    async def test_answer_with_citations_is_not_flagged(self):
        """有检索、有引用的正常轮次不该被标异常（否则标记就失去意义）。"""
        script = [
            AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "上线"})]),
            AIMessage(content="上线时间定在十月[1]。"),
        ]
        events, session, _ = await self._run(script)
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        self.assertEqual(assistant.stop_reason, chat_graph.STOP_FINISHED)
        self.assertIsNone(assistant.error_message)

    async def test_max_rounds_gate_is_reported_and_persisted(self):
        script = [AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "同一个词"})])
                  for _ in range(4)]
        os.environ["CHAT_MAX_ROUNDS"] = "1"
        try:
            events, session, _ = await self._run(script)
        finally:
            del os.environ["CHAT_MAX_ROUNDS"]
        done = [payload for kind, payload in events if kind == "done"][0]
        self.assertEqual(done["stop_reason"], chat_graph.STOP_MAX_ROUNDS)
        self.assertIn("最大推理轮数", done["notice"])
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        self.assertEqual(assistant.stop_reason, chat_graph.STOP_MAX_ROUNDS)

    async def test_tool_failure_does_not_break_the_answer(self):
        """检索挂了也要能回答：工具层把失败包成 ok=false 交给模型自纠。"""
        script = [
            AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "上线"})]),
            AIMessage(content="检索失败了，我先说不知道。"),
        ]
        session = await chat_service.create_session(self.user)
        model = ScriptedChatModel(rounds=script)
        with patch.object(kb_service, "search_knowledge",
                          AsyncMock(side_effect=kb_service.KbError("向量库不可达"))):
            events = []
            async for kind, payload in chat_service.stream_answer(
                    session, "上线时间", user=self.user, model=model):
                events.append((kind, payload))
        done = [payload for kind, payload in events if kind == "done"][0]
        self.assertEqual(done["stop_reason"], chat_graph.STOP_FINISHED)
        tool_end = [payload for kind, payload in events if kind == "tool_end"][0]
        self.assertIn("失败", tool_end["summary"])
        self.assertEqual([payload for kind, payload in events if kind == "citation"], [])

    async def test_scope_limits_which_meetings_the_tools_may_read(self):
        other_meeting = await Meeting.create(
            meeting_no="MTG-C2", title="别人的会", start_time="2026-09-11 10:00:00",
            end_time="2026-09-11 11:00:00", creator_id=self.other.id,
            host_id=self.other.id, status="FINISHED")
        session = await chat_service.create_session(self.user)
        session = await chat_service.get_session(session.id, self.user)
        await ChatSession.filter(id=session.id).update(
            scope_meeting_ids=[self.meeting.id, other_meeting.id])
        session = await chat_service.get_session(session.id, self.user)
        events, _, search = await self._run(
            [AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "上线"})]),
             AIMessage(content="好。")], session=session)
        # 范围内的"别人的会"不在权限内 → 取交集后只剩自己那场
        self.assertEqual(search.await_args.kwargs["allowed_meeting_ids"], [self.meeting.id])

    async def test_model_error_is_reported_and_persisted(self):
        session = await chat_service.create_session(self.user)
        model = ScriptedChatModel(rounds=[])  # 脚本为空 → 调用时抛 IndexError
        events = []
        async for kind, payload in chat_service.stream_answer(
                session, "会挂吗", user=self.user, model=model):
            events.append((kind, payload))
        self.assertEqual(events[-1][0], "error")
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        self.assertIn("问答模型调用失败", assistant.error_message)


class MemoryTest(unittest.IsolatedAsyncioTestCase):
    """会话记忆两层：滑窗取最近 N 条、摘要只吃水位线之后的消息。"""

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="mem-t", password="x", name="张晨")
        self.config = await AiModelConfig.create(
            name="本地对话", model_type="AGENT", provider="OPENAI_COMPATIBLE",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="unit-test-key", model_name="qwen-test", enabled=True)

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def _seed(self, session: ChatSession, count: int) -> list[ChatMessage]:
        rows = []
        for index in range(count):
            role = "USER" if index % 2 == 0 else "ASSISTANT"
            rows.append(await ChatMessage.create(session_id=session.id, role=role,
                                                 content=f"第{index}条"))
        return rows

    async def test_window_keeps_only_the_latest_messages(self):
        session = await chat_service.create_session(self.user)
        await self._seed(session, 12)
        session = await chat_service.get_session(session.id, self.user)
        with patch.dict(os.environ, {"CHAT_HISTORY_WINDOW": "4"}):
            memory = await chat_service.build_memory(session, user=self.user)
        # 1 条系统提示词 + 最近 4 条
        self.assertEqual(len(memory), 5)
        self.assertEqual(memory[0].content.startswith("你是企业会议知识助手"), True)
        self.assertEqual(memory[-1].content, "第11条")

    async def test_summary_compacts_old_messages_and_advances_the_watermark(self):
        session = await chat_service.create_session(self.user)
        rows = await self._seed(session, 14)
        session = await chat_service.get_session(session.id, self.user)
        with patch.dict(os.environ, {"CHAT_HISTORY_WINDOW": "4", "CHAT_SUMMARY_THRESHOLD": "6"}), \
                patch.object(chat_service.model_factory, "invoke_json",
                             AsyncMock(return_value="用户问过上线时间")):
            summarized = await chat_service.maybe_summarize(session)
        self.assertTrue(summarized)
        session = await chat_service.get_session(session.id, self.user)
        self.assertEqual(session.summary, "用户问过上线时间")
        # 水位线停在"窗口之外的最后一条"：第 9 条（id 升序的第 10 个）
        self.assertEqual(session.summary_upto_message_id, rows[9].id)
        with patch.dict(os.environ, {"CHAT_HISTORY_WINDOW": "4"}):
            memory = await chat_service.build_memory(session, user=self.user)
        # 系统提示词 + 摘要 + 窗口内 4 条
        self.assertEqual(len(memory), 6)
        self.assertIn("用户问过上线时间", memory[1].content)

    async def test_summary_is_not_repeated_when_below_threshold(self):
        session = await chat_service.create_session(self.user)
        await self._seed(session, 4)
        session = await chat_service.get_session(session.id, self.user)
        invoke = AsyncMock(return_value="不该被调用")
        with patch.object(chat_service.model_factory, "invoke_json", invoke):
            self.assertFalse(await chat_service.maybe_summarize(session))
        invoke.assert_not_awaited()

    async def test_summary_failure_does_not_block_the_answer(self):
        session = await chat_service.create_session(self.user)
        await self._seed(session, 14)
        session = await chat_service.get_session(session.id, self.user)
        with patch.dict(os.environ, {"CHAT_HISTORY_WINDOW": "4", "CHAT_SUMMARY_THRESHOLD": "6"}), \
                patch.object(chat_service.model_factory, "invoke_json",
                             AsyncMock(side_effect=RuntimeError("摘要模型挂了"))), \
                patch.object(chat_service.logger, "exception") as logged:
            self.assertFalse(await chat_service.maybe_summarize(session))
        logged.assert_called_once()

    async def test_failed_assistant_turns_stay_out_of_context(self):
        session = await chat_service.create_session(self.user)
        await ChatMessage.create(session_id=session.id, role="USER", content="问了一句")
        await ChatMessage.create(session_id=session.id, role="ASSISTANT", content="")
        session = await chat_service.get_session(session.id, self.user)
        memory = await chat_service.build_memory(session, user=self.user)
        self.assertEqual(len(memory), 2)
        self.assertEqual(memory[-1].content, "问了一句")

    async def test_scoped_session_puts_the_scope_into_the_prompt(self):
        meeting = await Meeting.create(
            meeting_no="MTG-M1", title="范围里的会", start_time="2026-09-10 10:00:00",
            end_time="2026-09-10 11:00:00", creator_id=self.user.id,
            host_id=self.user.id, status="FINISHED")
        session = await chat_service.create_session(self.user, meeting_ids=[meeting.id])
        self.assertEqual(session.scope_meeting_ids, [meeting.id])
        memory = await chat_service.build_memory(session, user=self.user)
        self.assertIn("范围里的会", memory[1].content)

    async def test_foreign_meeting_never_enters_the_scope(self):
        other = await User.create(username="mem-o", password="x", name="李月")
        foreign = await Meeting.create(
            meeting_no="MTG-M2", title="别人的会", start_time="2026-09-10 10:00:00",
            end_time="2026-09-10 11:00:00", creator_id=other.id,
            host_id=other.id, status="FINISHED")
        session = await chat_service.create_session(self.user, meeting_ids=[foreign.id])
        self.assertEqual(session.scope_meeting_ids, [])

    async def test_missing_model_config_is_readable(self):
        await AiModelConfig.filter(model_type="AGENT").update(enabled=False)
        with self.assertRaises(chat_service.ChatError):
            await chat_service.get_model_config()


class PromptTest(unittest.IsolatedAsyncioTestCase):
    """系统提示词：模板缺失有内置兜底，引用协议永远在末尾。"""

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_default_prompt_is_used_when_template_is_absent(self):
        prompt = await chat_service.get_system_prompt()
        self.assertIn("企业会议知识助手", prompt)
        self.assertIn("【引用规则】", prompt)

    async def test_database_template_wins_but_protocol_still_appended(self):
        from models import PromptTemplate
        await PromptTemplate.create(
            code="CHAT_ANSWER", name="问答", scene_type="CHAT",
            system_prompt="你是定制助手。", user_prompt="")
        prompt = await chat_service.get_system_prompt()
        self.assertTrue(prompt.startswith("你是定制助手。"))
        self.assertIn("【引用规则】", prompt)

    async def test_template_user_prompt_is_appended_after_the_question(self):
        from models import PromptTemplate
        await PromptTemplate.create(
            code="CHAT_ANSWER", name="问答", scene_type="CHAT",
            system_prompt="你是定制助手。", user_prompt="请用要点回答。")
        message = await chat_service.question_message("上线时间定了吗")
        self.assertTrue(message.content.startswith("上线时间定了吗"))
        self.assertIn("请用要点回答。", message.content)

    async def test_the_question_survives_even_if_the_template_forgets_it(self):
        from models import PromptTemplate
        await PromptTemplate.create(
            code="CHAT_ANSWER", name="问答", scene_type="CHAT",
            system_prompt="你是定制助手。", user_prompt="严格按以下格式：{不存在的变量}")
        message = await chat_service.question_message("上线时间定了吗")
        self.assertIn("上线时间定了吗", message.content)

    async def test_compaction_prompt_is_not_admin_editable(self):
        # 摘要提示词是内部压缩步骤，不进 Prompt 模板表：改不到就不存在"改坏了但没人知道"
        from models import PromptTemplate
        self.assertFalse(await PromptTemplate.filter(code="CHAT_SUMMARY").exists())
        self.assertIn("压缩", chat_service.SUMMARY_SYSTEM_PROMPT)


class SessionApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="sess-t", password="x", name="张晨")
        self.other = await User.create(username="sess-o", password="x", name="李月")

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_sessions_are_private_to_their_owner(self):
        mine = await chat_service.create_session(self.user)
        with self.assertRaises(chat_service.ChatError):
            await chat_service.get_session(mine.id, self.other)

    async def test_delete_is_soft_and_messages_survive(self):
        session = await chat_service.create_session(self.user)
        await ChatMessage.create(session_id=session.id, role="USER", content="留下我")
        await chat_service.delete_session(session.id, self.user)
        self.assertEqual((await ChatSession.get(id=session.id)).status, "DELETED")
        self.assertEqual(await ChatMessage.filter(session_id=session.id).count(), 1)
        with self.assertRaises(chat_service.ChatError):
            await chat_service.get_session(session.id, self.user)

    async def test_message_list_exposes_citations_and_stop_notice(self):
        session = await chat_service.create_session(self.user)
        await ChatMessage.create(
            session_id=session.id, role="ASSISTANT", content="回答[1]",
            citations_json={"used": [{"ref": 1, "meeting_id": 3}], "all": [], "unknown_refs": []},
            tools_json=[{"name": "search_knowledge", "summary": "1 条切片", "arguments": {}}],
            stop_reason=chat_graph.STOP_STALLED, token_count=12)
        rows = await chat_service.list_messages(session.id, self.user)
        self.assertEqual(rows[-1]["citations"], [{"ref": 1, "meeting_id": 3}])
        self.assertEqual(rows[-1]["notice"], chat_graph.STOP_NOTICE[chat_graph.STOP_STALLED])
        self.assertEqual(rows[-1]["tools"][0]["name"], "search_knowledge")

    async def test_session_list_pages_by_user(self):
        await chat_service.create_session(self.user)
        await chat_service.create_session(self.other)
        page = await chat_service.list_sessions(self.user)
        self.assertEqual(page["total"], 1)


if __name__ == "__main__":
    unittest.main()
