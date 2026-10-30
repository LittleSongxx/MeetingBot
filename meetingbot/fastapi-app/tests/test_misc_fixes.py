import io
import unittest
from pathlib import Path
from uuid import uuid4

from api import account_dict
from api.department import delete_batch as department_delete_batch
from api.files import UPLOAD_DIR, upload_file
from api.meeting_material import delete_batch as material_delete_batch
from api.meeting_minutes import delete_minutes
from api.user import delete_batch as user_delete_batch
from common.urls import to_relative_url
from models import (
    AgentRun,
    AgentStep,
    Department,
    Meeting,
    MeetingMaterial,
    MeetingMinutes,
    User,
)
from starlette.datastructures import UploadFile
from tortoise import Tortoise


class MiscFixTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=False,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.admin = await User.create(
            username="misc_admin", password="x", name="管理员", role="ADMIN"
        )
        self.owner = await User.create(
            username="misc_owner", password="x", name="创建人", role="EMPLOYEE"
        )
        self.meeting = await Meeting.create(
            meeting_no="MTG-MISC-001", title="杂项验证",
            start_time="2026-09-01 09:00:00", end_time="2026-09-01 10:00:00",
            creator_id=self.owner.id, host_id=self.owner.id, status="FINISHED",
        )

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    def test_relative_url_normalization(self):
        # 历史绝对地址 → 相对路径；外部地址与相对路径原样
        self.assertEqual(
            to_relative_url("http://127.0.0.1:9090/files/download/a.png"), "/files/download/a.png"
        )
        self.assertEqual(to_relative_url("/files/download/a.png"), "/files/download/a.png")
        self.assertEqual(to_relative_url("https://cdn.example.com/x.png"), "https://cdn.example.com/x.png")
        self.assertIsNone(to_relative_url(None))

    async def test_account_dict_returns_relative_avatar(self):
        await User.filter(id=self.owner.id).update(avatar="http://127.0.0.1:9090/files/download/3.png")
        user = await User.get(id=self.owner.id)
        data = await account_dict(user)
        self.assertEqual(data["avatar"], "/files/download/3.png")

    async def test_avatar_upload_returns_relative_url(self):
        upload = UploadFile(filename="avatar.png", file=io.BytesIO(b"\x89PNG\r\n\x1a\nfake"))
        result = await upload_file(upload, self.admin)
        self.assertEqual(result.code, "200")
        self.assertTrue(result.data.startswith("/files/download/"))
        stored = UPLOAD_DIR / result.data.rsplit("/", 1)[-1]
        self.assertTrue(stored.is_file())
        Path(stored).unlink(missing_ok=True)

    async def test_batch_delete_with_json_array_body(self):
        # 五个删除入口的 ids 现在是 Body 参数；直接传 list 等价于前端 JSON 数组
        department = await Department.create(name="待删部门", description="", status="NORMAL")
        await department_delete_batch([department.id], self.admin)
        self.assertFalse(await Department.filter(id=department.id).exists())

        spare = await User.create(username="misc_spare", password="x", name="待删账号", role="EMPLOYEE")
        await user_delete_batch([spare.id], self.admin)
        self.assertFalse(await User.filter(id=spare.id).exists())

        material = await MeetingMaterial.create(
            meeting_id=self.meeting.id, file_name="a.txt", storage_name=uuid4().hex + ".txt",
            file_type="ATTACHMENT", content_type="text/plain", file_ext="txt",
            file_size=1, file_hash=uuid4().hex, uploader_id=self.owner.id,
        )
        await material_delete_batch([material.id], self.owner)
        self.assertFalse(await MeetingMaterial.filter(id=material.id).exists())

    async def test_delete_minutes_cascades_agent_runs(self):
        minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id, source_task_id=1, model_config_id=1,
            created_by=self.owner.id, status="DRAFT", summary="待删纪要",
        )
        run = await AgentRun.create(
            run_no="AGR-MISC-001", minutes_id=minutes.id, meeting_id=self.meeting.id,
            user_id=self.owner.id, model_config_id=1, status="SUCCEEDED",
        )
        await AgentStep.create(run_id=run.id, step_no=1, step_type="REVIEW", status="SUCCEEDED")
        await delete_minutes(minutes, self.owner)
        self.assertFalse(await MeetingMinutes.filter(id=minutes.id).exists())
        self.assertFalse(await AgentRun.filter(id=run.id).exists())
        self.assertEqual(await AgentStep.filter(run_id=run.id).count(), 0)


if __name__ == "__main__":
    unittest.main()
