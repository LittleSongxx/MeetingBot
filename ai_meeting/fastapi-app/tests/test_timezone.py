import unittest
from datetime import datetime

from tortoise import Tortoise

from api.meeting_minutes import minutes_dict
from common.times import LOCAL_TZ, format_datetime, now
from models import Meeting, MeetingMinutes, User
from services.observability_service import parse_date_range


class TimezoneTest(unittest.IsolatedAsyncioTestCase):
    """页面上所有时间都按东八区显示，这里盯住存进去和读出来的一致性。"""

    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=True,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.owner = await User.create(username="tz_owner", password="123456", name="张三", role="EMPLOYEE")
        self.meeting = await Meeting.create(
            meeting_no="MTG-TZ-001", title="时区校验会议",
            start_time="2026-08-27 09:00:00", end_time="2026-08-27 10:00:00",
            creator_id=self.owner.id, host_id=self.owner.id, status="FINISHED",
        )

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_confirmed_time_reads_back_as_the_moment_it_was_written(self):
        """纪要确认时间存的是带时区的时刻，读回来必须还是页面上看到的那个点。"""
        confirmed = datetime(2026, 3, 5, 18, 30, 0, tzinfo=LOCAL_TZ)
        minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id, source_task_id=1, model_config_id=1,
            created_by=self.owner.id, status="CONFIRMED",
            confirmed_by=self.owner.id, confirmed_time=confirmed,
        )
        data = await minutes_dict(minutes)
        self.assertEqual(data["confirmed_time"], "2026-03-05 18:30:00")

    async def test_default_date_range_covers_local_today(self):
        _, end_date, start_time, end_time = parse_date_range(None, None)
        self.assertEqual(end_date, now().date())
        self.assertEqual(start_time.utcoffset(), LOCAL_TZ.utcoffset(None))
        self.assertLess(now(), end_time)

    async def test_format_datetime_renders_local_time(self):
        self.assertEqual(format_datetime(datetime(2026, 3, 5, 23, 59, 59, tzinfo=LOCAL_TZ)), "2026-03-05 23:59:59")


if __name__ == "__main__":
    unittest.main()
