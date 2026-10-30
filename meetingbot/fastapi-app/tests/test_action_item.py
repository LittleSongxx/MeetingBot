import unittest

from api.action_item import (
    ActionAddPayload,
    ActionStatusPayload,
    ActionUpdatePayload,
    add,
    confirm_batch,
    generate,
    update,
    update_status,
)
from common.exception_handler import CustomException
from models import ActionItem, Meeting, MeetingMinutes, User
from services.action_item_service import parse_due_date, split_owner_names
from tortoise import Tortoise


class ActionItemTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=False,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.owner = await User.create(username="action_owner", password="123456", name="张三", role="EMPLOYEE")
        self.outsider = await User.create(username="action_outsider", password="123456", name="李四", role="EMPLOYEE")
        self.admin = await User.create(username="action_admin", password="123456", name="管理员", role="ADMIN")
        self.meeting = await Meeting.create(
            meeting_no="MTG-ACTION-001",
            title="行动项验证会",
            start_time="2026-08-27 09:00:00",
            end_time="2026-08-27 10:00:00",
            creator_id=self.owner.id,
            host_id=self.owner.id,
        )
        self.minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id,
            source_task_id=1,
            model_config_id=1,
            status="CONFIRMED",
            summary="确认了行动项",
            created_by=self.owner.id,
            pending_items_json=[
                {"content": "评估队列方案", "owner_suggestion": "张三", "deadline_suggestion": "2026-09-10"},
                {"content": "前端任务列表联调", "owner_suggestion": "王五、张三", "deadline_suggestion": ""},
            ],
            decisions_json=[
                {"content": "导出改异步", "basis": "压测", "owner_suggestion": "张三", "deadline_suggestion": "2026-09-12"},
                {"content": "其它无主决策", "basis": "讨论", "owner_suggestion": "", "deadline_suggestion": ""},
            ],
        )

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_generate_is_idempotent_and_skips_non_actionable_decisions(self):
        first = await generate(self.minutes.id, self.owner)
        self.assertEqual(first.data, {"created": 3, "skipped": 0})
        second = await generate(self.minutes.id, self.owner)
        self.assertEqual(second.data, {"created": 0, "skipped": 3})
        self.assertEqual(await ActionItem.all().count(), 3)
        item = await ActionItem.get(source_field="pending_items", source_index=0)
        self.assertEqual(item.owner_user_id, self.owner.id)
        self.assertEqual(item.due_date.isoformat(), "2026-09-10")
        self.assertEqual(item.status, "DRAFT")
        # 多姓名/重名不强行绑定用户
        multi = await ActionItem.get(source_field="pending_items", source_index=1)
        self.assertIsNone(multi.owner_user_id)

    async def test_permission_and_status_flow(self):
        with self.assertRaises(CustomException):
            await generate(self.minutes.id, self.outsider)
        await generate(self.minutes.id, self.owner)
        items = await ActionItem.all()
        with self.assertRaises(CustomException):
            await confirm_batch([item.id for item in items], self.outsider)
        confirmed = await confirm_batch([item.id for item in items], self.owner)
        self.assertEqual(confirmed.data["confirmed"], 3)
        self.assertEqual(await ActionItem.filter(status="OPEN").count(), 3)
        item = await ActionItem.get(source_field="pending_items", source_index=0)
        await update_status(item.id, ActionStatusPayload(status="DONE"), self.owner)
        self.assertEqual((await ActionItem.get(id=item.id)).status, "DONE")
        await update_status(item.id, ActionStatusPayload(status="CANCELLED"), self.owner)
        self.assertEqual((await ActionItem.get(id=item.id)).status, "CANCELLED")
        await update_status(item.id, ActionStatusPayload(status="OPEN"), self.owner)
        self.assertEqual((await ActionItem.get(id=item.id)).status, "OPEN")
        updated = await update(
            ActionUpdatePayload(id=item.id, content="改过的行动项", due_text="下周一"), self.owner
        )
        self.assertEqual(updated.code, "200")
        self.assertEqual((await ActionItem.get(id=item.id)).content, "改过的行动项")

    async def test_owner_clear_and_draft_cancel(self):
        await generate(self.minutes.id, self.owner)
        item = await ActionItem.get(source_field="pending_items", source_index=0)
        await update(ActionUpdatePayload(id=item.id, owner_user_id=self.owner.id), self.owner)
        self.assertEqual((await ActionItem.get(id=item.id)).owner_name, "张三")
        await update(ActionUpdatePayload(id=item.id, owner_user_id=None), self.owner)
        reloaded = await ActionItem.get(id=item.id)
        self.assertIsNone(reloaded.owner_user_id)
        self.assertIsNone(reloaded.owner_name)
        await update_status(item.id, ActionStatusPayload(status="CANCELLED"), self.owner)
        self.assertEqual((await ActionItem.get(id=item.id)).status, "CANCELLED")

    async def test_duplicate_pending_and_decision_only_create_once(self):
        minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id,
            source_task_id=2,
            model_config_id=1,
            status="CONFIRMED",
            summary="重复条目验证",
            created_by=self.owner.id,
            pending_items_json=[
                {"content": "导出改为异步导出", "owner_suggestion": "张三", "deadline_suggestion": ""},
            ],
            decisions_json=[
                {"content": "导出改为异步导出", "basis": "压测", "owner_suggestion": "张三", "deadline_suggestion": ""},
            ],
        )
        result = await generate(minutes.id, self.owner)
        self.assertEqual(result.data, {"created": 1, "skipped": 0})
        self.assertEqual(await ActionItem.filter(minutes_id=minutes.id).count(), 1)

    async def test_generate_requires_confirmed_and_meeting_delete_cascades(self):
        self.minutes.status = "DRAFT"
        await self.minutes.save()
        with self.assertRaises(CustomException):
            await generate(self.minutes.id, self.owner)
        self.minutes.status = "CONFIRMED"
        await self.minutes.save()
        await generate(self.minutes.id, self.owner)
        self.assertEqual(await ActionItem.all().count(), 3)
        from api.meeting import delete as delete_meeting
        await delete_meeting(self.meeting.id, self.admin)
        self.assertEqual(await ActionItem.all().count(), 0)

    async def test_whitespace_content_is_rejected(self):
        with self.assertRaises(CustomException):
            await add(ActionAddPayload(meeting_id=self.meeting.id, content="   "), self.owner)
        await generate(self.minutes.id, self.owner)
        item = await ActionItem.get(source_field="pending_items", source_index=0)
        with self.assertRaises(CustomException):
            await update(ActionUpdatePayload(id=item.id, content="  "), self.owner)
        self.assertNotEqual((await ActionItem.get(id=item.id)).content, "")

    async def test_delete_cascade_with_minutes(self):
        from api.meeting_minutes import delete_minutes

        await generate(self.minutes.id, self.owner)
        self.assertEqual(await ActionItem.all().count(), 3)
        await delete_minutes(self.minutes, self.owner)
        self.assertEqual(await ActionItem.all().count(), 0)

    def test_parse_helpers(self):
        self.assertEqual(str(parse_due_date("2026年9月10日")), "2026-09-10")
        self.assertEqual(str(parse_due_date("2026/09/10")), "2026-09-10")
        self.assertIsNone(parse_due_date("下次评审同步"))
        self.assertEqual(split_owner_names("张涛、李悦"), ["张涛", "李悦"])
        self.assertEqual(split_owner_names("张涛（后端）"), ["张涛"])


if __name__ == "__main__":
    unittest.main()
