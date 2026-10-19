"""系列归类与状态工具的真实链路单测（sqlite，零模型调用）。"""

from __future__ import annotations

import unittest
from datetime import datetime

from common.exception_handler import CustomException
from models import ActionItem, Meeting, MeetingMemory, MeetingSeries, User
from services import kb_tools, series_service
from tortoise import Tortoise


class SeriesAssignTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]})
        await Tortoise.generate_schemas()
        self.admin = await User.create(username="admin", password="x", name="管理员", role="ADMIN")
        self.employee = await User.create(username="emp", password="x", name="员工", role="EMPLOYEE")
        self.series = await MeetingSeries.create(name="周会", owner_id=self.admin.id)
        self.meeting = await Meeting.create(
            meeting_no="M1", title="周会 1",
            start_time=datetime(2026, 10, 1, 9), end_time=datetime(2026, 10, 1, 10),
            creator_id=self.admin.id, host_id=self.admin.id, status="FINISHED")

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_admin_can_assign_finished_meeting(self):
        result = await series_service.assign_meeting(self.admin, self.meeting.id, self.series.id)
        self.assertEqual(result["series_id"], self.series.id)
        self.assertEqual((await Meeting.get(id=self.meeting.id)).series_id, self.series.id)
        # 传 None 移出系列
        await series_service.assign_meeting(self.admin, self.meeting.id, None)
        self.assertIsNone((await Meeting.get(id=self.meeting.id)).series_id)

    async def test_other_employee_cannot_assign(self):
        with self.assertRaises(CustomException):
            await series_service.assign_meeting(self.employee, self.meeting.id, self.series.id)

    async def test_state_tool_only_returns_confirmed(self):
        await MeetingMemory.create(
            scope_type="SERIES", scope_id=self.series.id, kind="DECISION",
            content="9月1日发布", status="CONFIRMED", source_type="MANUAL",
            source_meeting_id=self.meeting.id, created_by=self.admin.id)
        await MeetingMemory.create(
            scope_type="SERIES", scope_id=self.series.id, kind="DECISION",
            content="未确认候选", status="PROPOSED", source_type="MANUAL",
            source_meeting_id=self.meeting.id, created_by=self.admin.id)
        await ActionItem.create(
            series_id=self.series.id, meeting_id=self.meeting.id,
            content="修复附件下载", status="OPEN", created_by=self.admin.id)
        actor = await kb_tools.Actor.resolve(self.admin)
        memories = await kb_tools.execute_tool(
            actor, "query_meeting_state", {"kind": "decision"})
        actions = await kb_tools.execute_tool(
            actor, "query_meeting_state", {"kind": "action_item"})
        self.assertTrue(memories["ok"])
        self.assertEqual([item["content"] for item in memories["items"]], ["9月1日发布"])
        self.assertTrue(actions["ok"])
        self.assertEqual(actions["items"][0]["content"], "修复附件下载")
