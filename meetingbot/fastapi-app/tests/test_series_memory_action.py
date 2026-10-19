"""会议系列 / 长期记忆 / 待办 / 状态查询的回归测试（sqlite 内存库 + ASGI 直连，零模型调用）。

锁定 P0 的几条产品不变量，它们出错不会报错、只会悄悄越权或给出过期事实：

- **系列权限**：可见=管理员/负责人/系列下有可见会议；编辑=负责人/管理员；建议只读
- **记忆**：候选只从已确认纪要的 decisions 生成；确认冲突可替代；只有 CONFIRMED 进工具层
- **待办**：从已确认纪要生成；同系列判重→结转而非重复建；状态变更留事件
- **纪要确认钩子**：确认后生成、取消/删除后撤回候选；幂等；失败只记日志不阻断
"""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
from common.auth import create_token
from common.exception_handler import setup_exceptions
from common.times import now
from fastapi import FastAPI
from models import (
    ActionItem,
    ActionItemEvent,
    Meeting,
    MeetingMemory,
    MeetingMinutes,
    MeetingParticipant,
    MeetingSeries,
    User,
)
from services import action_item_service, memory_service, state_service
from tortoise import Tortoise

TZ = "Asia/Shanghai"


class DomainBase(unittest.IsolatedAsyncioTestCase):
    """内存库 + 真实 ASGI 往返：路由、鉴权、事务与序列化都按生产路径走。"""

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone=TZ)
        await Tortoise.generate_schemas()
        self.admin = await User.create(
            username="admin", password="x", name="管理员", role="ADMIN")
        self.owner = await User.create(username="owner", password="x", name="李月")
        self.member = await User.create(username="member", password="x", name="张晨")
        self.outsider = await User.create(username="outsider", password="x", name="王工")
        app = FastAPI()
        from api import action_item, meeting, meeting_minutes, memory, series
        for module in (series, memory, action_item, meeting, meeting_minutes):
            app.include_router(module.router)
        setup_exceptions(app)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test")
        self._tokens: dict[int, str] = {}

    async def asyncTearDown(self):
        await self.client.aclose()
        await Tortoise.close_connections()

    def auth(self, user: User) -> dict:
        if user.id not in self._tokens:
            self._tokens[user.id] = create_token(user)
        return {"token": self._tokens[user.id]}

    async def make_series(self, *, name: str = "产品发布评审系列", owner: User | None = None,
                          status: str = "ACTIVE") -> MeetingSeries:
        return await MeetingSeries.create(
            name=name, description="系列说明", cadence="IRREGULAR",
            owner_id=(owner or self.owner).id, status=status)

    async def make_meeting(self, *, title: str = "产品发布评审会议",
                           creator: User | None = None, host: User | None = None,
                           participants: tuple[User, ...] = (), series_id: int | None = None,
                           status: str = "FINISHED", day: int = 10) -> Meeting:
        creator = creator or self.owner
        host = host or self.owner
        start = datetime(2026, 9, day, 10, 40)
        end = datetime(2026, 9, day, 13, 0)
        meeting = await Meeting.create(
            meeting_no=f"MTG-{uuid4().hex[:10]}", title=title, start_time=start,
            end_time=end, creator_id=creator.id, host_id=host.id, status=status,
            series_id=series_id)
        for user in {creator, host, *participants}:
            await MeetingParticipant.create(meeting_id=meeting.id, user_id=user.id)
        return meeting

    async def make_minutes(self, meeting: Meeting, *, decisions=None, pending_items=None,
                           status: str = "CONFIRMED", created_by: User | None = None,
                           confirmed_by: User | None = None) -> MeetingMinutes:
        row = getattr(self, "_task_seq", 0) + 1
        self._task_seq = row
        creator = created_by or self.owner
        confirmed = status == "CONFIRMED"
        return await MeetingMinutes.create(
            meeting_id=meeting.id, source_task_id=row, model_config_id=1, status=status,
            summary="会议摘要", decisions_json=decisions or [],
            pending_items_json=pending_items or [], created_by=creator.id,
            confirmed_by=(confirmed_by or creator).id if confirmed else None,
            confirmed_time=now() if confirmed else None)

    async def api(self, method: str, path: str, user: User, *, params=None, json=None):
        response = await self.client.request(
            method, path, params=params, json=json, headers=self.auth(user))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def decision(content: str, *, quote: str = "", speaker: str = "李月") -> dict:
        return {
            "content": content,
            "owner": {"text": "", "basis": "not_mentioned"},
            "origin": "stated",
            "evidence": [{"quote": quote or content, "speaker": speaker}],
        }

    @staticmethod
    def pending(content: str, *, owner: str = "", deadline: str = "") -> dict:
        return {
            "content": content,
            "owner": {"text": owner, "basis": "stated" if owner else "not_mentioned"},
            "deadline": {"text": deadline,
                         "basis": "stated" if deadline else "not_mentioned"},
            "origin": "stated",
            "evidence": [{"quote": content, "speaker": "李月"}],
        }


class SeriesApiTest(DomainBase):
    """系列可见性、编辑权限、建议算法（建议不得写库）。"""

    async def test_visibility_edit_permission_and_options(self):
        series = await self.make_series()
        await self.make_meeting(series_id=series.id, participants=(self.member,))
        await self.make_meeting(title="另一场会", participants=(self.member,),
                                series_id=(await self.make_series(name="其他系列")).id)

        page = await self.api("GET", "/meeting/series/selectPage", self.member)
        self.assertEqual(page["data"]["total"], 2)

        page = await self.api("GET", "/meeting/series/selectPage", self.outsider)
        self.assertEqual(page["data"]["total"], 0)
        page = await self.api("GET", "/meeting/series/selectPage", self.admin)
        self.assertEqual(page["data"]["total"], 2)

        filtered = await self.api("GET", "/meeting/series/selectPage", self.owner,
                                  params={"name": "产品发布"})
        self.assertEqual(filtered["data"]["total"], 1)
        self.assertEqual(filtered["data"]["list"][0]["name"], "产品发布评审系列")
        # 列表计数与详情字段必须齐全（前端按契约直接读）
        row = filtered["data"]["list"][0]
        for key in ("id", "name", "description", "cadence", "owner_id", "owner_name",
                    "status", "meeting_count", "open_action_count", "memory_count",
                    "create_time", "update_time"):
            self.assertIn(key, row)
        self.assertEqual(row["meeting_count"], 1)

        options = await self.api("GET", "/meeting/series/options", self.owner)
        self.assertEqual([item["name"] for item in options["data"]],
                         ["产品发布评审系列", "其他系列"])

        denied = await self.client.put(
            "/meeting/series/update", json={"id": series.id, "name": "改名"},
            headers=self.auth(self.member))
        self.assertEqual(denied.status_code, 403)
        updated = await self.api("PUT", "/meeting/series/update", self.owner,
                                 json={"id": series.id, "name": "改名成功",
                                       "cadence": "WEEKLY"})
        self.assertEqual(updated["data"]["name"], "改名成功")
        self.assertEqual(updated["data"]["cadence"], "WEEKLY")
        archived = await self.api("POST", "/meeting/series/archive", self.owner,
                                  json={"id": series.id})
        self.assertEqual(archived["data"]["status"], "ARCHIVED")

    async def test_add_series_owner_defaults_and_admin_assignment(self):
        created = await self.api("POST", "/meeting/series/add", self.member,
                                 json={"name": "周会", "cadence": "WEEKLY"})
        self.assertEqual(created["data"]["owner_id"], self.member.id)
        self.assertEqual(created["data"]["cadence"], "WEEKLY")
        assigned = await self.api("POST", "/meeting/series/add", self.admin,
                                  json={"name": "管理员代建", "cadence": "MONTHLY",
                                        "owner_id": self.owner.id})
        self.assertEqual(assigned["data"]["owner_id"], self.owner.id)
        denied = await self.client.post(
            "/meeting/series/add",
            json={"name": "越权代建", "cadence": "WEEKLY", "owner_id": self.owner.id},
            headers=self.auth(self.member))
        self.assertEqual(denied.status_code, 403)
        invalid = await self.client.post(
            "/meeting/series/add", json={"name": "  "}, headers=self.auth(self.owner))
        self.assertNotEqual(invalid.json()["code"], "200")
        renamed = await self.api(
            "PUT", "/meeting/series/update", self.admin,
            json={"id": assigned["data"]["id"], "name": "管理员代建（改）",
                  "owner_id": self.member.id})
        self.assertEqual(renamed["data"]["owner_id"], self.member.id)

    async def test_detail_returns_meetings_and_counts(self):
        series = await self.make_series()
        meeting = await self.make_meeting(series_id=series.id,
                                          participants=(self.member,))
        await MeetingMemory.create(
            scope_type="SERIES", scope_id=series.id, kind="DECISION", content="已确认决策",
            normalized="已确认决策", status="CONFIRMED", created_by=self.owner.id,
            confirmed_by=self.owner.id, confirmed_time=now())
        await MeetingMemory.create(
            scope_type="SERIES", scope_id=series.id, kind="DECISION", content="待确认候选",
            normalized="待确认候选", status="PROPOSED", created_by=self.owner.id)
        await ActionItem.create(
            series_id=series.id, meeting_id=meeting.id, content="待办一",
            owner_name="张晨", status="OPEN", created_by=self.owner.id)

        detail = await self.api("GET", f"/meeting/series/selectById/{series.id}", self.member)
        data = detail["data"]
        self.assertEqual(data["id"], series.id)
        self.assertEqual(data["meeting_count"], 1)
        self.assertEqual(data["open_action_count"], 1)
        self.assertEqual(data["memory_count"], 1)
        self.assertEqual(data["pending_memory_count"], 1)
        self.assertEqual(data["meetings"][0]["id"], meeting.id)
        self.assertEqual(data["meetings"][0]["title"], meeting.title)

        denied = await self.client.get(
            f"/meeting/series/selectById/{series.id}", headers=self.auth(self.outsider))
        self.assertEqual(denied.status_code, 403)

    async def test_suggest_top3_reasons_and_does_not_write(self):
        series = await self.make_series()
        # 系列历史会议：标题相近、参会人重叠、主持人相同、日期较近
        await self.make_meeting(series_id=series.id, participants=(self.member,))
        await self.make_meeting(title="产品发布评审会议（周会）", day=3,
                                series_id=series.id, participants=(self.member,))
        target = await self.make_meeting(
            title="产品发布评审会", day=10, participants=(self.member,))

        response = await self.api("POST", "/meeting/series/suggest", self.owner,
                                  json={"meeting_id": target.id})
        candidates = response["data"]["candidates"]
        self.assertTrue(0 < len(candidates) <= 3)
        top = candidates[0]
        self.assertEqual(top["series_id"], series.id)
        self.assertGreater(top["score"], 0)
        reasons = " ".join(top["reasons"])
        self.assertIn("标题相似", reasons)
        self.assertIn("参会人重叠", reasons)
        self.assertIn("主持人", reasons)
        # 建议是只读的：会议归属没有被写进去
        self.assertIsNone((await Meeting.get(id=target.id)).series_id)
        # 看不到任何会议的人拿不到候选（系列不可见）
        hidden = await self.api("POST", "/meeting/series/suggest", self.member,
                                json={"meeting_id": target.id})
        self.assertIsInstance(hidden["data"]["candidates"], list)
        denied = await self.client.post(
            "/meeting/series/suggest", json={"meeting_id": target.id},
            headers=self.auth(self.outsider))
        self.assertEqual(denied.status_code, 403)


class MemoryTest(DomainBase):
    """候选生成、冲突替代、可见性与只有 CONFIRMED 进状态查询。"""

    async def test_propose_only_from_decisions_and_idempotent(self):
        series = await self.make_series()
        meeting = await self.make_meeting(series_id=series.id, participants=(self.member,))
        minutes = await self.make_minutes(meeting, decisions=[
            self.decision("9月1日正式发布AI智能会议助手"),
            self.decision("移动端下载问题须在发布前修复"),
        ])
        result = await memory_service.propose_from_minutes(minutes)
        self.assertEqual(result["created"], 2)
        rows = await MeetingMemory.filter(source_id=minutes.id, source_type="MINUTES")
        self.assertEqual({row.status for row in rows}, {"PROPOSED"})
        self.assertEqual({row.kind for row in rows}, {"DECISION"})
        self.assertEqual({row.scope_type for row in rows}, {"SERIES"})
        self.assertEqual({row.scope_id for row in rows}, {series.id})
        first = rows[0]
        self.assertEqual(first.source_meeting_id, meeting.id)
        self.assertIsNotNone(first.evidence_json.get("quote"))
        # 幂等：同一份纪要再跑一次不新增；summary 不会变成结论候选
        again = await memory_service.propose_from_minutes(minutes)
        self.assertEqual(again["created"], 0)
        self.assertEqual(await MeetingMemory.filter(source_id=minutes.id).count(), 2)

    async def test_confirm_conflict_and_supersede(self):
        series = await self.make_series()
        meeting = await self.make_meeting(series_id=series.id, participants=(self.member,))
        old = await MeetingMemory.create(
            scope_type="SERIES", scope_id=series.id, kind="DECISION",
            content="9月1日正式发布AI会议助手", normalized="9月1日正式发布ai会议助手",
            source_type="MANUAL", status="CONFIRMED", created_by=self.owner.id,
            confirmed_by=self.owner.id, confirmed_time=now(), valid_from=now())
        candidate = await MeetingMemory.create(
            scope_type="SERIES", scope_id=series.id, kind="DECISION",
            content="9月1日正式发布AI会议助手功能", normalized="9月1日正式发布ai会议助手功能",
            source_type="MINUTES", source_id=1, source_meeting_id=meeting.id,
            status="PROPOSED", created_by=self.owner.id)

        denied = await self.client.post("/memory/confirm", json={"id": candidate.id},
                                       headers=self.auth(self.member))
        self.assertEqual(denied.status_code, 403)

        # 有相似活跃条目时先返回冲突提示，候选保持 PROPOSED（前端选择替代后再确认）
        conflict = await self.client.post(
            "/memory/confirm", json={"id": candidate.id},
            headers=self.auth(self.owner))
        self.assertEqual(conflict.status_code, 200)
        payload = conflict.json()["data"]
        self.assertFalse(payload["confirmed"])
        self.assertEqual([item["id"] for item in payload["conflicts"]], [old.id])
        self.assertEqual((await MeetingMemory.get(id=candidate.id)).status, "PROPOSED")

        done = await self.api("POST", "/memory/confirm", self.owner,
                              json={"id": candidate.id, "supersede_id": old.id})
        self.assertTrue(done["data"]["confirmed"])
        self.assertEqual(done["data"]["superseded_id"], old.id)
        refreshed_old = await MeetingMemory.get(id=old.id)
        refreshed_new = await MeetingMemory.get(id=candidate.id)
        self.assertEqual(refreshed_old.status, "SUPERSEDED")
        self.assertIsNotNone(refreshed_old.valid_to)
        self.assertEqual(refreshed_new.status, "CONFIRMED")
        self.assertEqual(refreshed_new.supersedes_id, old.id)
        self.assertIsNotNone(refreshed_new.confirmed_time)

    async def test_preference_scope_reject_expire_update(self):
        member_pref = await self.api("POST", "/memory/preference", self.member,
                                     json={"content": "回答先给结论，再给依据"})
        self.assertTrue(member_pref["data"]["created"])
        duplicate = await self.client.post(
            "/memory/preference", json={"content": "回答先给结论再给依据"},
            headers=self.auth(self.member))
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("相似", duplicate.json()["msg"])

        await self.api("POST", "/memory/preference", self.outsider,
                       json={"content": "只用英文回答"})
        page = await self.api("GET", "/memory/selectPage", self.member,
                              params={"scope_type": "USER"})
        contents = [row["content"] for row in page["data"]["list"]]
        self.assertIn("回答先给结论，再给依据", contents)
        self.assertNotIn("只用英文回答", contents)

        pref_id = member_pref["data"]["id"]
        denied = await self.client.post(
            "/memory/confirm", json={"id": pref_id}, headers=self.auth(self.owner))
        self.assertEqual(denied.status_code, 403)

        proposed = await MeetingMemory.create(
            scope_type="SERIES", scope_id=(await self.make_series()).id, kind="DECISION",
            content="待拒绝的候选", normalized="待拒绝的候选", status="PROPOSED",
            created_by=self.owner.id)
        rejected = await self.api("POST", "/memory/reject", self.owner,
                                 json={"id": proposed.id})
        self.assertEqual(rejected["data"]["status"], "REJECTED")

        updated = await self.api("PUT", "/memory/update", self.member,
                                 json={"id": pref_id, "content": "回答先给结论"})
        self.assertEqual(updated["data"]["content"], "回答先给结论")
        expired = await self.api("POST", "/memory/expire", self.member, json={"id": pref_id})
        self.assertEqual(expired["data"]["status"], "EXPIRED")
        self.assertIsNotNone(expired["data"]["valid_to"])


class StateServiceTest(DomainBase):
    """query_state：只返回 CONFIRMED 记忆与有效待办，权限只收窄不扩大。"""

    async def test_only_confirmed_memories_and_required_keys(self):
        series = await self.make_series()
        meeting = await self.make_meeting(series_id=series.id, participants=(self.member,))
        confirmed = await MeetingMemory.create(
            scope_type="SERIES", scope_id=series.id, kind="DECISION",
            content="已确认决策", normalized="已确认决策", status="CONFIRMED",
            source_type="MINUTES", source_id=99, source_meeting_id=meeting.id,
            evidence_json={"quote": "原话", "speaker": "李月"},
            created_by=self.owner.id, confirmed_by=self.owner.id, confirmed_time=now())
        await MeetingMemory.create(
            scope_type="SERIES", scope_id=series.id, kind="DECISION",
            content="未确认候选", normalized="未确认候选", status="PROPOSED",
            source_meeting_id=meeting.id, created_by=self.owner.id)

        actor = SimpleNamespace(user=self.member,
                                allowed_meeting_ids=[meeting.id])
        items = await state_service.query_state(actor, "decision")
        self.assertEqual([item["content"] for item in items], ["已确认决策"])
        required = {"kind", "content", "status", "source_meeting_id", "meeting_title",
                    "minutes_id", "field", "item_index", "evidence", "owner_name",
                    "deadline_text", "series_id", "series_name", "state_id"}
        self.assertTrue(required.issubset(items[0].keys()))
        self.assertEqual(items[0]["state_id"], confirmed.id)
        self.assertEqual(items[0]["meeting_title"], meeting.title)
        self.assertEqual(items[0]["series_name"], series.name)
        self.assertEqual(items[0]["kind"], "decision")

        # 权限收窄：允许会议集合不含该会议时一条都不返回
        blocked = SimpleNamespace(user=self.member, allowed_meeting_ids=[])
        self.assertEqual(await state_service.query_state(blocked, "decision"), [])

    async def test_action_items_and_preferences_scope(self):
        series = await self.make_series()
        meeting = await self.make_meeting(series_id=series.id, participants=(self.member,))
        open_item = await ActionItem.create(
            series_id=series.id, meeting_id=meeting.id, content="未完成待办",
            owner_user_id=self.member.id, owner_name="张晨", status="OPEN",
            deadline_text="2020-01-01", created_by=self.owner.id)
        await ActionItem.create(
            series_id=series.id, meeting_id=meeting.id, content="已完成待办",
            owner_name="张晨", status="DONE", created_by=self.owner.id)
        prefix = "9月1日发布"
        await MeetingMemory.create(
            scope_type="SERIES", scope_id=series.id, kind="DECISION", content=prefix,
            normalized=prefix, status="CONFIRMED", source_meeting_id=meeting.id,
            created_by=self.owner.id, confirmed_by=self.owner.id, confirmed_time=now())

        actor = SimpleNamespace(user=self.member, allowed_meeting_ids=[meeting.id])
        items = await state_service.query_state(actor, "action_item", status="open")
        self.assertEqual([item["content"] for item in items], ["未完成待办"])
        self.assertEqual(items[0]["owner_name"], "张晨")
        self.assertEqual(items[0]["deadline_text"], "2020-01-01")
        self.assertEqual(items[0]["state_id"], open_item.id)
        done = await state_service.query_state(actor, "action_item", status="done")
        self.assertEqual([item["content"] for item in done], ["已完成待办"])

        await self.api("POST", "/memory/preference", self.member,
                       json={"content": "偏好一"})
        prefs = await state_service.query_state(actor, "preference")
        self.assertEqual([item["content"] for item in prefs], ["偏好一"])
        # 别人的偏好看不到
        other = SimpleNamespace(user=self.outsider, allowed_meeting_ids=[])
        self.assertEqual(await state_service.query_state(other, "preference"), [])


class ActionItemTest(DomainBase):
    """待办生成/负责人匹配/期限解析/判重结转/状态事件/手工维护。"""

    async def test_sync_generates_owner_and_deadline(self):
        series = await self.make_series()
        meeting = await self.make_meeting(series_id=series.id, participants=(self.member,))
        minutes = await self.make_minutes(meeting, pending_items=[
            self.pending("修复移动端附件下载进度问题", owner="张晨", deadline="8月30日18:00"),
            self.pending("完成灰度方案", owner="外部顾问", deadline="待定"),
        ])
        result = await action_item_service.sync_from_minutes(minutes, actor_id=self.owner.id)
        self.assertEqual(result, {"created": 2, "carried": 0, "skipped": 0})
        rows = await ActionItem.filter(minutes_id=minutes.id).order_by("source_index")
        first, second = rows
        self.assertEqual(first.series_id, series.id)
        self.assertEqual(first.meeting_id, meeting.id)
        self.assertEqual(first.owner_user_id, self.member.id)
        self.assertEqual(first.owner_name, "张晨")
        self.assertEqual(first.deadline_text, "8月30日18:00")
        self.assertEqual(first.deadline.date(), datetime(2026, 8, 30).date())
        self.assertIsNone(second.owner_user_id)
        self.assertEqual(second.owner_name, "外部顾问")
        self.assertIsNone(second.deadline)
        self.assertEqual(second.deadline_text, "待定")
        self.assertEqual(await ActionItemEvent.filter(
            action_item_id=first.id, event_type="CREATE").count(), 1)

    async def test_duplicate_carry_over_is_idempotent(self):
        series = await self.make_series()
        first_meeting = await self.make_meeting(
            series_id=series.id, participants=(self.member,), day=3)
        second_meeting = await self.make_meeting(
            title="产品发布评审会议（周会）", series_id=series.id,
            participants=(self.member,), day=10)
        item = [self.pending("修复移动端附件下载进度问题", owner="张晨",
                             deadline="8月30日18:00")]
        await action_item_service.sync_from_minutes(
            await self.make_minutes(first_meeting, pending_items=item),
            actor_id=self.owner.id)
        second_minutes = await self.make_minutes(second_meeting, pending_items=item)
        carried = await action_item_service.sync_from_minutes(
            second_minutes, actor_id=self.owner.id)
        self.assertEqual(carried, {"created": 0, "carried": 1, "skipped": 1})
        self.assertEqual(await ActionItem.filter(series_id=series.id).count(), 1)
        row = await ActionItem.get(series_id=series.id)
        self.assertEqual(row.last_seen_meeting_id, second_meeting.id)
        carry_events = await ActionItemEvent.filter(
            action_item_id=row.id, event_type="CARRY_OVER")
        self.assertEqual(len(carry_events), 1)
        self.assertEqual(carry_events[0].from_value, str(first_meeting.id))
        self.assertEqual(carry_events[0].to_value, str(second_meeting.id))
        # 同一场会重复同步不再写结转事件（钩子幂等）
        again = await action_item_service.sync_from_minutes(
            second_minutes, actor_id=self.owner.id)
        self.assertEqual(again, {"created": 0, "carried": 0, "skipped": 1})
        self.assertEqual(await ActionItemEvent.filter(
            action_item_id=row.id, event_type="CARRY_OVER").count(), 1)

    async def test_status_events_and_permissions(self):
        series = await self.make_series()
        meeting = await self.make_meeting(series_id=series.id, participants=(self.member,))
        minutes = await self.make_minutes(meeting, pending_items=[
            self.pending("修复移动端附件下载进度问题", owner="张晨")])
        await action_item_service.sync_from_minutes(minutes, actor_id=self.owner.id)
        row = await ActionItem.get(series_id=series.id)
        # 负责人本人可改状态，写 STATUS 事件
        status = await self.api("PUT", "/action-item/updateStatus", self.member,
                                json={"id": row.id, "status": "DONE", "note": "已完成"})
        self.assertEqual(status["data"]["status"], "DONE")
        events = await self.api("GET", f"/action-item/events/{row.id}", self.member)
        last = events["data"][-1]
        self.assertEqual((last["event_type"], last["from_value"], last["to_value"]),
                         ("STATUS", "OPEN", "DONE"))
        self.assertEqual(last["note"], "已完成")
        # 与会议无关的人不能操作
        denied = await self.client.put(
            "/action-item/updateStatus", json={"id": row.id, "status": "OPEN"},
            headers=self.auth(self.outsider))
        self.assertEqual(denied.status_code, 403)
        # 非法状态值被拒绝
        invalid = await self.client.put(
            "/action-item/updateStatus", json={"id": row.id, "status": "UNKNOWN"},
            headers=self.auth(self.member))
        self.assertNotEqual(invalid.json()["code"], "200")

    async def test_manual_add_update_list_and_overdue(self):
        series = await self.make_series()
        meeting = await self.make_meeting(series_id=series.id, participants=(self.member,))
        added = await self.api("POST", "/action-item/add", self.member,
                              json={"meeting_id": meeting.id, "content": "整理发布清单",
                                    "owner_user_id": self.member.id,
                                    "deadline_text": "2020-01-01"})
        data = added["data"]
        self.assertEqual(data["series_id"], series.id)
        self.assertEqual(data["owner_name"], "张晨")
        self.assertEqual(data["deadline"], "2020-01-01 00:00:00")
        item_id = data["id"]
        updated = await self.api("PUT", "/action-item/update", self.member,
                                 json={"id": item_id, "content": "整理发布清单（新版）",
                                       "deadline_text": "2020-02-02"})
        self.assertEqual(updated["data"]["content"], "整理发布清单（新版）")
        self.assertEqual(updated["data"]["deadline"], "2020-02-02 00:00:00")
        events = await action_item_service.list_events(self.member, item_id)
        self.assertIn("DEADLINE", [event["event_type"] for event in events])
        # 逾期过滤命中；状态过滤不命中；关联名称齐备
        overdue = await self.api("GET", "/action-item/selectPage", self.member,
                                 params={"overdue": "true"})
        self.assertEqual(overdue["data"]["total"], 1)
        row = overdue["data"]["list"][0]
        self.assertEqual(row["series_name"], series.name)
        self.assertEqual(row["meeting_title"], meeting.title)
        self.assertEqual(row["owner_user_id"], self.member.id)
        done = await self.api("GET", "/action-item/selectPage", self.member,
                              params={"status": "DONE"})
        self.assertEqual(done["data"]["total"], 0)
        # 不相关的人看不到
        hidden = await self.api("GET", "/action-item/selectPage", self.outsider)
        self.assertEqual(hidden["data"]["total"], 0)


class MinutesHookTest(DomainBase):
    """纪要确认钩子：生成、重新生成、取消确认/删除后的撤回，失败不阻断。"""

    async def _draft(self) -> tuple[Meeting, MeetingMinutes]:
        series = await self.make_series()
        meeting = await self.make_meeting(series_id=series.id, participants=(self.member,))
        minutes = await self.make_minutes(
            meeting, status="DRAFT",
            decisions=[self.decision("9月1日正式发布AI智能会议助手")],
            pending_items=[self.pending("修复移动端附件下载进度问题", owner="张晨",
                                        deadline="8月30日18:00")])
        return meeting, minutes

    async def test_confirm_hook_generates_then_regenerate_and_unconfirm(self):
        _, minutes = await self._draft()
        confirmed = await self.api("PUT", f"/meetingMinutes/confirm/{minutes.id}", self.owner)
        self.assertEqual(confirmed["code"], "200")
        self.assertEqual(await MeetingMemory.filter(
            source_id=minutes.id, source_type="MINUTES", status="PROPOSED").count(), 1)
        self.assertEqual(await ActionItem.filter(minutes_id=minutes.id).count(), 1)

        # 重复确认被状态机挡下：钩子不会跑第二遍
        repeated = await self.client.put(
            f"/meetingMinutes/confirm/{minutes.id}", headers=self.auth(self.owner))
        self.assertNotEqual(repeated.json()["code"], "200")
        self.assertEqual(await MeetingMemory.filter(source_id=minutes.id).count(), 1)
        self.assertEqual(await ActionItem.filter(minutes_id=minutes.id).count(), 1)

        # 管理员重新生成候选：旧候选撤回后重建，待办幂等不重复
        regenerated = await self.api(
            "POST", f"/meetingMinutes/regenerateCandidates/{minutes.id}", self.admin)
        self.assertEqual(regenerated["data"]["memory_rejected"], 1)
        self.assertEqual(regenerated["data"]["memory_created"], 1)
        self.assertEqual(regenerated["data"]["action_created"], 0)
        self.assertEqual(await MeetingMemory.filter(
            source_id=minutes.id, status="PROPOSED").count(), 1)
        self.assertEqual(await ActionItem.filter(minutes_id=minutes.id).count(), 1)

        # 取消确认：该纪要来源的候选置 REJECTED，待办保留
        await self.api("PUT", f"/meetingMinutes/unconfirm/{minutes.id}", self.owner)
        statuses = await MeetingMemory.filter(
            source_id=minutes.id).values_list("status", flat=True)
        self.assertEqual(set(statuses), {"REJECTED"})
        self.assertEqual(await ActionItem.filter(minutes_id=minutes.id).count(), 1)

    async def test_delete_minutes_rejects_candidates_but_keeps_action_items(self):
        meeting, minutes = await self._draft()
        await self.api("PUT", f"/meetingMinutes/confirm/{minutes.id}", self.owner)
        await self.api("DELETE", f"/meetingMinutes/delete/{minutes.id}", self.owner)
        self.assertFalse(await MeetingMinutes.filter(id=minutes.id).exists())
        statuses = await MeetingMemory.filter(source_id=minutes.id).values_list(
            "status", flat=True)
        self.assertEqual(set(statuses), {"REJECTED"})
        # 待办不随纪要删除（历史保留，可能已在别的会议上结转）
        self.assertEqual(await ActionItem.filter(meeting_id=meeting.id).count(), 1)

    async def test_generation_failure_does_not_block_confirm(self):
        _, minutes = await self._draft()
        with patch.object(memory_service, "propose_from_minutes",
                          side_effect=RuntimeError("候选写入失败")):
            response = await self.api(
                "PUT", f"/meetingMinutes/confirm/{minutes.id}", self.owner)
        self.assertEqual(response["code"], "200")
        self.assertEqual((await MeetingMinutes.get(id=minutes.id)).status, "CONFIRMED")
        self.assertEqual(await MeetingMemory.filter(source_id=minutes.id).count(), 0)
        # 待办侧的同步是独立 try：一个失败不影响另一个
        self.assertEqual(await ActionItem.filter(minutes_id=minutes.id).count(), 1)

    async def test_regenerate_requires_admin_and_confirmed_minutes(self):
        _, minutes = await self._draft()
        denied = await self.client.post(
            f"/meetingMinutes/regenerateCandidates/{minutes.id}",
            headers=self.auth(self.owner))
        self.assertEqual(denied.status_code, 403)
        not_confirmed = await self.client.post(
            f"/meetingMinutes/regenerateCandidates/{minutes.id}",
            headers=self.auth(self.admin))
        self.assertNotEqual(not_confirmed.json()["code"], "200")


class MeetingSeriesIntegrationTest(DomainBase):
    """会议接口的系列字段：payload 写入、列表过滤、权限校验、删除不动系列。"""

    def _payload(self, series_id: int | None, **overrides) -> dict:
        payload = {
            "title": "系列内会议",
            "start_time": "2026-10-01 10:00:00",
            "end_time": "2026-10-01 11:00:00",
            "location": "线上会议",
            "host_id": self.owner.id,
            "participant_ids": [self.member.id],
            "series_id": series_id,
        }
        payload.update(overrides)
        return payload

    async def test_meeting_saves_series_and_filters_by_it(self):
        series = await self.make_series()
        added = await self.api("POST", "/meeting/add", self.owner,
                               json=self._payload(series.id))
        data = added["data"]
        self.assertEqual(data["series_id"], series.id)
        self.assertEqual(data["series_name"], series.name)
        meeting_id = data["id"]

        page = await self.api("GET", "/meeting/selectPage", self.member,
                              params={"series_id": series.id})
        self.assertEqual(page["data"]["total"], 1)
        self.assertEqual(page["data"]["list"][0]["series_id"], series.id)

        other = await self.make_series(name="其他系列")
        updated = await self.api("PUT", "/meeting/update", self.owner,
                                 json={**self._payload(other.id), "id": meeting_id})
        self.assertEqual(updated["data"]["series_id"], other.id)
        self.assertEqual(updated["data"]["series_name"], "其他系列")

        detail = await self.api("GET", f"/meeting/selectById/{meeting_id}", self.member)
        self.assertEqual(detail["data"]["series_id"], other.id)
        self.assertEqual(detail["data"]["series_name"], "其他系列")

    async def test_meeting_rejects_invisible_series(self):
        series = await self.make_series()
        denied = await self.client.post(
            "/meeting/add", json=self._payload(series.id, host_id=self.member.id,
                                               participant_ids=[]),
            headers=self.auth(self.outsider))
        self.assertEqual(denied.status_code, 403)

    async def test_delete_meeting_keeps_series(self):
        series = await self.make_series()
        meeting = await self.make_meeting(series_id=series.id)
        await self.api("DELETE", f"/meeting/delete/{meeting.id}", self.admin)
        self.assertFalse(await Meeting.filter(id=meeting.id).exists())
        self.assertTrue(await MeetingSeries.filter(id=series.id).exists())


if __name__ == "__main__":
    unittest.main()
