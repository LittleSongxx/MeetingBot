"""工具层的单测：权限收窄、参数校验、错误不抛三个不变量。

这组测试钉的不是"功能能跑"，而是**越权不可能发生**：
- 工具的会议范围参数只能收窄 Actor 的权限集合，不能扩大；
- 空权限集合必须原样传下去（空 ≠ 不过滤）；
- 参数错误与执行失败都必须是 `{"ok": false}`，不能让异常穿透到传输层。

模型与向量库全部打桩，零外部调用。
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from common.times import LOCAL_TZ
from models import (
    AiModelConfig,
    Meeting,
    MeetingMinutes,
    MeetingParticipant,
    TranscriptionTask,
    TranscriptSegment,
    User,
)
from pydantic import ValidationError
from services import kb_service, kb_tools
from services.kb_service import Hit
from tortoise import Tortoise


class NarrowScopeTest(unittest.TestCase):
    """范围收窄是纯函数，单独测：它是权限的最后一米。"""

    def test_admin_without_request_is_unfiltered(self):
        self.assertIsNone(kb_tools._narrow(None, None))

    def test_admin_with_request_is_limited_to_request(self):
        self.assertEqual(kb_tools._narrow(None, [3, 1, 3]), [1, 3])

    def test_employee_is_intersected_not_replaced(self):
        # 请求里混着无权访问的会议：只能取交集，不能把 99 带出去
        self.assertEqual(kb_tools._narrow([1, 2], [2, 99]), [2])

    def test_employee_requesting_nothing_is_unfiltered(self):
        self.assertEqual(kb_tools._narrow([1, 2], None), [1, 2])

    def test_empty_permission_stays_empty(self):
        # 空权限 + 任意请求 = 空；绝不能变成 None（那会变成"不过滤"）
        self.assertEqual(kb_tools._narrow([], [1, 2]), [])
        self.assertEqual(kb_tools._narrow([], None), [])


class ToolRegistryTest(unittest.TestCase):
    """工具数量与形状是架构承诺的一部分（3–5 个；schema 由参数模型派生）。"""

    def test_exactly_four_tools(self):
        self.assertEqual([spec.name for spec in kb_tools.TOOL_SPECS], [
            "search_knowledge", "list_facts", "get_minutes", "get_transcript_excerpt"])

    def test_openai_schema_is_derived_from_argument_model(self):
        tool = kb_tools.openai_tools()[0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["function"]["name"], "search_knowledge")
        parameters = tool["function"]["parameters"]
        self.assertIn("query", parameters["properties"])
        self.assertIn("query", parameters["required"])

    def test_unknown_tool_is_a_readable_error(self):
        import asyncio
        actor = kb_tools.Actor(user=User(id=1, role="ADMIN", username="a", password="x", name="a"),
                               allowed_meeting_ids=None)
        result = asyncio.run(kb_tools.execute_tool(actor, "drop_database", {}))
        self.assertFalse(result["ok"])
        self.assertIn("没有这个工具", result["error"])
        self.assertEqual(sorted(result["available"]), sorted(kb_tools.TOOLS_BY_NAME))

    def test_invalid_arguments_do_not_raise(self):
        import asyncio
        actor = kb_tools.Actor(user=User(id=1, role="ADMIN", username="a", password="x", name="a"),
                               allowed_meeting_ids=None)
        result = asyncio.run(kb_tools.execute_tool(
            actor, "search_knowledge", {"limit": 999, "query": "x"}))
        self.assertFalse(result["ok"])
        self.assertIn("参数校验失败", result["error"])


class LooseArgumentTest(unittest.TestCase):
    """参数被写成 JSON 字符串时要能接住：这是 q26/q27 两轮空答案的直接原因。

    实测链路：模型发 `meeting_ids="[2]"` → `StructuredTool` 的 args_schema 校验拒 →
    回给模型的错误文本不是 JSON → 模型原样重试三次 → 停滞闸掐断 → 答案为空。
    边界同样要钉住：宽松解析**不是**"参数怎么错都能过"。
    """

    def test_stringified_array_is_parsed(self):
        args = kb_tools.ListFactsArgs.model_validate(
            {"kind": "pending_item", "meeting_ids": "[2]"})
        self.assertEqual(args.meeting_ids, [2])

    def test_stringified_array_with_several_ids_is_parsed(self):
        args = kb_tools.ListFactsArgs.model_validate(
            {"kind": "decision", "meeting_ids": "[2, 7]"})
        self.assertEqual(args.meeting_ids, [2, 7])

    def test_stringified_authority_is_parsed(self):
        args = kb_tools.SearchKnowledgeArgs.model_validate(
            {"query": "上线时间", "authority": '["RAW"]'})
        self.assertEqual(args.authority, ["RAW"])

    def test_stringified_null_is_parsed_as_absent(self):
        args = kb_tools.TranscriptExcerptArgs.model_validate({"task_id": 1, "end_ms": "null"})
        self.assertIsNone(args.end_ms)

    def test_plain_string_is_still_a_reportable_error(self):
        with self.assertRaises(ValidationError):
            kb_tools.ListFactsArgs.model_validate(
                {"kind": "decision", "meeting_ids": "不是 JSON"})

    def test_json_of_the_wrong_type_is_still_a_reportable_error(self):
        # 解析成功但类型对不上：原样交回 pydantic，报常规的参数错误
        with self.assertRaises(ValidationError):
            kb_tools.ListFactsArgs.model_validate(
                {"kind": "decision", "meeting_ids": '{"a": 1}'})

    def test_boolean_is_not_a_number(self):
        # bool 是 int 的子类，`"true"` 一旦被当成 1，end_ms 会静默变成 1 毫秒
        with self.assertRaises(ValidationError):
            kb_tools.TranscriptExcerptArgs.model_validate({"task_id": "true"})

    def test_unknown_authority_value_is_still_rejected(self):
        with self.assertRaises(ValidationError):
            kb_tools.SearchKnowledgeArgs.model_validate(
                {"query": "x", "authority": '["GOLD"]'})

    def test_json_schema_still_describes_the_container_type(self):
        # 宽松解析不能把给模型看的 schema 改掉：模型看到的仍是数组（可空，故为 anyOf）
        parameters = kb_tools.TOOLS_BY_NAME["list_facts"].json_schema()
        variants = parameters["properties"]["meeting_ids"]["anyOf"]
        self.assertIn("array", [variant.get("type") for variant in variants])


class KbToolsDbTest(unittest.IsolatedAsyncioTestCase):
    """需要数据库的工具：sqlite 内存库 + 打桩的检索。"""

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.admin = await User.create(username="admin-t", password="x", name="管理员", role="ADMIN")
        self.owner = await User.create(username="owner-t", password="x", name="张晨")
        self.other = await User.create(username="other-t", password="x", name="李月")
        self.config = await AiModelConfig.create(
            name="本地嵌入", model_type="EMBEDDING", provider="OPENAI_COMPATIBLE",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="unit-test-key", model_name="text-embedding-v4", enabled=True)

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def _meeting(self, no: str, creator: User, *, host: User | None = None) -> Meeting:
        return await Meeting.create(
            meeting_no=no, title=f"会议{no}", start_time="2026-09-10 10:00:00",
            end_time="2026-09-10 11:00:00", creator_id=creator.id,
            host_id=(host or creator).id, status="FINISHED")

    async def _minutes(self, meeting: Meeting, creator: User, *, status: str = "CONFIRMED",
                       decisions=None, pending=None, risks=None) -> MeetingMinutes:
        self._material_seq = getattr(self, "_material_seq", 0) + 1
        task = await TranscriptionTask.create(
            meeting_id=meeting.id, material_id=10_000 + self._material_seq,
            model_config_id=self.config.id, initiator_id=creator.id, status="SUCCEEDED")
        return await MeetingMinutes.create(
            meeting_id=meeting.id, source_task_id=task.id, model_config_id=self.config.id,
            status=status, created_by=creator.id, summary="一句话摘要",
            decisions_json=decisions or [], pending_items_json=pending or [],
            risks_json=risks or [])

    def _decision(self, content: str, owner: str = "") -> dict:
        item = {"content": {"text": content, "basis": "stated"}, "origin": "stated",
                "evidence": [{"quote": content, "speaker": "张晨"}]}
        if owner:
            item["owner"] = {"text": owner, "basis": "stated", "evidence": []}
        return item

    # ------------------------------------------------------------------ #
    # search_knowledge
    # ------------------------------------------------------------------ #

    async def test_search_passes_permission_set_through_and_adds_titles(self):
        meeting = await self._meeting("MTG-1", self.owner)
        actor = await kb_tools.Actor.resolve(self.owner)
        hit = Hit(chunk_id=7, score=0.5, text="上线时间定在十月", meeting_id=meeting.id,
                  authority="RAW", chunk_type="TRANSCRIPT_WINDOW", field=None,
                  item_index=None, speaker="张晨", start_ms=12_000, end_ms=20_000,
                  document_id=1, origin=None)
        search = AsyncMock(return_value=[hit])
        with patch.object(kb_service, "search_knowledge", search):
            result = await kb_tools.execute_tool(actor, "search_knowledge", {"query": "上线时间"})
        self.assertTrue(result["ok"])
        self.assertEqual(search.await_args.kwargs["allowed_meeting_ids"], [meeting.id])
        self.assertEqual(result["hits"][0]["meeting_title"], "会议MTG-1")
        self.assertEqual(result["hits"][0]["start_second"], 12)
        self.assertEqual(result["hits"][0]["quote"], "上线时间定在十月")

    async def test_search_with_no_visible_meeting_returns_empty_scope(self):
        # 用户什么会议都看不到：集合必须是空列表（空 ≠ 不过滤）
        actor = await kb_tools.Actor.resolve(self.other)
        search = AsyncMock(return_value=[])
        with patch.object(kb_service, "search_knowledge", search):
            result = await kb_tools.execute_tool(actor, "search_knowledge", {"query": "x"})
        self.assertTrue(result["ok"])
        self.assertEqual(search.await_args.kwargs["allowed_meeting_ids"], [])
        self.assertEqual(result["scope_meeting_count"], 0)

    # ------------------------------------------------------------------ #
    # search_knowledge 的限定额（P1-5）
    # ------------------------------------------------------------------ #

    async def test_search_filters_reach_the_retrieval_layer(self):
        """会议/日期/类型限定要真的传到检索层：它们解决"下午这场会""最近七天"
        这类语义相似度碰不到的限定。"""
        meeting = await self._meeting("MTG-F1", self.owner)
        actor = await kb_tools.Actor.resolve(self.owner)
        search = AsyncMock(return_value=[])
        with patch.object(kb_service, "search_knowledge", search):
            result = await kb_tools.execute_tool(actor, "search_knowledge", {
                "query": "灰度", "meeting_ids": [meeting.id], "date_from": "2026-09-10",
                "date_to": "2026-09-11", "chunk_type": ["TRANSCRIPT_WINDOW"]})
        kwargs = search.await_args.kwargs
        self.assertEqual(kwargs["allowed_meeting_ids"], [meeting.id])
        self.assertEqual(kwargs["chunk_type"], ["TRANSCRIPT_WINDOW"])
        # 2026-09-10 00:00:00 与 2026-09-11 23:59:59（本地），日期按**整天**解释
        self.assertEqual(kwargs["meeting_time_from"],
                         int(datetime(2026, 9, 10, 0, 0, tzinfo=LOCAL_TZ).timestamp()))
        self.assertEqual(kwargs["meeting_time_to"],
                         int(datetime(2026, 9, 11, 23, 59, 59, tzinfo=LOCAL_TZ).timestamp()))
        self.assertTrue(result["ok"])
        self.assertIn("会议", "、".join(result["applied_filters"]))

    async def test_search_meeting_ids_only_narrow_the_visible_scope(self):
        theirs = await self._meeting("MTG-F2", self.other)
        actor = await kb_tools.Actor.resolve(self.owner)
        search = AsyncMock(return_value=[])
        with patch.object(kb_service, "search_knowledge", search):
            await kb_tools.execute_tool(actor, "search_knowledge",
                                        {"query": "x", "meeting_ids": [theirs.id]})
        self.assertEqual(search.await_args.kwargs["allowed_meeting_ids"], [])

    async def test_empty_result_explains_the_applied_filters(self):
        """过滤后空结果必须说明"为什么空"：否则模型会把它讲成"知识库里没有这件事"。"""
        actor = await kb_tools.Actor.resolve(self.owner)
        with patch.object(kb_service, "search_knowledge", AsyncMock(return_value=[])):
            result = await kb_tools.execute_tool(actor, "search_knowledge", {
                "query": "灰度", "date_from": "2026-09-10"})
        self.assertIn("没有命中", result["notice"])
        self.assertIn("2026-09-10", result["notice"])
        self.assertIn("放宽", result["notice"])

    async def test_search_rejects_a_bad_date_and_an_inverted_range(self):
        actor = await kb_tools.Actor.resolve(self.owner)
        bad = await kb_tools.execute_tool(actor, "search_knowledge",
                                          {"query": "x", "date_from": "上周"})
        self.assertFalse(bad["ok"])
        self.assertIn("时间格式", bad["error"])
        inverted = await kb_tools.execute_tool(actor, "search_knowledge", {
            "query": "x", "date_from": "2026-09-11", "date_to": "2026-09-10"})
        self.assertFalse(inverted["ok"])
        self.assertIn("date_to", inverted["error"])

    # ------------------------------------------------------------------ #
    # list_facts
    # ------------------------------------------------------------------ #

    async def test_list_facts_reads_confirmed_minutes_only(self):
        meeting = await self._meeting("MTG-2", self.owner)
        await self._minutes(meeting, self.owner, status="CONFIRMED",
                            decisions=[self._decision("上线时间定在十月")])
        await self._minutes(meeting, self.owner, status="DRAFT",
                            decisions=[self._decision("这是草稿里的猜测")])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "list_facts", {"kind": "decision"})
        self.assertTrue(result["ok"])
        contents = [fact["item"]["content"] for fact in result["facts"]]
        self.assertEqual(len(contents), 1)
        self.assertIn("上线时间定在十月", contents[0]["text"])
        self.assertEqual(result["facts"][0]["field"], "decisions")

    async def test_list_facts_requesting_foreign_meeting_returns_nothing(self):
        mine = await self._meeting("MTG-3", self.owner)
        theirs = await self._meeting("MTG-4", self.other)
        await self._minutes(theirs, self.other, decisions=[self._decision("别人的决策")])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(
            actor, "list_facts", {"kind": "decision", "meeting_ids": [theirs.id]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["scope_meeting_count"], 0)
        # 自己那场会没被请求，所以也不该出现
        self.assertNotIn(mine.id, [])

    async def test_list_facts_filters_by_owner(self):
        meeting = await self._meeting("MTG-5", self.owner)
        await self._minutes(meeting, self.owner, pending=[
            self._decision("补一份上线检查清单", owner="张晨"),
            self._decision("约一次回访", owner="李月"),
        ])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(
            actor, "list_facts", {"kind": "pending_item", "owner": "张晨"})
        self.assertEqual(len(result["facts"]), 1)
        self.assertIn("张晨", result["facts"][0]["item"]["owner_suggestion"])

    async def test_list_facts_accepts_a_stringified_meeting_ids(self):
        """q26/q27 的现场：参数里的数组被写成 JSON 字符串，整轮不能因此报错。"""
        meeting = await self._meeting("MTG-5B", self.owner)
        await self._minutes(meeting, self.owner, pending=[self._decision("下午的待办")])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(
            actor, "list_facts", {"kind": "pending_item",
                                  "meeting_ids": f"[{meeting.id}]"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["facts"]), 1)

    async def test_list_facts_meeting_kind_lists_visible_meetings(self):
        await self._meeting("MTG-6", self.owner)
        await self._meeting("MTG-7", self.other)
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "list_facts", {"kind": "meeting"})
        titles = [fact["title"] for fact in result["facts"]]
        self.assertEqual(titles, ["会议MTG-6"])

    async def test_list_facts_sees_participated_meetings(self):
        # 参会人也是可见：口径来自 common.access，工具层不另立一份
        meeting = await self._meeting("MTG-8", self.other)
        await MeetingParticipant.create(meeting_id=meeting.id, user_id=self.owner.id)
        await self._minutes(meeting, self.other, decisions=[self._decision("参会人看得到的决策")])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "list_facts", {"kind": "decision"})
        self.assertEqual(len(result["facts"]), 1)

    async def test_list_facts_says_why_nothing_was_found_when_only_drafts_exist(self):
        """查不到东西时要说明原因与下一步（q26/q27 的根因：模型不知道可以回退到转写）。

        草稿不算事实是**正确**的产品行为；缺陷是"空结果不解释"。
        """
        meeting = await self._meeting("MTG-8B", self.owner)
        await self._minutes(meeting, self.owner, status="DRAFT",
                            pending=[self._decision("下午定的待办")])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(
            actor, "list_facts", {"kind": "pending_item", "meeting_ids": [meeting.id]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["source_coverage"]["draft_minutes"], 1)
        self.assertIn("未确认", result["notice"])
        self.assertIn("search_knowledge", result["notice"])

    async def test_list_facts_reports_coverage_even_when_it_found_something(self):
        """查到东西时也要报"资料覆盖"：q26 的坑正是"查到 1 条就以为是全部"。

        已确认纪要在时 `facts` 非空，旧实现于是什么都不提示——模型把"确认纪要里只有
        1 条待办"讲成了"这场会只有 1 条待办"，而下午那段录音的待办还没确认。
        """
        meeting = await self._meeting("MTG-8D", self.owner)
        await self._minutes(meeting, self.owner, status="CONFIRMED",
                            pending=[self._decision("已确认的待办")])
        await self._minutes(meeting, self.owner, status="DRAFT",
                            pending=[self._decision("草稿里的待办")])
        await TranscriptionTask.create(
            meeting_id=meeting.id, material_id=88_002, model_config_id=self.config.id,
            initiator_id=self.owner.id, status="SUCCEEDED")
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "list_facts",
                                             {"kind": "pending_item", "meeting_ids": [meeting.id]})
        self.assertEqual(len(result["facts"]), 1)
        # `_minutes` 每建一份纪要都会带一个转写任务，所以这里是 2 + 1 = 3 段录音
        self.assertEqual(result["source_coverage"],
                         {"confirmed_minutes": 1, "draft_minutes": 1, "recordings": 3})
        self.assertIn("search_knowledge", result["notice"])

    async def test_list_facts_recording_lists_succeeded_transcriptions(self):
        meeting = await self._meeting("MTG-8C", self.owner)
        task = await TranscriptionTask.create(
            meeting_id=meeting.id, material_id=88_001, model_config_id=self.config.id,
            initiator_id=self.owner.id, status="SUCCEEDED", total_duration_ms=3_600_000)
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "list_facts", {"kind": "recording"})
        self.assertTrue(result["ok"])
        self.assertEqual([fact["task_id"] for fact in result["facts"]], [task.id])
        self.assertEqual(result["facts"][0]["duration_minutes"], 60.0)
        self.assertIn("task_id", result["notice"])

    # ------------------------------------------------------------------ #
    # get_minutes
    # ------------------------------------------------------------------ #

    async def test_get_minutes_prefers_confirmed_and_marks_authority(self):
        meeting = await self._meeting("MTG-9", self.owner)
        await self._minutes(meeting, self.owner, status="DRAFT",
                            decisions=[self._decision("草稿决策")])
        await self._minutes(meeting, self.owner, status="CONFIRMED",
                            decisions=[self._decision("确认决策")])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "get_minutes", {"meeting_id": meeting.id})
        self.assertTrue(result["ok"])
        self.assertEqual(result["authority"], "CONFIRMED")
        self.assertIn("确认决策", str(result["fields"]["decisions"]))

    async def test_get_minutes_falls_back_to_draft_with_label(self):
        meeting = await self._meeting("MTG-10", self.owner)
        await self._minutes(meeting, self.owner, status="DRAFT")
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "get_minutes", {"meeting_id": meeting.id})
        self.assertTrue(result["ok"])
        self.assertEqual(result["authority"], "DRAFT")
        self.assertIn("草稿", result["notice"])

    async def test_get_minutes_of_foreign_meeting_is_refused(self):
        meeting = await self._meeting("MTG-11", self.other)
        minutes = await self._minutes(meeting, self.other)
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "get_minutes", {"minutes_id": minutes.id})
        self.assertFalse(result["ok"])
        self.assertIn("没有这场会议的访问权限", result["error"])

    async def test_get_minutes_requires_one_identifier(self):
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "get_minutes", {})
        self.assertFalse(result["ok"])
        self.assertIn("至少要给一个", result["error"])

    # ------------------------------------------------------------------ #
    # get_transcript_excerpt
    # ------------------------------------------------------------------ #

    async def _transcript(self, meeting: Meeting, user: User) -> TranscriptionTask:
        task = await TranscriptionTask.create(
            meeting_id=meeting.id, material_id=99_000 + meeting.id,
            model_config_id=self.config.id, initiator_id=user.id, status="SUCCEEDED")
        await TranscriptSegment.bulk_create([
            TranscriptSegment(task_id=task.id, meeting_id=meeting.id, segment_no=i,
                              start_ms=i * 5000, end_ms=i * 5000 + 4900,
                              speaker_user_id=user.id, speaker_label="01-0",
                              content=f"第{i}句原话", original_text=f"第{i}句原话")
            for i in range(5)])
        return task

    async def test_excerpt_returns_window_with_seconds_and_speaker(self):
        meeting = await self._meeting("MTG-12", self.owner)
        task = await self._transcript(meeting, self.owner)
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "get_transcript_excerpt", {
            "task_id": task.id, "start_ms": 5000, "end_ms": 10_000})
        self.assertTrue(result["ok"])
        self.assertEqual([s["segment_no"] for s in result["segments"]], [1, 2])
        self.assertEqual(result["segments"][0]["start_second"], 5)
        self.assertEqual(result["segments"][0]["speaker"], "张晨")
        self.assertFalse(result["truncated"])

    async def test_excerpt_of_foreign_transcript_is_refused(self):
        meeting = await self._meeting("MTG-13", self.other)
        task = await self._transcript(meeting, self.other)
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "get_transcript_excerpt", {"task_id": task.id})
        self.assertFalse(result["ok"])
        self.assertIn("没有这场会议的访问权限", result["error"])

    async def test_excerpt_rejects_inverted_window(self):
        meeting = await self._meeting("MTG-14", self.owner)
        task = await self._transcript(meeting, self.owner)
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "get_transcript_excerpt", {
            "task_id": task.id, "start_ms": 10_000, "end_ms": 5000})
        self.assertFalse(result["ok"])
        self.assertIn("end_ms", result["error"])

    async def test_excerpt_truncates_and_says_so(self):
        meeting = await self._meeting("MTG-15", self.owner)
        task = await self._transcript(meeting, self.owner)
        actor = await kb_tools.Actor.resolve(self.owner)
        with patch.object(kb_tools, "MAX_EXCERPT_SEGMENTS", 2):
            result = await kb_tools.execute_tool(actor, "get_transcript_excerpt",
                                                 {"task_id": task.id})
        self.assertEqual(len(result["segments"]), 2)
        self.assertTrue(result["truncated"])


class AdminScopeTest(unittest.IsolatedAsyncioTestCase):
    """管理员：Actor.allowed 为 None（不过滤），工具参数才决定范围。"""

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.admin = await User.create(username="root-t", password="x", name="管理员", role="ADMIN")
        self.employee = await User.create(username="emp-t", password="x", name="员工")
        self.meeting = await Meeting.create(
            meeting_no="MTG-A1", title="管理员的会", start_time="2026-09-10 10:00:00",
            end_time="2026-09-10 11:00:00", creator_id=self.employee.id,
            host_id=self.employee.id, status="FINISHED")

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_admin_sees_every_meeting(self):
        actor = await kb_tools.Actor.resolve(self.admin)
        self.assertIsNone(actor.allowed_meeting_ids)
        result = await kb_tools.execute_tool(actor, "list_facts", {"kind": "meeting"})
        self.assertEqual([fact["meeting_id"] for fact in result["facts"]], [self.meeting.id])
        self.assertIsNone(result["scope_meeting_count"])

    async def test_actor_resolve_honours_requested_scope(self):
        actor = await kb_tools.Actor.resolve(self.admin, requested=[self.meeting.id])
        self.assertEqual(actor.allowed_meeting_ids, [self.meeting.id])


if __name__ == "__main__":
    unittest.main()
