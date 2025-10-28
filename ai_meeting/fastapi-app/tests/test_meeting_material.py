import unittest
from io import BytesIO

from starlette.datastructures import Headers, UploadFile
from tortoise import Tortoise

from api.meeting_material import MATERIAL_DIR, delete, select_page, upload
from common.exception_handler import CustomException
from models import Meeting, MeetingMaterial, MeetingParticipant, User


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
