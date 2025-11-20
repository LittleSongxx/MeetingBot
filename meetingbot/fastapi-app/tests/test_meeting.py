"""会议 API 的回归测试。

来历：2026-09-27 修复轮给 save_participants 加事务时，导入语句因锚文本不匹配
静默未插入（"from common.result import Result" vs 实际 "…PageInfo, Result"），
单元测试全绿而线上 /meeting/add 500——meeting.py 当时没有任何测试文件。
本文件锁住两件事：模块可导入且事务依赖在位；save_participants 的删旧+插新
确实发生在事务里。
"""

import unittest
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch


class FakeQuerySet:
    """可 await（返回旧名单）也可 .delete()（记日志）——Tortoise QuerySet 的两副面孔。"""

    def __init__(self, journal: list, rows: list):
        self._journal, self._rows = journal, rows

    def __await__(self):
        async def _coro():
            return self._rows
        return _coro().__await__()

    async def delete(self):
        self._journal.append("delete")


class MeetingModuleIntegrityTest(unittest.TestCase):
    def test_transaction_dependency_is_importable_at_call_time(self):
        """锁 NameError 类回归：函数体引用的名字必须在模块全局里存在。"""
        from api import meeting as api_meeting
        self.assertTrue(callable(api_meeting.in_transaction))


class SaveParticipantsTest(unittest.IsolatedAsyncioTestCase):
    async def test_delete_and_recreate_run_inside_transaction(self):
        from api import meeting as api_meeting

        meeting = MagicMock(creator_id=1, host_id=2, id=10)
        journal: list[str] = []

        @asynccontextmanager
        async def fake_transaction():
            journal.append("begin")
            yield
            journal.append("commit")

        query = FakeQuerySet(journal, rows=[])  # 旧名单为空 → 新人都从 PENDING 起
        participant_model = MagicMock()
        participant_model.filter = MagicMock(return_value=query)

        async def fake_bulk_create(rows):
            journal.append(f"bulk:{len(rows)}")

        participant_model.bulk_create = fake_bulk_create

        with patch.object(api_meeting, "in_transaction", fake_transaction), \
             patch.object(api_meeting, "MeetingParticipant", participant_model):
            await api_meeting.save_participants(meeting, [3])

        self.assertEqual(journal[0], "begin")
        self.assertEqual(journal[-1], "commit")
        self.assertIn("delete", journal)
        self.assertIn("bulk:3", journal)  # 参会人3 + 创建人 + 主持人


if __name__ == "__main__":
    unittest.main()
