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
        # 时间限定空结果会**自动放宽重试一次**，所以第一次调用才是带限定的那次
        kwargs = search.await_args_list[0].kwargs
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
                "query": "灰度", "chunk_type": ["MINUTES_ITEM"]})
        self.assertIn("没有命中", result["notice"])
        self.assertIn("MINUTES_ITEM", result["notice"])
        self.assertIn("放宽", result["notice"])

    async def test_time_filter_miss_retries_without_the_time_limit(self):
        """时间限定是**模型猜的**：实测它把 2026 年的会猜成 2024 年，一次猜错就把
        正确内容全挡在外面（那一轮的 3 条金标事实一条没检索到）。

        所以空结果时不只提示"你可以放宽"——自动去掉时间限定再检索一次，
        把结果与说明一起交出去。结论仍由模型下，它只是不再因为一次猜错而盲。
        """
        meeting = await self._meeting("MTG-F3", self.owner)
        actor = await kb_tools.Actor.resolve(self.owner)
        hit = kb_service.Hit(chunk_id=11, score=0.5, text="最近的调用成功率 98.6%", meeting_id=meeting.id,
                             authority="RAW", chunk_type="TRANSCRIPT_WINDOW", field=None,
                             item_index=None, speaker="张晨", start_ms=170_000, end_ms=180_000,
                             document_id=1, origin=None)
        calls: list[dict] = []

        async def fake_search(query, **kwargs):
            calls.append(kwargs)
            if kwargs.get("meeting_time_from") or kwargs.get("meeting_time_to"):
                return []
            return [hit]

        with patch.object(kb_service, "search_knowledge", AsyncMock(side_effect=fake_search)):
            result = await kb_tools.execute_tool(actor, "search_knowledge", {
                "query": "最近的调用成功率", "date_from": "2024-08-20", "date_to": "2024-08-26"})
        self.assertEqual(len(calls), 2, "带时间限定一次 + 放宽后一次")
        self.assertIsNone(calls[1].get("meeting_time_from"), "放宽时不再传时间限定")
        self.assertTrue(result["relaxed_filters"])
        self.assertEqual(len(result["hits"]), 1)
        self.assertIn("去掉时间与来源限定重试", result["notice"])
        self.assertIn("与实际情况不符", result["notice"])

    async def test_source_filter_miss_also_retries_without_it(self):
        """来源限定也是**模型猜的**（实测：它把"下午这场会"猜成某一段录音，
        而那场会两段录音的时间戳都是上午，元数据判不出"下午"）——空了同样要放宽。"""
        meeting = await self._meeting("MTG-F4", self.owner)
        actor = await kb_tools.Actor.resolve(self.owner)
        hit = kb_service.Hit(chunk_id=12, score=0.5, text="张涛负责报表导出异步化改造",
                             meeting_id=meeting.id, authority="RAW",
                             chunk_type="TRANSCRIPT_WINDOW", field=None, item_index=None,
                             speaker="系统管理员", start_ms=245_000, end_ms=260_000,
                             document_id=1, origin=None, source_id=7)
        calls: list[dict] = []

        async def fake_search(query, **kwargs):
            calls.append(kwargs)
            return [] if kwargs.get("source_ids") else [hit]

        with patch.object(kb_service, "search_knowledge", AsyncMock(side_effect=fake_search)):
            result = await kb_tools.execute_tool(actor, "search_knowledge", {
                "query": "下午这场会的行动项", "source_ids": [6]})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["source_types"], ("TRANSCRIPT",),
                         "source_ids 必须同时限定来源类型，避免同号纪要混入")
        self.assertIsNone(calls[1].get("source_ids"), "放宽时不再按来源收窄")
        self.assertIsNone(calls[1].get("source_types"), "放宽时不再按来源类型收窄")
        self.assertEqual(len(result["hits"]), 1)
        self.assertIn("去掉时间与来源限定重试", result["notice"])

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

    async def test_decision_coverage_does_not_nudge_extra_transcript_search(self):
        """决策复述题不该被“覆盖”提示带去做额外检索（任务 4a 的 q23）。"""
        meeting = await self._meeting("MTG-8E", self.owner)
        await self._minutes(meeting, self.owner, status="CONFIRMED",
                            decisions=[self._decision("已确认的决策")])
        await self._minutes(meeting, self.owner, status="DRAFT",
                            decisions=[self._decision("草稿里的决策")])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "list_facts",
                                             {"kind": "decision", "meeting_ids": [meeting.id]})
        self.assertIn("未确认", result["notice"])
        self.assertIn("不要仅仅为了覆盖", result["notice"])
        self.assertNotIn("必须再用 search_knowledge", result["notice"])

    async def test_list_facts_recording_lists_succeeded_transcriptions(self):
        meeting = await self._meeting("MTG-8C", self.owner)
        task = await TranscriptionTask.create(
            meeting_id=meeting.id, material_id=88_001, model_config_id=self.config.id,
            initiator_id=self.owner.id, status="SUCCEEDED", total_duration_ms=3_600_000,
            recorded_at=datetime(2026, 9, 10, 14, 0))
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "list_facts", {"kind": "recording"})
        self.assertTrue(result["ok"])
        self.assertEqual([fact["task_id"] for fact in result["facts"]], [task.id])
        self.assertEqual(result["facts"][0]["duration_minutes"], 60.0)
        self.assertEqual(result["facts"][0]["period"], "下午")
        self.assertIn("recorded_at", result["notice"])

    def test_period_of_recordings(self):
        self.assertEqual(kb_tools._period_of(datetime(2026, 9, 10, 9, 0)), "上午")
        self.assertEqual(kb_tools._period_of(datetime(2026, 9, 10, 14, 0)), "下午")
        self.assertEqual(kb_tools._period_of(datetime(2026, 9, 10, 19, 0)), "晚上")
        self.assertIsNone(kb_tools._period_of(None))

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

    async def test_get_minutes_fields_filter_narrows_the_context(self):
        """问待办时只取 pending_items，不要把决策/风险/议题一起塞进上下文。"""
        meeting = await self._meeting("MTG-9F", self.owner)
        await self._minutes(meeting, self.owner, status="CONFIRMED",
                            decisions=[self._decision("不该出现的决策")],
                            pending=[self._decision("只应出现的待办")])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "get_minutes", {
            "meeting_id": meeting.id, "fields": ["pending_items"]})
        self.assertTrue(result["ok"])
        self.assertEqual(list(result["fields"]), ["pending_items"])
        self.assertNotIn("decisions", result["fields"])
        self.assertIn("只应出现的待办", str(result["fields"]["pending_items"]))

    async def test_get_minutes_keywords_and_limit_keep_original_index(self):
        """q60 的现场：整字段返回 8–16 条，模型只问其中一两条。

        keywords/limit 是**工具侧**的收窄口；item_index 必须保留原始下标，
        否则过滤后的引用会指到另一条纪要条目。
        """
        meeting = await self._meeting("MTG-9G", self.owner)
        decisions = [self._decision(f"普通决策{i}") for i in range(8)]
        decisions[6]["content"]["text"] = "发布范围包含会议创建与知识库"
        decisions[7]["content"]["text"] = "发布范围包含任务跟踪"
        await self._minutes(meeting, self.owner, status="CONFIRMED", decisions=decisions)
        actor = await kb_tools.Actor.resolve(self.owner)

        first = await kb_tools.execute_tool(actor, "get_minutes", {
            "meeting_id": meeting.id, "fields": ["decisions"],
            "keywords": ["发布范围"], "limit": 1})
        self.assertTrue(first["ok"])
        self.assertEqual(first["field_meta"]["decisions"],
                         {"total": 2, "returned": 1, "truncated": True})
        self.assertEqual(first["fields"]["decisions"][0]["item_index"], 6)
        self.assertIn("发布范围", str(first["fields"]["decisions"][0]))

    async def test_get_minutes_without_limit_returns_all_items(self):
        """默认不截断：q62 的“最后四条”不能用数组顺序猜，先让模型看到全部。"""
        meeting = await self._meeting("MTG-9H", self.owner)
        await self._minutes(meeting, self.owner, status="CONFIRMED",
                            decisions=[self._decision(f"决策{i}") for i in range(8)])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "get_minutes", {
            "meeting_id": meeting.id, "fields": ["decisions"]})
        self.assertEqual(len(result["fields"]["decisions"]), 8)
        self.assertEqual(result["field_meta"]["decisions"],
                         {"total": 8, "returned": 8, "truncated": False})
        self.assertIn("数组顺序", result["notice"])

    async def test_list_facts_task_kind_merges_decisions_and_pending(self):
        """q42：只查 pending_item 会漏掉写在 decisions 里的任务；task 两类一起查。"""
        meeting = await self._meeting("MTG-9T", self.owner)
        await self._minutes(
            meeting, self.owner, status="CONFIRMED",
            decisions=[self._decision("发布决策复盘", owner="张晨")],
            pending=[self._decision("提交排班表", owner="张晨")])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "list_facts", {
            "kind": "task", "owner": "张晨", "meeting_ids": [meeting.id]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "task")
        fields = {fact["field"] for fact in result["facts"]}
        self.assertEqual(fields, {"decisions", "pending_items"})

    async def test_list_facts_keywords_narrow_the_facts(self):
        meeting = await self._meeting("MTG-9K", self.owner)
        await self._minutes(meeting, self.owner, status="CONFIRMED",
                            pending=[self._decision("报表导出异步化改造"),
                                     self._decision("灰度值班排班表")])
        actor = await kb_tools.Actor.resolve(self.owner)
        result = await kb_tools.execute_tool(actor, "list_facts", {
            "kind": "pending_item", "meeting_ids": [meeting.id],
            "keywords": ["报表"]})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["facts"]), 1)
        self.assertIn("报表", str(result["facts"][0]))

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

    async def test_excerpt_budget_is_per_turn_not_per_call(self):
        """单次上限管不住多次调用：同一轮第二次调用必须看到预算用尽。"""
        meeting = await self._meeting("MTG-15B", self.owner)
        task = await self._transcript(meeting, self.owner)
        actor = await kb_tools.Actor.resolve(self.owner)
        with patch.object(kb_tools, "excerpt_segments_per_turn", return_value=3):
            first = await kb_tools.execute_tool(actor, "get_transcript_excerpt",
                                                {"task_id": task.id})
            second = await kb_tools.execute_tool(actor, "get_transcript_excerpt",
                                                 {"task_id": task.id})
        self.assertEqual(len(first["segments"]), 3)
        self.assertEqual(second["segments"], [])
        self.assertIn("预算已用完", second["notice"])
        # 新一轮（新 Actor）预算重置
        fresh = await kb_tools.Actor.resolve(self.owner)
        with patch.object(kb_tools, "excerpt_segments_per_turn", return_value=3):
            third = await kb_tools.execute_tool(fresh, "get_transcript_excerpt",
                                                {"task_id": task.id})
        self.assertEqual(len(third["segments"]), 3)


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
