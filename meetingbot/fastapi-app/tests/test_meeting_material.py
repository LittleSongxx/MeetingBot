import unittest
from io import BytesIO

from api.meeting_material import MATERIAL_DIR, delete, select_page, upload
from common.exception_handler import CustomException
from models import Meeting, MeetingMaterial, MeetingParticipant, User
from starlette.datastructures import Headers, UploadFile
from tortoise import Tortoise


class MeetingMaterialTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=True,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.admin = await User.create(
            username="admin", password="admin", name="管理员", role="ADMIN"
        )
        self.creator = await User.create(
            username="creator", password="123456", name="创建人", role="EMPLOYEE"
        )
        self.participant = await User.create(
            username="participant", password="123456", name="参会人", role="EMPLOYEE"
        )
        self.outsider = await User.create(
            username="outsider", password="123456", name="无关员工", role="EMPLOYEE"
        )
        self.meeting = await Meeting.create(
            meeting_no="MTG-MATERIAL-001",
            title="会议资料权限验证",
            start_time="2026-08-27 09:00:00",
            end_time="2026-08-27 10:00:00",
            creator_id=self.creator.id,
            host_id=self.creator.id,
        )
        await MeetingParticipant.create(
            meeting_id=self.meeting.id,
            user_id=self.participant.id,
            participant_role="PARTICIPANT",
        )

    async def asyncTearDown(self):
        for material in await MeetingMaterial.all():
            (MATERIAL_DIR / material.storage_name).unlink(missing_ok=True)
        await Tortoise.close_connections()

    @staticmethod
    def audio_file(content: bytes = b"meeting audio content") -> UploadFile:
        return UploadFile(
            file=BytesIO(content),
            filename="weekly-meeting.mp3",
            headers=Headers({"content-type": "audio/mpeg"}),
        )

    async def test_upload_list_duplicate_and_delete(self):
        first = await upload(self.meeting.id, self.audio_file(), self.participant)
        self.assertEqual(first.code, "200")
        self.assertEqual(first.data["file_type"], "AUDIO")
        self.assertEqual(await MeetingMaterial.all().count(), 1)

        second = await upload(self.meeting.id, self.audio_file(), self.participant)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(await MeetingMaterial.all().count(), 1)

        page = await select_page(
            meetingId=self.meeting.id,
            fileName="weekly",
            fileType="AUDIO",
            pageNum=1,
            pageSize=10,
            current_user=self.participant,
        )
        self.assertEqual(page.data["total"], 1)

        await delete(first.data["id"], self.admin)
        self.assertEqual(await MeetingMaterial.all().count(), 0)

    async def test_outsider_cannot_access_materials(self):
        with self.assertRaises(CustomException) as context:
            await select_page(
                meetingId=self.meeting.id,
                current_user=self.outsider,
            )
        self.assertEqual(context.exception.code, "403")


if __name__ == "__main__":
    unittest.main()


class AvatarDownloadTest(unittest.IsolatedAsyncioTestCase):
    """头像文件不存在时必须是 404，不能是 500。

    实测：种子里有条目的头像名在部署里并不存在（头像是用户上传的数据，不随仓库
    分发），前端 <img> 一加载，FileResponse 就抛 FileNotFoundError → 500。
    "资源缺失"被记成"服务端故障"会污染失败率与告警，排查方向也会被带偏。
    """

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_missing_avatar_is_a_404_with_a_readable_message(self):
        from api import files as files_api
        with self.assertRaises(CustomException) as ctx:
            await files_api.download_file("一定不存在-abc123.png")
        self.assertEqual(ctx.exception.code, "404")
        self.assertIn("文件不存在", ctx.exception.message)

    async def test_path_traversal_is_still_refused(self):
        from api import files as files_api
        with self.assertRaises(CustomException):
            await files_api.download_file("../.env")
