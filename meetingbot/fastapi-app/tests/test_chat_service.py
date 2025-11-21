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
from datetime import datetime
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
    TranscriptionTask,
    User,
)
from pydantic import Field
from services import chat_graph, chat_service, entity_check, kb_service
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
        # 真实模型会"边说边调工具"（正文 + tool_calls 同一条消息），所以正文照发——
        # 只发工具调用会让"答了半句再卡住"这种形态在测试里无法构造
        text = str(message.content or "")
        if text:
            # 切成两段，验证"逐 token 推送"而不是整段一次给
            middle = max(1, len(text) // 2)
            for piece in (text[:middle], text[middle:]):
                if piece:
                    yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        if getattr(message, "tool_calls", None):
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[
                {"name": call["name"], "args": json.dumps(call["args"], ensure_ascii=False),
                 "id": call["id"], "index": index}
                for index, call in enumerate(message.tool_calls)]))
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

    async def test_stalled_turn_tells_the_user_where_it_got_stuck(self):
        """q26/q27 的形态：工具一直失败、模型原样重试 → 停滞闸掐断。

        停在闸门上是正确行为；**说不清"卡在哪"才是缺陷**——修复前用户只看到一句
        "已停止继续检索"，配一个空气泡，无从知道工具卡在参数上。
        """
        script = [AIMessage(content="", tool_calls=[_tool_call("search_knowledge",
                                                              {"query": "同一个词"})])
                  for _ in range(4)]
        session = await chat_service.create_session(self.user)
        model = ScriptedChatModel(rounds=script)
        with patch.object(kb_service, "search_knowledge",
                          AsyncMock(side_effect=kb_service.KbError("向量库不可达"))):
            events = []
            async for kind, payload in chat_service.stream_answer(
                    session, "下午这场会定了哪些行动项", user=self.user, model=model):
                events.append((kind, payload))
        self.assertEqual(events[-1][0], "error", "什么都没给出来的一轮走错误通道")
        message = events[-1][1]["message"]
        self.assertIn("原地重复", message)
        self.assertIn("search_knowledge", message)
        self.assertIn("向量库不可达", message)
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        self.assertEqual(assistant.stop_reason, chat_graph.STOP_STALLED)
        self.assertEqual(assistant.error_message, message, "页面与库里的说法必须一致")
        self.assertEqual(assistant.content, "")

    async def test_stalled_turn_with_a_partial_answer_keeps_the_done_event(self):
        """已经给用户答了话才停滞：结局仍走 done，原因随 payload 带出（红色告警只给"什么都没给"的那轮）。"""
        repeat = _tool_call("search_knowledge", {"query": "同一个词"})
        script = [AIMessage(content="", tool_calls=[repeat]),
                  AIMessage(content="", tool_calls=[repeat]),
                  # 第三轮带上正文：它会被推给用户，于是这一轮不再是"什么都没给"
                  AIMessage(content="检索一直失败，我只能先说明情况。", tool_calls=[repeat])]
        session = await chat_service.create_session(self.user)
        model = ScriptedChatModel(rounds=script)
        with patch.object(kb_service, "search_knowledge",
                          AsyncMock(side_effect=kb_service.KbError("向量库不可达"))):
            events = []
            async for kind, payload in chat_service.stream_answer(
                    session, "上线时间", user=self.user, model=model):
                events.append((kind, payload))
        done = [payload for kind, payload in events if kind == "done"][0]
        self.assertEqual(done["stop_reason"], chat_graph.STOP_STALLED)
        self.assertIn("原地重复", done["notice"])
        self.assertIn("向量库不可达", done["detail"])
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        self.assertIsNone(assistant.error_message, "有答案的一轮不该同时报错")
        self.assertIn("只能先说明情况", assistant.content)


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
        # 助手行进上下文时被压成"结论 + 引用编号"，用户行原样
        self.assertEqual(memory[-1].content, "第11条\n（本回答未引用条目）")

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
        self.assertIn("【资料覆盖规则】", prompt)
        self.assertIn("【引用规则】", prompt)

    def test_missing_slot_rule_covers_duration_and_count(self):
        """q28 的现场：工具只给议题标题，答案却直接说“没记录时长/数量”。

        缺槽位回退规则必须覆盖时长/数量/时间点，而不只是负责人/截止时间。
        """
        protocol = chat_service.SOURCE_COVERAGE_PROTOCOL
        for slot in ("负责人", "协作人", "截止时间", "时长", "数量", "时间点"):
            self.assertIn(slot, protocol)

    async def test_database_template_wins_but_protocol_still_appended(self):
        from models import PromptTemplate
        await PromptTemplate.create(
            code="CHAT_ANSWER", name="问答", scene_type="CHAT",
            system_prompt="你是定制助手。", user_prompt="")
        prompt = await chat_service.get_system_prompt()
        self.assertTrue(prompt.startswith("你是定制助手。"))
        self.assertIn("【资料覆盖规则】", prompt)
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

    async def test_history_citations_reads_previous_used_refs(self):
        from models import ChatMessage
        await ChatMessage.create(
            session_id=1, role="ASSISTANT", content="x",
            citations_json={"used": [{"ref": 3, "quote": "乙"}]})
        mapping = await chat_service._history_citations(1, 999)
        self.assertEqual(mapping[3]["quote"], "乙")


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


class AnswerCompressionTest(unittest.TestCase):
    """历史助手进上下文时压成"结论 + 引用编号"：原文完整留在库里（审计链不动）。

    实测依据：单轮 token 随会话从 5.7k 涨到 26.5k，涨的主要是上一轮那份 20–30 条断言的
    全文；而依据段逐条抄的就是检索原文，同一场会反复问时下一轮还会重新检索到。
    """

    ANSWER = (
        "**结论**\n本次灰度 8 月 20 日开始，覆盖 10% 的活跃用户。\n\n"
        "**依据**\n"
        "1. 人工确认纪要明确记录：“复盘 8 月 20 日至 26 日灰度发布数据”[1]。\n"
        "2. 会议转写原话中，张晨说明：“本次灰度从 8 月 20 日开始”（start_second=51）[2]。"
    )

    def test_evidence_section_is_dropped_and_refs_are_kept(self):
        compressed = chat_service.compress_answer(self.ANSWER, citations=[2, 1])
        self.assertIn("8 月 20 日开始", compressed)
        self.assertNotIn("start_second=51", compressed, "依据段（逐条抄的检索原文）不该进上下文")
        self.assertIn("[1][2]", compressed, "编号必须留着，模型才知道结论有依据")
        self.assertLess(len(compressed), len(self.ANSWER))

    def test_full_text_is_not_touched(self):
        # 压缩是"进上下文那一份"，不改落库的那一份（这条约束由调用方保证，
        # 这里钉住函数本身是纯函数、不改入参）
        original = self.ANSWER
        chat_service.compress_answer(original, citations=[1])
        self.assertEqual(original, self.ANSWER)

    def test_answer_without_evidence_section_keeps_the_head_and_says_so(self):
        long_answer = "结论：" + "内容" * 300
        compressed = chat_service.compress_answer(long_answer, citations=[])
        self.assertLess(len(compressed), len(long_answer))
        self.assertIn("后文略", compressed, "截断必须显式标注，不能假装完整")
        self.assertIn("未引用条目", compressed)

    def test_empty_answer_is_not_invented(self):
        self.assertEqual(chat_service.compress_answer("", citations=[]), "（本回答未引用条目）")


class SummaryGuardTest(unittest.IsolatedAsyncioTestCase):
    """摘要加固：摘要**不是事实来源**（编造 → 答案 → 摘要 → 后续回答的依据）。"""

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="sum-t", password="x", name="张晨")
        await AiModelConfig.create(
            name="本地对话", model_type="AGENT", provider="OPENAI_COMPATIBLE",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="unit-test-key", model_name="qwen-test", enabled=True)

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    def test_prompt_forbids_importing_facts(self):
        prompt = chat_service.SUMMARY_SYSTEM_PROMPT
        self.assertIn("不是事实来源", prompt)
        self.assertIn("不要引入原文没有", prompt.replace("绝不引入原文没有", "不要引入原文没有"))
        self.assertIn("引用编号", prompt)

    async def test_assistant_rows_are_rendered_with_their_refs(self):
        session = await chat_service.create_session(self.user)
        await ChatMessage.create(session_id=session.id, role="USER", content="上线时间？")
        await ChatMessage.create(
            session_id=session.id, role="ASSISTANT", content="十月上线[1]。",
            citations_json={"used": [{"ref": 1}, {"ref": 3}], "all": [], "unknown_refs": []})
        await ChatMessage.create(session_id=session.id, role="ASSISTANT", content="没有依据的一句。")
        rows = await ChatMessage.filter(session_id=session.id).order_by("id")
        transcript = chat_service._summary_transcript(list(rows))
        self.assertIn("助手（引用 [1] [3]）：十月上线[1]。", transcript)
        self.assertIn("助手（无引用）：没有依据的一句。", transcript)


class EntityCheckTest(unittest.TestCase):
    """实体核查：答案里的"新东西"要能被标出来（只标注、不拦截）。

    回放素材是**真实的 q14 答案**（2026-10-16 首测产物，`results/rag_packet_first_test.json`
    的 q14 答案节选）：种子里零出现的"李婷"与通知渠道被写进了答案，而产品侧当时
    没有任何环节会报警——判定侧 faithsfulness 逮到的那条不支撑断言就是它。
    """

    # 真实 q14 答案里的那句（含编造的人名与渠道）
    Q14_SENTENCE = (
        "此前对话摘要中提到李婷、8月31日17:00、产品内弹窗/官网公告/邮件通知等信息。"
    )
    # 同一轮检索到的内容（首测 q14 的引用原文节选：里面没有"李婷"，也没有那些渠道）
    RETRIEVED = [
        {"ref": 1, "quote": "[51s][张晨] 本次灰度从8月20日开始覆盖10%的活跃用户，"
                            "到8月26日扩大到30%",
         "source_text": "[51s][张晨] 本次灰度从8月20日开始覆盖10%的活跃用户"},
        {"ref": 2, "quote": "[205s][系统管理员] 可以通稿，定稿时间就定在发布前一日下午六点前",
         "source_text": "[205s][系统管理员] 可以通稿，定稿时间就定在发布前一日下午六点前"},
    ]

    def test_the_real_q14_fabrication_is_flagged(self):
        flags = entity_check.check_entities(self.Q14_SENTENCE, self.RETRIEVED)
        texts = [flag["text"] for flag in flags]
        self.assertIn("李婷", texts, "种子里零出现的人名必须被标出")
        self.assertIn("8月31日", texts, "检索结果里没有的日期必须被标出")

    def test_a_faithful_answer_is_not_flagged(self):
        answer = "张晨说：本次灰度从8月20日开始覆盖10%的活跃用户[1]。"
        self.assertEqual(entity_check.check_entities(answer, self.RETRIEVED), [])

    def test_citation_numbers_are_not_entities(self):
        answer = "结论来自人工确认纪要[1][2]。"
        self.assertEqual(entity_check.check_entities(answer, self.RETRIEVED), [])

    def test_number_format_differences_are_not_fabrications(self):
        # 原话写"8 月 20 日"（带空格）、答案写"8月20日"：是同一件事
        retrieved = [{"quote": "本次灰度从 8 月 20 日开始"}]
        self.assertEqual(entity_check.check_entities("灰度从8月20日开始", retrieved), [])

    def test_role_phrase_with_a_copula_does_not_capture_the_prefix(self):
        """'负责人为王工' 里的人名是王工，不是 '为王工'（实测扩展集的高频误报）。"""
        retrieved = [{"quote": "负责人王工，协作人陈琳"}]
        self.assertEqual(entity_check.check_entities("负责人为王工。", retrieved), [])

    def test_locator_seconds_are_not_meeting_numbers(self):
        """'224s–293s' 是引用锚点，不是会议事实（q09 的误报）。"""
        retrieved = [{"quote": "音频会按时长切分，转写完成后生成带时间轴的片段"}]
        answer = "张晨在 224s–293s 的原话：音频会按时长切分。"
        self.assertEqual(entity_check.check_entities(answer, retrieved), [])

    def test_tool_messages_are_part_of_the_entity_haystack(self):
        from langchain_core.messages import ToolMessage
        state = {"messages": [ToolMessage(content="recorded_at=2026-09-10 14:00",
                                          tool_call_id="t")]}
        haystack = chat_service._entity_haystack(
            chat_service.citations.CitationRegistry(), state)
        self.assertIn("14:00", haystack)

    def test_bare_small_counts_are_not_meeting_numbers(self):
        self.assertEqual(entity_check._numbers("共 4 项，第 2 条"), [])
        self.assertEqual(entity_check._numbers("2026年9月10日 17:00 12%"),
                         ["2026年9月10日", "17:00", "12%"])

    def test_nothing_retrieved_means_no_verdict(self):
        """没有检索结果时**不判**：那会变成"凡事都标"，标注失去意义。"""
        self.assertEqual(entity_check.check_entities("李婷负责这件事", []), [])

    def test_flags_are_capped(self):
        answer = "提到甲甲、乙乙、丙丙、丁丁、戊戊、己己。"
        self.assertLessEqual(len(entity_check.check_entities(answer, self.RETRIEVED, limit=3)), 3)

    def test_description_names_the_kinds(self):
        text = entity_check.describe([{"kind": "person", "text": "李婷", "reason": ""},
                                      {"kind": "number", "text": "8月31日", "reason": ""}])
        self.assertIn("人名 李婷", text)
        self.assertIn("数字/日期 8月31日", text)
        self.assertIn("没有拦截", text)
        self.assertEqual(entity_check.describe([]), "")


class EntityFlagPipelineTest(unittest.IsolatedAsyncioTestCase):
    """链路上：带编造人名的回答要被标注进 error_message，且 stop_reason 不变。"""

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="ent-t", password="x", name="张晨")
        self.meeting = await Meeting.create(
            meeting_no="MTG-E1", title="产品发布评审会议",
            start_time="2026-09-10 10:40:00", end_time="2026-09-10 13:00:00",
            creator_id=self.user.id, host_id=self.user.id, status="FINISHED")
        await AiModelConfig.create(
            name="本地对话", model_type="AGENT", provider="OPENAI_COMPATIBLE",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="unit-test-key", model_name="qwen-test", enabled=True)

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_fabricated_name_is_annotated_but_not_blocked(self):
        from services.kb_service import Hit
        hit = Hit(chunk_id=601, score=0.9, text="[51s][张晨] 本次灰度从8月20日开始覆盖10%的活跃用户",
                  meeting_id=self.meeting.id, authority="RAW", chunk_type="TRANSCRIPT_WINDOW",
                  field=None, item_index=None, speaker="张晨", start_ms=51_000,
                  end_ms=60_000, document_id=1, origin=None)
        script = [
            AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "灰度"})]),
            # 用"由李婷负责"这种**可检测**的写法：光写"李婷负责"是文档里
            # 明写的漏检形态（没有角色词、也没有前置动词时不捞候选）
            AIMessage(content="由李婷负责这次灰度，来源见[1]。"),
        ]
        session = await chat_service.create_session(self.user)
        model = ScriptedChatModel(rounds=script)
        with patch.object(kb_service, "search_knowledge", AsyncMock(return_value=[hit])):
            events = []
            async for kind, payload in chat_service.stream_answer(
                    session, "谁负责灰度", user=self.user, model=model):
                events.append((kind, payload))
        # 只标注、不拦截：本轮仍是正常结束（done），标注在 entity_flags 与 error_message
        kinds = [kind for kind, _ in events]
        self.assertIn("done", kinds)
        self.assertNotIn("error", kinds)
        done = [payload for kind, payload in events if kind == "done"][0]
        self.assertEqual(done["stop_reason"], "finished")
        self.assertIn("李婷", [flag["text"] for flag in done["entity_flags"]])
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        self.assertEqual(assistant.stop_reason, "finished")
        self.assertIn("李婷", assistant.error_message or "")
        self.assertIn("没有拦截", assistant.error_message or "")
        self.assertEqual(assistant.stop_reason, chat_graph.STOP_FINISHED, "标注不改停止原因")
        self.assertEqual(assistant.content, "由李婷负责这次灰度，来源见[1]。", "正文原样保留")


class EntityCheckFalsePositiveTest(unittest.TestCase):
    """假阳性回归：只标注不拦截**不代表可以乱标注**——噪声进到红色告警里这个功能就废了。"""

    def test_role_nouns_are_not_names(self):
        # 实测踩过的假阳性："来自人工确认纪要"被切成"来自人工"+"确认"
        retrieved = [{"quote": "可以通稿，定稿时间就定在发布前一日下午六点前"}]
        self.assertEqual(
            entity_check.check_entities("结论来自人工确认纪要[1]。", retrieved), [])

    def test_a_faithful_attribution_is_not_flagged(self):
        retrieved = [{"quote": "张晨汇报：本次灰度从8月20日开始，到8月26日扩大到30%"}]
        self.assertEqual(
            entity_check.check_entities("产品负责人陈琳说月度数据已核对[1]。", retrieved),
            [{"kind": "person", "text": "陈琳",
              "reason": "本轮检索结果里没有出现过这个名字"}],
            "只标出检索结果里没有的陈琳；张晨在检索结果里出现过，不标")


class QueryPlanTest(unittest.TestCase):
    """查询规划：触发门、归一化、提示渲染。**默认关**，开了才多一次调用。"""

    def test_disabled_by_default(self):
        from services import query_plan
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KB_QUERY_PLAN_ENABLED", None)
            self.assertFalse(query_plan.plan_enabled())
        with patch.dict(os.environ, {"KB_QUERY_PLAN_ENABLED": "1"}):
            self.assertTrue(query_plan.plan_enabled())

    def test_trigger_gate_only_fires_on_relative_time_or_reference(self):
        """没有相对时间/指代的问题不值得多花一次调用——触发门把它们挡在外面。"""
        from services import query_plan
        self.assertEqual(query_plan.trigger_terms("灰度从什么时候开始"), [])
        self.assertEqual(query_plan.trigger_terms("最近七天的调用成功率是多少"), ["最近"])
        self.assertTrue(query_plan.trigger_terms("下午这场会定了哪些行动项"))
        self.assertEqual(query_plan.trigger_terms(""), [])

    def test_normalize_keeps_the_original_question_when_rewriting_is_empty(self):
        from services import query_plan
        plan = query_plan.normalize({"intent": "single_fact", "rewritten": ""}, "最近七天呢")
        self.assertEqual(plan["rewritten"], "最近七天呢", "改写为空要回退原问题")
        self.assertTrue(any("回退" in issue for issue in plan["issues"]))

    def test_normalize_rejects_out_of_scope_intent_and_tools(self):
        from services import query_plan
        plan = query_plan.normalize(
            {"intent": "查天气", "rewritten": "x", "suggested_tools": ["drop_database", "list_facts"]},
            "最近七天")
        self.assertEqual(plan["intent"], "single_fact", "越界意图回退到最保守的一类")
        self.assertEqual(plan["suggested_tools"], ["list_facts"], "幻觉工具名不许进建议")
        self.assertTrue(any("越界" in issue for issue in plan["issues"]))
        self.assertTrue(any("幻觉" in issue for issue in plan["issues"]))

    def test_hint_says_it_is_only_a_suggestion(self):
        from services import query_plan
        hint = query_plan.plan_hint({"intent": "aggregation", "rewritten": "9 月 1 日发布了哪些功能",
                                     "suggested_tools": ["list_facts"], "needs_retrieval": True,
                                     "rewrite_in_scope": True})
        self.assertIn("枚举聚合", hint)
        self.assertIn("9 月 1 日发布了哪些功能", hint)
        self.assertIn("list_facts", hint)
        self.assertIn("只是建议", hint)

    def test_low_confidence_abstains_and_falls_back_to_retrieval(self):
        """弃权路径（行业共识：低置信度宁可按检索走，也不要送错工具）。

        低置信度时：丢掉工具建议（它是噪声）、强制 needs_retrieval、提示里明说按检索处理。
        """
        from services import query_plan
        plan = query_plan.normalize({"intent": "procedure", "confidence": 0.3,
                                     "rewritten": "问", "suggested_tools": ["list_facts"],
                                     "needs_retrieval": False}, "问")
        self.assertTrue(plan["low_confidence"])
        self.assertEqual(plan["suggested_tools"], [])
        self.assertTrue(plan["needs_retrieval"], "弃权时必须要求检索")
        self.assertIn("按检索处理", query_plan.plan_hint(plan))

    def test_confidence_is_clamped_and_unknown_is_tolerated(self):
        from services import query_plan
        high = query_plan.normalize({"intent": "single_fact", "confidence": 1.4,
                                     "rewritten": "问"}, "问")
        self.assertEqual(high["confidence"], 1.0)
        self.assertFalse(high["low_confidence"])
        missing = query_plan.normalize({"intent": "single_fact", "rewritten": "问"}, "问")
        self.assertIsNone(missing["confidence"], "模型没给置信度时不假装知道")
        self.assertFalse(missing["low_confidence"], "没给不按低置信处理（那是另一种猜）")

    def test_sub_questions_are_cleaned_and_capped(self):
        """子问句：空串丢掉、重复去掉、与原句相同的丢掉、单条不算分解、超过上限截断。"""
        from services import query_plan
        plan = query_plan.normalize({"intent": "aggregation", "rewritten": "问",
                                     "sub_questions": ["A？", "A？", "", "问",
                                                       "B？", "C？", "D？", "E？", "F？"]}, "问")
        self.assertEqual(plan["sub_questions"], ["A？", "B？", "C？", "D？"])
        single = query_plan.normalize({"intent": "single_fact", "rewritten": "问",
                                       "sub_questions": ["只有一个"]}, "问")
        self.assertEqual(single["sub_questions"], [], "只有一条不算分解")

    def test_hint_asks_for_separate_retrieval_on_multi_part_questions(self):
        from services import query_plan
        hint = query_plan.plan_hint({"intent": "aggregation", "rewritten": "问",
                                     "sub_questions": ["灰度分几步？", "回滚要多久？"],
                                     "suggested_tools": ["search_knowledge"]})
        self.assertIn("分别检索", hint)
        self.assertIn("1. 灰度分几步？", hint)

    def test_hint_drops_the_rewrite_when_it_was_out_of_scope(self):
        """触发门只管改写：没有相对时间/指代时，改写那一句不进提示（本来就没改写空间）。"""
        from services import query_plan
        plan = {"intent": "aggregation", "question": "首周复盘要看哪五项数据",
                "rewritten": "首周复盘要看哪五项数据", "rewrite_in_scope": False,
                "suggested_tools": ["list_facts"]}
        hint = query_plan.plan_hint(plan)
        self.assertIn("枚举聚合", hint)
        self.assertIn("list_facts", hint)
        self.assertNotIn("落到事实后是", hint)


class QueryPlanPipelineTest(unittest.IsolatedAsyncioTestCase):
    """链路上：规划开着时注入一条提示、落 plan_json；失败时不注入也不报错。"""

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="plan-t", password="x", name="张晨")
        self.meeting = await Meeting.create(
            meeting_no="MTG-P1", title="产品发布评审会议",
            start_time="2026-09-10 10:40:00", end_time="2026-09-10 13:00:00",
            creator_id=self.user.id, host_id=self.user.id, status="FINISHED")
        await AiModelConfig.create(
            name="本地对话", model_type="AGENT", provider="OPENAI_COMPATIBLE",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="unit-test-key", model_name="qwen-test", enabled=True)

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    def _plan(self):
        return {"enabled": True, "used": True, "triggers": ["最近"],
                "intent": "aggregation", "rewritten": "最近七天的调用成功率",
                "suggested_tools": ["list_facts"], "needs_retrieval": True, "issues": []}

    async def _run(self, question="最近七天的调用成功率是多少"):
        from services import query_plan
        script = [AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "成功率"})]),
                  AIMessage(content="根据纪要[1]，是 98.6%。")]
        session = await chat_service.create_session(self.user)
        events = []
        grounded = Hit(chunk_id=1, score=1.0, text="调用成功率是 98.6%",
                       meeting_id=self.meeting.id, authority="RAW",
                       chunk_type="TRANSCRIPT_WINDOW", field=None, item_index=None,
                       speaker="张晨", start_ms=0, end_ms=1000, document_id=1,
                       origin=None, source_id=6)
        with patch.dict(os.environ, {"KB_QUERY_PLAN_ENABLED": "1"}), \
                patch.object(query_plan, "plan_query", AsyncMock(return_value=self._plan())), \
                patch.object(kb_service, "search_knowledge", AsyncMock(return_value=[grounded])):
            async for kind, payload in chat_service.stream_answer(
                    session, question, user=self.user, model=ScriptedChatModel(rounds=script)):
                events.append((kind, payload))
        return events, session

    async def test_plan_is_injected_persisted_and_reported(self):
        events, session = await self._run()
        meta = [payload for kind, payload in events if kind == "meta"][0]
        self.assertEqual(meta["plan"]["intent"], "aggregation")
        done = [payload for kind, payload in events if kind == "done"][0]
        self.assertEqual(done["plan"]["rewritten"], "最近七天的调用成功率")
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        self.assertEqual(assistant.plan_json["intent"], "aggregation", "规划要落库，否则无法归因")
        messages = await chat_service.list_messages(session.id, self.user)
        self.assertEqual(messages[-1]["plan"]["intent"], "aggregation")

    async def test_entity_flag_is_a_warning_not_a_failed_turn(self):
        """实体核查只标注：有人名找不到时仍要有 done，stop_reason 仍是 finished。"""
        script = [AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "负责人"})]),
                  AIMessage(content="会上提到李婷负责这件事[1]。")]
        grounded = Hit(chunk_id=1, score=1.0, text="张晨负责这件事",
                       meeting_id=self.meeting.id, authority="RAW",
                       chunk_type="TRANSCRIPT_WINDOW", field=None, item_index=None,
                       speaker="张晨", start_ms=0, end_ms=1000, document_id=1,
                       origin=None, source_id=6)
        session = await chat_service.create_session(self.user)
        events = []
        with patch.object(kb_service, "search_knowledge", AsyncMock(return_value=[grounded])):
            async for kind, payload in chat_service.stream_answer(
                    session, "谁负责这件事", user=self.user,
                    model=ScriptedChatModel(rounds=script)):
                events.append((kind, payload))
        done = [payload for kind, payload in events if kind == "done"][0]
        self.assertTrue(done["entity_flags"])
        self.assertEqual(done["stop_reason"], "finished")
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        self.assertEqual(assistant.stop_reason, "finished")
        self.assertIn("李婷", assistant.error_message or "")

    async def test_plan_call_failure_does_not_break_the_turn(self):
        from services import query_plan
        script = [AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "x"})]),
                  AIMessage(content="答[1]。")]
        session = await chat_service.create_session(self.user)
        events = []
        with patch.dict(os.environ, {"KB_QUERY_PLAN_ENABLED": "1"}), \
                patch.object(query_plan, "plan_query",
                             AsyncMock(side_effect=RuntimeError("规划模型挂了"))), \
                patch.object(kb_service, "search_knowledge", AsyncMock(return_value=[])):
            async for kind, payload in chat_service.stream_answer(
                    session, "最近七天呢", user=self.user, model=ScriptedChatModel(rounds=script)):
                events.append((kind, payload))
        self.assertEqual(events[-1][0], "done", "规划失败不该把这一轮变成错误")
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        # 失败也留痕（不是 None）：复盘时要能分清"没开规划"与"开了但失败了"
        self.assertFalse(assistant.plan_json["used"])
        self.assertIn("规划失败", assistant.plan_json["reason"])

    async def test_plan_is_skipped_when_the_switch_is_off(self):
        from services import query_plan
        script = [AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "x"})]),
                  AIMessage(content="答[1]。")]
        session = await chat_service.create_session(self.user)
        events = []
        with patch.object(query_plan, "plan_enabled", return_value=False), \
                patch.object(query_plan, "plan_query", AsyncMock()) as planner, \
                patch.object(kb_service, "search_knowledge", AsyncMock(return_value=[])):
            async for kind, payload in chat_service.stream_answer(
                    session, "最近七天呢", user=self.user, model=ScriptedChatModel(rounds=script)):
                events.append((kind, payload))
        self.assertEqual(planner.await_count, 0, "开关关着就不许发生规划调用")
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        self.assertIsNone(assistant.plan_json)

    def test_cross_recording_prefetch_depth_is_configurable(self):
        with patch.dict(os.environ, {"CROSS_RECORDING_PREFETCH_K": "2"}):
            self.assertEqual(chat_service.cross_recording_prefetch_k(), 2)
        with patch.dict(os.environ, {"CROSS_RECORDING_PREFETCH_K": "99"}):
            self.assertEqual(chat_service.cross_recording_prefetch_k(), 5)
        with patch.dict(os.environ, {"CROSS_RECORDING_PREFETCH_K": "0"}):
            self.assertEqual(chat_service.cross_recording_prefetch_k(), 0)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CROSS_RECORDING_PREFETCH_K", None)
            self.assertEqual(chat_service.cross_recording_prefetch_k(), 0,
                             "cross8 标定后默认关")

    def test_cross_recording_markers(self):
        for question in ("两段录音分别提到了哪些风险？",
                         "发布公告与对外通稿分别由谁负责？",
                         "两个功能问题分别怎么解决？",
                         "发布时间的结论在两段录音里一致吗？"):
            self.assertTrue(chat_service.wants_cross_recording(question), question)
        self.assertFalse(chat_service.wants_cross_recording("这次灰度从什么时候开始？"))

    async def test_cross_recording_prefetch_registers_both_sources(self):
        first = await TranscriptionTask.create(
            meeting_id=self.meeting.id, material_id=92_001, model_config_id=1,
            initiator_id=self.user.id, status="SUCCEEDED",
            recorded_at=datetime(2026, 9, 10, 9, 0))
        second = await TranscriptionTask.create(
            meeting_id=self.meeting.id, material_id=92_002, model_config_id=1,
            initiator_id=self.user.id, status="SUCCEEDED",
            recorded_at=datetime(2026, 9, 10, 14, 0))
        actor = await chat_service.kb_tools.Actor.resolve(
            self.user, requested=[self.meeting.id])
        registry = chat_service.citations.CitationRegistry()

        limits: list[int] = []

        async def fake_search(query, **kwargs):
            source_id = kwargs["source_ids"][0]
            limits.append(kwargs["limit"])
            return [Hit(chunk_id=source_id, score=1.0, text=f"task {source_id} 的原文",
                        meeting_id=self.meeting.id, authority="RAW",
                        chunk_type="TRANSCRIPT_WINDOW", field=None, item_index=None,
                        speaker="张晨", start_ms=0, end_ms=1000, document_id=1,
                        origin=None, source_id=source_id)]

        with patch.dict(os.environ, {"CROSS_RECORDING_PREFETCH_K": "3"}), \
                patch.object(kb_service, "search_knowledge",
                             AsyncMock(side_effect=fake_search)) as search:
            message = await chat_service._cross_recording_message(
                actor, registry, "两段录音分别说了什么",
                scope=[self.meeting.id], biz_id=0)
        self.assertIsNotNone(message)
        self.assertIn(f"task {first.id}", message.content)
        self.assertIn(f"task {second.id}", message.content)
        self.assertIn("上午", message.content)
        self.assertIn("下午", message.content)
        self.assertEqual(search.await_count, 2)
        self.assertEqual(limits, [3, 3], "默认每段 3 条")
        self.assertEqual(len(registry), 2)

    async def test_cross_recording_prefetch_skips_non_cross_questions(self):
        actor = await chat_service.kb_tools.Actor.resolve(
            self.user, requested=[self.meeting.id])
        registry = chat_service.citations.CitationRegistry()
        with patch.dict(os.environ, {"CROSS_RECORDING_PREFETCH_K": "3"}), \
                patch.object(kb_service, "search_knowledge", AsyncMock()) as search:
            message = await chat_service._cross_recording_message(
                actor, registry, "灰度从什么时候开始",
                scope=[self.meeting.id], biz_id=0)
        self.assertIsNone(message)
        self.assertEqual(search.await_count, 0)


class CrossRecordingSupplementTest(unittest.IsolatedAsyncioTestCase):
    """条件补搜：只补缺的那段、等 Agent 先检索、走正式引用登记。"""

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="supp-t", password="x", name="张晨")
        self.meeting = await Meeting.create(
            meeting_no="MTG-S1", title="产品发布评审会议",
            start_time="2026-09-10 10:40:00", end_time="2026-09-10 13:00:00",
            creator_id=self.user.id, host_id=self.user.id, status="FINISHED")
        self.morning = await TranscriptionTask.create(
            meeting_id=self.meeting.id, material_id=93_001, model_config_id=1,
            initiator_id=self.user.id, status="SUCCEEDED",
            recorded_at=datetime(2026, 9, 10, 9, 0))
        self.afternoon = await TranscriptionTask.create(
            meeting_id=self.meeting.id, material_id=93_002, model_config_id=1,
            initiator_id=self.user.id, status="SUCCEEDED",
            recorded_at=datetime(2026, 9, 10, 14, 0))
        self.actor = await chat_service.kb_tools.Actor.resolve(
            self.user, requested=[self.meeting.id])
        self.registry = chat_service.citations.CitationRegistry()

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    def _hit(self, source_id: int) -> Hit:
        return Hit(chunk_id=100 + source_id, score=1.0, text=f"task {source_id} 的主题",
                   meeting_id=self.meeting.id, authority="RAW",
                   chunk_type="TRANSCRIPT_WINDOW", field=None, item_index=None,
                   speaker="张晨", start_ms=0, end_ms=5000, document_id=1,
                   origin=None, source_id=source_id)

    def _result(self, *source_ids: int) -> dict:
        return {"ok": True, "hits": [self._hit(source_id).as_citation()
                                     for source_id in source_ids]}

    async def _state(self, question: str = "两段录音分别说了什么"):
        with patch.dict(os.environ, {"CROSS_RECORDING_SUPPLEMENT_K": "1",
                                     "CROSS_RECORDING_PREFETCH_K": "0"}):
            return await chat_service._cross_recording_state(
                question, scope=[self.meeting.id])

    def test_depth_is_configurable(self):
        with patch.dict(os.environ, {"CROSS_RECORDING_SUPPLEMENT_K": "2"}):
            self.assertEqual(chat_service.cross_recording_supplement_k(), 2)
        with patch.dict(os.environ, {"CROSS_RECORDING_SUPPLEMENT_K": "99"}):
            self.assertEqual(chat_service.cross_recording_supplement_k(), 3)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CROSS_RECORDING_SUPPLEMENT_K", None)
            self.assertEqual(chat_service.cross_recording_supplement_k(), 0,
                             "标定前默认关")

    def test_strong_markers_exclude_bare_fenbie(self):
        for question in ("两段录音分别提到了哪些风险？",
                         "发布时间的结论在两段录音里一致吗？",
                         "灰度这件事在两段录音里分别讨论到什么？",
                         "上午和下午的会各自定了什么？"):
            self.assertTrue(chat_service.wants_cross_recording_supplement(question),
                            question)
        for question in ("切片和向量分别存在哪里？",
                         "这次会议的三个议题分别是什么？",
                         "两个功能问题分别怎么解决？",
                         "下午的会定了哪些行动项、分别由谁负责？"):
            self.assertFalse(chat_service.wants_cross_recording_supplement(question),
                             question)

    async def test_state_requires_two_successful_recordings(self):
        solo = await Meeting.create(
            meeting_no="MTG-S2", title="单段录音会",
            start_time="2026-09-10 10:00:00", end_time="2026-09-10 11:00:00",
            creator_id=self.user.id, host_id=self.user.id, status="FINISHED")
        await TranscriptionTask.create(
            meeting_id=solo.id, material_id=93_003, model_config_id=1,
            initiator_id=self.user.id, status="SUCCEEDED")
        with patch.dict(os.environ, {"CROSS_RECORDING_SUPPLEMENT_K": "1"}):
            self.assertIsNone(await chat_service._cross_recording_state(
                "两段录音分别说了什么", scope=[solo.id]))

    async def test_state_is_skipped_without_a_meeting_scope(self):
        with patch.dict(os.environ, {"CROSS_RECORDING_SUPPLEMENT_K": "1"}):
            self.assertIsNone(await chat_service._cross_recording_state(
                "两段录音分别说了什么", scope=[]))

    async def test_prefetch_disables_conditional_supplement(self):
        with patch.dict(os.environ, {"CROSS_RECORDING_SUPPLEMENT_K": "1",
                                     "CROSS_RECORDING_PREFETCH_K": "3"}):
            self.assertIsNone(await chat_service._cross_recording_state(
                "两段录音分别说了什么", scope=[self.meeting.id]))

    async def test_bare_fenbie_question_gets_no_state(self):
        with patch.dict(os.environ, {"CROSS_RECORDING_SUPPLEMENT_K": "1"}):
            self.assertIsNone(await chat_service._cross_recording_state(
                "两个功能问题分别怎么解决？", scope=[self.meeting.id]))

    async def test_waits_for_two_tool_results_before_supplementing(self):
        state = await self._state()
        with patch.object(kb_service, "search_knowledge",
                          AsyncMock(return_value=[self._hit(self.afternoon.id)])) as search:
            first = await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, self._result(self.morning.id))
            self.assertEqual(search.await_count, 0, "第一次工具结果后不抢跑")
            self.assertNotIn("coverage_hits", first)
            second = await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, {"ok": True, "facts": []})
        self.assertEqual(search.await_count, 1)
        kwargs = search.await_args.kwargs
        self.assertEqual(kwargs["source_ids"], [self.afternoon.id])
        self.assertEqual(kwargs["limit"], 1)
        self.assertEqual(kwargs["allowed_meeting_ids"], [self.meeting.id])
        self.assertEqual(second["coverage_supplement"]["searched_task_ids"],
                         [self.afternoon.id])
        self.assertEqual(len(second["coverage_hits"]), 1)
        self.assertIn("ref", second["coverage_hits"][0])
        self.assertIn("coverage_hits", second["notice"])

    async def test_zero_coverage_supplements_both_recordings(self):
        state = await self._state()

        async def fake_search(query, **kwargs):
            return [self._hit(kwargs["source_ids"][0])]

        with patch.object(kb_service, "search_knowledge",
                          AsyncMock(side_effect=fake_search)) as search:
            await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, {"ok": True, "hits": []})
            updated = await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, {"ok": True, "facts": []})
        self.assertEqual(search.await_count, 2)
        self.assertEqual([call.kwargs["source_ids"][0] for call in search.await_args_list],
                         [self.morning.id, self.afternoon.id])
        self.assertEqual(len(updated["coverage_hits"]), 2)

    async def test_full_coverage_does_not_supplement(self):
        state = await self._state()
        with patch.object(kb_service, "search_knowledge", AsyncMock()) as search:
            await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, self._result(self.morning.id))
            updated = await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, self._result(self.afternoon.id))
        self.assertEqual(search.await_count, 0)
        self.assertNotIn("coverage_hits", updated)

    async def test_excerpt_segments_count_as_transcript_coverage(self):
        state = await self._state()
        with patch.object(kb_service, "search_knowledge",
                          AsyncMock(return_value=[self._hit(self.afternoon.id)])) as search:
            await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry,
                {"ok": True, "task_id": self.morning.id,
                 "segments": [{"segment_no": 1, "text": "上午开场"}]})
            await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, {"ok": True, "facts": []})
        self.assertEqual(search.await_count, 1)
        self.assertEqual(search.await_args.kwargs["source_ids"], [self.afternoon.id])

    async def test_facts_and_minutes_chunks_are_not_transcript_coverage(self):
        state = await self._state()
        with patch.object(kb_service, "search_knowledge", AsyncMock()) as search:
            await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry,
                {"ok": True, "hits": [{"chunk_type": "MINUTES_ITEM", "source_id": 3,
                                       "quote": "[decisions] 发布公告"}]})
            await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, {"ok": True, "facts": []})
        self.assertEqual(search.await_count, 2, "纪要条目不能冒充转写覆盖")

    async def test_supplement_failure_does_not_break_the_tool_result(self):
        state = await self._state()
        with patch.object(kb_service, "search_knowledge",
                          AsyncMock(side_effect=RuntimeError("向量库不可达"))):
            await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, {"ok": True, "hits": []})
            updated = await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, {"ok": True, "facts": []})
        self.assertTrue(updated["ok"])
        self.assertNotIn("coverage_hits", updated)
        self.assertTrue(state.attempted)

    async def test_supplement_is_attempted_only_once_per_turn(self):
        state = await self._state()
        with patch.object(kb_service, "search_knowledge",
                          AsyncMock(return_value=[self._hit(self.afternoon.id)])) as search:
            await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, {"ok": True, "hits": []})
            await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, {"ok": True, "facts": []})
            await chat_service.maybe_supplement_cross_recording(
                state, self.actor, self.registry, {"ok": True, "facts": []})
        # 一轮只补一次：缺两段时一次补两段（2 次搜索），第三次工具结果不再触发。
        self.assertEqual(search.await_count, 2)
        self.assertTrue(state.attempted)

    async def test_tool_runner_puts_supplemental_hits_in_the_tool_message(self):
        state = await self._state()

        async def fake_execute(actor, name, arguments):
            if name == "list_facts":
                return {"ok": True, "kind": "recording", "facts": []}
            return {"ok": True, "fields": {}, "meeting": {"id": self.meeting.id}}

        async def fake_search(query, **kwargs):
            return [self._hit(kwargs["source_ids"][0])]

        tools = {tool.name: tool for tool in chat_service.build_tools(
            self.actor, self.registry, state)}
        with patch.object(chat_service.kb_tools, "execute_tool",
                          side_effect=fake_execute), \
                patch.object(kb_service, "search_knowledge",
                             AsyncMock(side_effect=fake_search)):
            await tools["list_facts"].coroutine(kind="recording")
            text = await tools["get_minutes"].coroutine(meeting_id=self.meeting.id)
        payload = json.loads(text)
        self.assertTrue(payload["coverage_hits"])
        self.assertIn("ref", payload["coverage_hits"][0])
        self.assertEqual(payload["coverage_supplement"]["searched_task_ids"],
                         [self.morning.id, self.afternoon.id])


class FailureDiagnosisTest(unittest.IsolatedAsyncioTestCase):
    """失败原因不许被换成另一句错诊断（实测踩过：中间失败的轮次被标成"没检索"）。

    真实链路：模型调了两次工具后，最后一次调用挂住 122 秒失败 → 图抛异常 →
    `final_state` 空 → `tool_calls` 读出来是 0 → 旧代码判"这一轮没有检索"，
    把"问答模型调用失败"这条真原因**覆盖**掉了。排查的人会照着"为什么没检索"查。
    """

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="diag-t", password="x", name="张晨")
        self.meeting = await Meeting.create(
            meeting_no="MTG-D1", title="产品发布评审会议",
            start_time="2026-09-10 10:40:00", end_time="2026-09-10 13:00:00",
            creator_id=self.user.id, host_id=self.user.id, status="FINISHED")
        await AiModelConfig.create(
            name="本地对话", model_type="AGENT", provider="OPENAI_COMPATIBLE",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="unit-test-key", model_name="qwen-test", enabled=True)

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_mid_turn_failure_keeps_its_own_reason(self):
        """工具跑过但最终调用失败：原因必须是失败本身，不能写成"没检索"。"""
        class DyingModel(ScriptedChatModel):
            async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
                if self.cursor == 0:
                    async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
                        yield chunk
                    return
                raise RuntimeError("No streaming chunk received for 120.0s")

        script = [AIMessage(content="", tool_calls=[_tool_call("search_knowledge", {"query": "灰度"})]),
                  AIMessage(content="永远到不了这里")]
        session = await chat_service.create_session(self.user)
        events = []
        with patch.object(kb_service, "search_knowledge", AsyncMock(return_value=[])):
            async for kind, payload in chat_service.stream_answer(
                    session, "灰度是什么", user=self.user,
                    model=DyingModel(rounds=script)):
                events.append((kind, payload))
        assistant = await ChatMessage.filter(session_id=session.id, role="ASSISTANT").first()
        self.assertNotEqual(assistant.stop_reason, chat_graph.STOP_NO_RETRIEVAL,
                            "中间失败不是「没有检索」")
        self.assertIn("调用失败", assistant.error_message or "")


class CitationResolutionTest(unittest.TestCase):
    """旧引用编号的落库解析：能从历史对上号的补回，补不回的才记 unknown。"""

    def test_old_refs_are_resolved_from_history_and_true_unknowns_kept(self):
        registry = chat_service.citations.CitationRegistry()
        registry.annotate("search_knowledge", {"ok": True, "hits": [{
            "chunk_id": 1, "meeting_id": 2, "quote": "甲", "meeting_title": "会",
            "start_second": 1, "end_second": 2, "authority": "RAW", "source_id": 6,
        }]})
        used, unknown = chat_service._resolve_unknown_refs(
            "答 [1][3][9]", registry,
            {3: {"ref": 3, "kind": "chunk", "meeting_id": 2, "quote": "乙"}})
        self.assertEqual([item["ref"] for item in used], [1, 3])
        self.assertEqual(unknown, [9])
