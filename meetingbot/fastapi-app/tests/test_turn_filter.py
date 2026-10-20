"""turn 级上下文过滤的单测：纯函数 + 引用登记表 prune，零模型调用。"""
from __future__ import annotations

import json
import unittest

from langchain_core.messages import ToolMessage
from services import citations, turn_filter


class TurnFilterTest(unittest.TestCase):
    def test_collect_and_apply_keep(self):
        payload = {"ok": True, "hits": [
            {"ref": 1, "quote": "a"}, {"ref": 2, "quote": "b"}, {"ref": 3, "quote": "c"}]}
        message = ToolMessage(content=json.dumps(payload), tool_call_id="t1",
                              name="search_knowledge")
        entries = turn_filter.collect_entries([message])
        self.assertEqual([entry["ref"] for entry in entries], [1, 2, 3])
        registry = citations.CitationRegistry.from_dicts([
            {"ref": 1, "kind": "chunk", "meeting_id": 1, "quote": "a", "chunk_id": 1},
            {"ref": 2, "kind": "chunk", "meeting_id": 1, "quote": "b", "chunk_id": 2},
            {"ref": 3, "kind": "chunk", "meeting_id": 1, "quote": "c", "chunk_id": 3},
        ])
        filtered = turn_filter.apply_keep([message], entries, {1, 3}, registry)
        data = json.loads(filtered[0].content)
        self.assertEqual([hit["ref"] for hit in data["hits"]], [1, 3])
        self.assertEqual([item.ref for item in registry.all()], [1, 3])

    def test_conflict_hint_only_for_count_question_with_multiple_counts(self):
        first = ToolMessage(content=json.dumps({"ok": True, "hits": [
            {"ref": 1, "quote": "抽样检查了管理员、会议创建人、主持人、普通参会人员四类账号"}]}),
            tool_call_id="t1", name="search_knowledge")
        second = ToolMessage(content=json.dumps({"ok": True, "hits": [
            {"ref": 2, "quote": "覆盖管理员和员工两种角色"}]}),
            tool_call_id="t2", name="search_knowledge")
        hint = turn_filter.conflict_hint("权限验证覆盖哪几类账号？", [first, second])
        self.assertIsNotNone(hint)
        self.assertIsNone(turn_filter.conflict_hint("发布范围是什么？", [first, second]))

    def test_single_entry_is_left_alone(self):
        payload = {"ok": True, "hits": [{"ref": 1, "quote": "a"}]}
        message = ToolMessage(content=json.dumps(payload), tool_call_id="t1",
                              name="search_knowledge")
        self.assertEqual(turn_filter.collect_entries([message])[0]["ref"], 1)


if __name__ == "__main__":
    unittest.main()
