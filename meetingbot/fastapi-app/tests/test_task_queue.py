"""任务调度的单测：按类型分队列、按类型配并发、兼容队列不被漏掉。

这三条出错都不会报错，只会表现为"任务一直排队"或"一类任务打满影响另一类"，
所以直接钉住队列名与并发取值的规则。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from services import task_queue


class TaskQueueTest(unittest.TestCase):
    def test_each_kind_gets_its_own_queue(self):
        queues = {kind: task_queue.task_queue_for(kind) for kind in task_queue.KINDS}
        self.assertEqual(len(set(queues.values())), len(queues), "队列名不能撞车")
        for kind, name in queues.items():
            self.assertTrue(name.endswith(kind), name)

    def test_concurrency_falls_back_to_the_global_value(self):
        with mock.patch.dict(os.environ, {"TEMPORAL_MAX_CONCURRENCY": "5"}):
            for kind in task_queue.KINDS:
                self.assertEqual(task_queue.max_concurrent(kind), 5)

    def test_per_kind_override_wins(self):
        with mock.patch.dict(os.environ, {"TEMPORAL_MAX_CONCURRENCY": "5",
                                          "TEMPORAL_MAX_CONCURRENCY_TRANSCRIPTION": "1",
                                          "TEMPORAL_MAX_CONCURRENCY_AGENT_RUN": "9"}):
            self.assertEqual(task_queue.max_concurrent(task_queue.KIND_TRANSCRIPTION), 1)
            self.assertEqual(task_queue.max_concurrent(task_queue.KIND_AGENT_RUN), 9)
            # 没单独配的类型仍走全局值
            self.assertEqual(task_queue.max_concurrent(task_queue.KIND_MINUTES), 5)

    def test_kb_index_env_name_uses_underscores(self):
        # 类型名里有连字符（kb-index），环境变量名不能带它
        with mock.patch.dict(os.environ, {"TEMPORAL_MAX_CONCURRENCY_KB_INDEX": "3"}):
            self.assertEqual(task_queue.max_concurrent(task_queue.KIND_KB_INDEX), 3)

    def test_zero_or_negative_is_rejected(self):
        with mock.patch.dict(os.environ, {"TEMPORAL_MAX_CONCURRENCY_MINUTES": "0"}), \
                self.assertRaises(ValueError):
            task_queue.max_concurrent(task_queue.KIND_MINUTES)

    def test_every_kind_has_an_activity_and_a_workflow(self):
        from services import temporal_workflows
        for kind in task_queue.KINDS:
            self.assertEqual(len(temporal_workflows.activities_for(kind)), 1, kind)
            self.assertIn(kind, temporal_workflows.WORKFLOW_BY_KIND, kind)
        # 兼容 worker 要能消费所有类型的在途任务
        self.assertEqual(len(temporal_workflows.all_activities()), len(task_queue.KINDS))
        self.assertEqual(temporal_workflows.activities_for("未知类型"), [])


if __name__ == "__main__":
    unittest.main()
