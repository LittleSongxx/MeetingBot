import unittest
from datetime import datetime, timedelta, timezone

from tortoise import Tortoise

from api.user import has_business_reference, validate_user_delete
from common.exception_handler import CustomException
from models import AgentRun, Meeting, User


class UserIntegrityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=True,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.user = await User.create(username="deletable", password="123456", name="Deletable", role="EMPLOYEE")
        self.owner = await User.create(username="owner", password="123456", name="Owner", role="EMPLOYEE")

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_unused_user_can_be_deleted(self):
        self.assertFalse(await has_business_reference(self.user.id))
        await validate_user_delete([self.user.id])

    async def test_meeting_and_agent_run_references_block_deletion(self):
        now = datetime.now(timezone.utc)
        await Meeting.create(
            meeting_no="MTG-USER-INTEGRITY", title="Reference meeting", start_time=now,
            end_time=now + timedelta(hours=1), creator_id=self.user.id, host_id=self.owner.id,
        )
        self.assertTrue(await has_business_reference(self.user.id))
        with self.assertRaises(CustomException):
            await validate_user_delete([self.user.id])

        # 发起过自检的人同样不能删，运行记录上还挂着他的 ID
        reviewer = await User.create(username="reviewer", password="123456", name="Reviewer", role="EMPLOYEE")
        await AgentRun.create(
            run_no="AGR-USER-INTEGRITY", minutes_id=1, meeting_id=1,
            user_id=reviewer.id, model_config_id=1, status="SUCCEEDED",
        )
        self.assertTrue(await has_business_reference(reviewer.id))
        with self.assertRaises(CustomException):
            await validate_user_delete([reviewer.id])


if __name__ == "__main__":
    unittest.main()
