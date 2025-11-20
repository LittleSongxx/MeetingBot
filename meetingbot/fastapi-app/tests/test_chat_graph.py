"""问答状态图与引用登记的单测：零模型、零数据库、零向量库。

钉住的四组行为：
- **三道硬闸**都要能停：轮数、token/成本预算、停滞；且停止原因如实写进状态；
- **正常路径**：模型要工具 → 工具结果进消息 → 模型给答案 → 结束（原因 finished）；
- **引用登记**：按身份去重、编号只增不改、越界编号被识别为 unknown；
- **答案与轨迹的读取**：停在硬闸上时不许把工具结果的 JSON 当答案。
"""

from __future__ import annotations

import unittest
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from services import chat_graph, citations


def _ai(text: str = "", calls: list[dict] | None = None, tokens: int = 10) -> AIMessage:
    message = AIMessage(content=text)
    if calls:
        message.tool_calls = calls
    message.usage_metadata = {"input_tokens": tokens // 2, "output_tokens": tokens - tokens // 2,
                              "total_tokens": tokens}
    return message


def _call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class BudgetTest(unittest.TestCase):
    def test_env_defaults_are_sane(self):
        budget = chat_graph.budget_from_env()
        self.assertEqual(budget["max_rounds"], 6)
        self.assertEqual(budget["max_stall"], 3)
        self.assertGreater(budget["max_tokens"], 0)

    def test_bad_env_is_rejected_loudly(self):
        import os
        os.environ["CHAT_MAX_ROUNDS"] = "abc"
        try:
            with self.assertRaises(ValueError):
                chat_graph.budget_from_env()
        finally:
            del os.environ["CHAT_MAX_ROUNDS"]


class GateTest(unittest.TestCase):
    """三道闸的判定是纯函数，先单独钉住它们。"""

    BUDGET = {"max_rounds": 3, "max_tool_calls": 4, "max_tokens": 100,
              "max_cost_micro": 500, "max_stall": 2}

    def _state(self, **kwargs) -> dict:
        state = {"budget": self.BUDGET}
        state.update(kwargs)
        return state

    def test_no_reason_when_everything_is_within_budget(self):
        self.assertEqual(chat_graph.gate_reason(self._state(round_no=1, tokens_used=10)), "")

    def test_rounds_gate(self):
        self.assertEqual(chat_graph.gate_reason(self._state(round_no=3)), chat_graph.STOP_MAX_ROUNDS)

    def test_token_gate(self):
        self.assertEqual(chat_graph.gate_reason(self._state(tokens_used=100)),
                         chat_graph.STOP_BUDGET_TOKENS)

    def test_cost_gate_needs_its_own_limit(self):
        state = self._state(cost_micro=999)
        self.assertEqual(chat_graph.gate_reason(state), chat_graph.STOP_BUDGET_COST)
        # 限为 0 表示不按成本设限
        state["budget"] = {**self.BUDGET, "max_cost_micro": 0}
        self.assertEqual(chat_graph.gate_reason(state), "")

    def test_tool_call_gate(self):
        self.assertEqual(chat_graph.gate_reason(self._state(tool_calls=4)),
                         chat_graph.STOP_BUDGET_TOOLS)

    def test_stall_gate(self):
        self.assertEqual(chat_graph.gate_reason(self._state(stall_streak=2)),
                         chat_graph.STOP_STALLED)
        # 阈值 0 = 关掉停滞检测
        self.assertEqual(chat_graph.gate_reason(
            self._state(stall_streak=9, budget={**self.BUDGET, "max_stall": 0})), "")


class GraphRunTest(unittest.IsolatedAsyncioTestCase):
    """整图跑通：注入的模型脚本决定路径，工具用真 ToolNode 执行。"""

    async def asyncSetUp(self):
        self.calls: list[str] = []

        async def search(query: str) -> str:
            self.calls.append(query)
            return '{"ok": true, "hits": []}'

        self.tool = _plain_tool(search)

    def _graph(self, script: list[Any], tools: Any, budget: dict | None = None):
        from langgraph.prebuilt import ToolNode

        steps = list(script)
        node = ToolNode([tools])

        async def call_model(messages):
            self.messages_seen = list(messages)
            return steps.pop(0)

        async def run_tools(state):
            return await node.ainvoke(state)

        return chat_graph.build_chat_graph(), {
            "call_model": call_model,
            "tools": run_tools,
            "budget": budget or {"max_rounds": 4, "max_tool_calls": 4, "max_tokens": 1000,
                                 "max_cost_micro": 0, "max_stall": 3},
        }

    async def _run(self, graph, injected, messages):
        return await graph.ainvoke(
            {"messages": messages, **injected},
            config={"recursion_limit": 10})

    async def test_answer_without_tools_is_nudged_once_then_accepted(self):
        """直接作答会被提醒一次（提醒后仍直接作答则接受，由上层标为 no_retrieval）。"""
        graph, injected = self._graph(
            [_ai("没有相关问题。"), _ai("抱歉，我这边没有相关资料。")], self.tool)
        state = await self._run(graph, injected, [HumanMessage("今天天气如何")])
        self.assertTrue(state["nudged"], "首轮没调工具必须提醒过")
        self.assertEqual(state["round_no"], 2, "提醒后应再跑一轮")
        self.assertEqual(state["tool_calls"], 0)
        self.assertEqual(chat_graph.final_answer(state), "抱歉，我这边没有相关资料。")

    async def test_nudge_only_happens_once(self):
        """提醒只发一次：否则会与模型来回拉锯，把预算烧在没有进展的轮次上。"""
        script = [_ai("第一次直接作答。"), _ai("第二次仍然直接作答。"),
                  _ai("第三次不该被调用。")]
        graph, injected = self._graph(script, self.tool)
        state = await self._run(graph, injected, [HumanMessage("问题")])
        self.assertEqual(state["round_no"], 2, "提醒后不应再提醒")
        self.assertEqual(state["stop_reason"], chat_graph.STOP_FINISHED)

    async def test_nudged_model_finds_tools_and_uses_them(self):
        """提醒起作用的路径：第二轮开始调工具，工具结果回到模型，最终给带依据的答案。"""
        graph, injected = self._graph([
            _ai("会议里没有这件事。"),
            _ai("", calls=[_call("search_knowledge", {"query": "上线时间"})]),
            _ai("上线时间定在十月[1]。"),
        ], self.tool)
        state = await self._run(graph, injected, [HumanMessage("上线时间？")])
        self.assertEqual(self.calls, ["上线时间"], "提醒之后应该真的去检索了")
        self.assertEqual(state["tool_calls"], 1)
        self.assertEqual(chat_graph.final_answer(state), "上线时间定在十月[1]。")

    async def test_tool_round_trip_then_answer(self):
        graph, injected = self._graph([
            _ai("", calls=[_call("search_knowledge", {"query": "上线时间"})]),
            _ai("上线时间定在十月[1]。"),
        ], self.tool)
        state = await self._run(graph, injected, [HumanMessage("上线时间？")])
        self.assertEqual(self.calls, ["上线时间"])
        self.assertEqual(chat_graph.final_answer(state), "上线时间定在十月[1]。")
        self.assertEqual(state["tool_calls"], 1)
        self.assertEqual(state["round_no"], 2)
        self.assertEqual(state["stop_reason"], chat_graph.STOP_FINISHED)

    async def test_max_rounds_stops_before_another_model_call(self):
        # 模型每轮都要调工具：轮数上限必须由代码兜住
        script = [_ai("", calls=[_call("search_knowledge", {"query": f"q{i}"})], tokens=1)
                  for i in range(10)]
        graph, injected = self._graph(script, self.tool,
                                      budget={"max_rounds": 2, "max_tool_calls": 99,
                                              "max_tokens": 10_000, "max_cost_micro": 0,
                                              "max_stall": 0})
        state = await self._run(graph, injected, [HumanMessage("查一下")])
        self.assertEqual(state["round_no"], 2)
        self.assertEqual(state["stop_reason"], chat_graph.STOP_MAX_ROUNDS)
        self.assertEqual(len(self.calls), 1)

    async def test_token_budget_stops_the_loop(self):
        script = [_ai("", calls=[_call("search_knowledge", {"query": f"q{i}"})], tokens=60)
                  for i in range(10)]
        graph, injected = self._graph(script, self.tool,
                                      budget={"max_rounds": 99, "max_tool_calls": 99,
                                              "max_tokens": 100, "max_cost_micro": 0,
                                              "max_stall": 0})
        state = await self._run(graph, injected, [HumanMessage("查一下")])
        self.assertEqual(state["stop_reason"], chat_graph.STOP_BUDGET_TOKENS)
        self.assertGreaterEqual(state["tokens_used"], 100)

    async def test_cost_budget_uses_injected_converter(self):
        script = [_ai("", calls=[_call("search_knowledge", {"query": f"q{i}"})], tokens=1)
                  for i in range(10)]
        graph, injected = self._graph(script, self.tool,
                                      budget={"max_rounds": 99, "max_tool_calls": 99,
                                              "max_tokens": 10_000, "max_cost_micro": 3,
                                              "max_stall": 0})
        injected["cost_micro_of"] = lambda usage: 2
        state = await self._run(graph, injected, [HumanMessage("查一下")])
        self.assertEqual(state["stop_reason"], chat_graph.STOP_BUDGET_COST)
        self.assertEqual(state["cost_micro"], 4)

    async def test_identical_repeated_call_is_detected_as_stall(self):
        script = [_ai("", calls=[_call("search_knowledge", {"query": "同一个词"})], tokens=1)
                  for _ in range(10)]
        graph, injected = self._graph(script, self.tool,
                                      budget={"max_rounds": 99, "max_tool_calls": 99,
                                              "max_tokens": 10_000, "max_cost_micro": 0,
                                              "max_stall": 3})
        state = await self._run(graph, injected, [HumanMessage("查一下")])
        self.assertEqual(state["stop_reason"], chat_graph.STOP_STALLED)
        # 第 3 次重复在**调用之前**就被拦住：只执行了两次
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(state["stall_streak"], 3)

    async def test_different_arguments_are_not_a_stall(self):
        script = [_ai("", calls=[_call("search_knowledge", {"query": f"第{i}个词"})], tokens=1)
                  for i in range(2)] + [_ai("够了。", tokens=1)]
        graph, injected = self._graph(script, self.tool,
                                      budget={"max_rounds": 9, "max_tool_calls": 9,
                                              "max_tokens": 10_000, "max_cost_micro": 0,
                                              "max_stall": 2})
        state = await self._run(graph, injected, [HumanMessage("查一下")])
        self.assertEqual(state["stop_reason"], chat_graph.STOP_FINISHED)
        self.assertEqual(len(self.calls), 2)

    async def test_final_answer_is_empty_when_stopped_on_a_tool_result(self):
        script = [_ai("", calls=[_call("search_knowledge", {"query": f"q{i}"})], tokens=1)
                  for i in range(4)]
        graph, injected = self._graph(script, self.tool,
                                      budget={"max_rounds": 1, "max_tool_calls": 9,
                                              "max_tokens": 10_000, "max_cost_micro": 0,
                                              "max_stall": 0})
        state = await self._run(graph, injected, [HumanMessage("查一下")])
        # 最后一条消息是工具结果：绝不能把它的 JSON 当答案
        self.assertEqual(chat_graph.final_answer(state), "")


class ToolTraceTest(unittest.TestCase):
    def test_trace_pairs_calls_with_results(self):
        messages = [
            HumanMessage("有哪些待办"),
            _ai("", calls=[_call("list_facts", {"kind": "pending_item"}, call_id="a1")]),
            ToolMessage(content='{"ok": true, "facts": [1, 2, 3], "kind": "pending_item"}',
                        name="list_facts", tool_call_id="a1"),
            _ai("有三条[1]。"),
        ]
        trace = chat_graph.tool_trace(messages)
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["name"], "list_facts")
        self.assertEqual(trace[0]["arguments"], {"kind": "pending_item"})
        self.assertEqual(trace[0]["summary"], "3 条事实")
        self.assertTrue(trace[0]["ok"])

    def test_failed_tool_is_reported_as_failure(self):
        messages = [
            _ai("", calls=[_call("search_knowledge", {"query": "x"}, call_id="b1")]),
            ToolMessage(content='{"ok": false, "error": "向量库不可达"}', name="search_knowledge",
                        tool_call_id="b1"),
        ]
        trace = chat_graph.tool_trace(messages)
        self.assertFalse(trace[0]["ok"])
        self.assertIn("失败", trace[0]["summary"])


class CitationRegistryTest(unittest.TestCase):
    def _search_result(self) -> dict:
        return {"ok": True, "query": "上线", "hits": [
            {"chunk_id": 11, "meeting_id": 3, "quote": "十月上线", "speaker": "张晨",
             "start_second": 12, "authority": "RAW", "meeting_title": "评审会"},
            {"chunk_id": 12, "meeting_id": 3, "quote": "继续灰度", "speaker": "李月",
             "start_second": 40, "authority": "CONFIRMED", "meeting_title": "评审会"},
        ]}

    def test_refs_are_assigned_in_order_and_deduped(self):
        registry = citations.CitationRegistry()
        first = registry.annotate("search_knowledge", self._search_result())
        self.assertEqual([hit["ref"] for hit in first["hits"]], [1, 2])
        # 同一片切片再检索到一次：编号不变，登记表也不增长
        again = registry.annotate("search_knowledge", self._search_result())
        self.assertEqual([hit["ref"] for hit in again["hits"]], [1, 2])
        self.assertEqual(len(registry), 2)

    def test_refs_never_renumber(self):
        registry = citations.CitationRegistry()
        result = self._search_result()
        registry.annotate("search_knowledge", result)
        facts = registry.annotate("list_facts", {"ok": True, "kind": "decision", "facts": [
            {"meeting_id": 3, "minutes_id": 5, "field": "decisions", "item_index": 0,
             "authority": "CONFIRMED", "meeting_title": "评审会",
             "item": {"content": {"text": "决议正文"}, "evidence": [{"quote": "原句"}]}},
        ]})
        self.assertEqual(facts["facts"][0]["ref"], 3)
        self.assertEqual(registry.get(1).chunk_id, 11)
        self.assertNotIn("_citation", facts["facts"][0], "登记完不能把内部对象留在结果里")
        self.assertEqual(len(registry), 3)

    def test_failed_result_is_not_registered(self):
        registry = citations.CitationRegistry()
        result = registry.annotate("search_knowledge", {"ok": False, "error": "挂了"})
        self.assertEqual(len(registry), 0)
        self.assertEqual(result["error"], "挂了")

    def test_fact_citation_prefers_evidence_quote(self):
        registry = citations.CitationRegistry()
        registry.annotate("list_facts", {"ok": True, "kind": "decision", "facts": [
            {"meeting_id": 3, "minutes_id": 5, "field": "decisions", "item_index": 0,
             "authority": "CONFIRMED", "meeting_title": "评审会",
             "item": {"content": {"text": "改写过的正文"},
                      "evidence": [{"quote": "转写里的原句", "speaker": "张晨"}]}},
        ]})
        self.assertEqual(registry.get(1).quote, "转写里的原句")

    def test_cited_and_unknown_refs(self):
        registry = citations.CitationRegistry()
        registry.annotate("search_knowledge", self._search_result())
        text = "结论是十月上线[1]，另外[9]存疑。"
        self.assertEqual(citations.cited_refs(text), [1, 9])
        self.assertEqual(citations.unknown_refs(text, registry), [9])
        self.assertEqual([c.ref for c in citations.used_citations(text, registry)], [1])
        self.assertEqual(citations.strip_refs(text), "结论是十月上线，另外存疑。")

    def test_annotation_does_not_leak_objects_into_the_json(self):
        import json
        registry = citations.CitationRegistry()
        result = registry.annotate("search_knowledge", self._search_result())
        # 结果要能原样 json.dumps 进 tool message（不能塞进 Citation 对象）
        payload = json.dumps(result, ensure_ascii=False)
        self.assertIn('"ref": 1', payload)
        self.assertNotIn("Citation", payload)

    def test_segment_citations_are_numbered_after_hits(self):
        registry = citations.CitationRegistry()
        registry.annotate("search_knowledge", self._search_result())
        result = registry.annotate("get_transcript_excerpt", {
            "ok": True, "task_id": 7, "meeting": {"id": 3, "title": "评审会"},
            "segments": [{"segment_no": 4, "speaker": "张晨", "start_second": 12,
                          "text": "十月上线"}]})
        self.assertEqual(result["segments"][0]["ref"], 3)
        citation = registry.get(3)
        self.assertEqual((citation.kind, citation.task_id, citation.segment_no),
                         (citations.KIND_SEGMENT, 7, 4))


def _plain_tool(coroutine) -> Any:
    """把普通协程包成 ToolNode 认的 BaseTool（只依赖 langchain_core，不建 pydantic 模型）。

    参数用 `query` 一个字段就够——这个测试关心的是图的走向，不是工具参数校验
    （参数校验在 test_kb_tools.py 里钉）。
    """
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class SearchArgs(BaseModel):
        query: str = Field(default="")

    return StructuredTool.from_function(
        coroutine=coroutine, name="search_knowledge", description="检索",
        args_schema=SearchArgs)


if __name__ == "__main__":
    unittest.main()


class MinutesCitationTest(unittest.TestCase):
    """整份纪要的条目也要能被引用：这条路径原先不登记，模型只能编编号。"""

    def test_minutes_items_get_refs(self):
        registry = citations.CitationRegistry()
        result = registry.annotate("get_minutes", {
            "ok": True, "meeting": {"id": 2, "title": "产品发布评审会议"},
            "minutes_id": 3, "authority": "CONFIRMED",
            "fields": {
                "summary": "一句话摘要",
                "decisions": [
                    {"content": {"text": "九月一日发布"}, "origin": "stated",
                     "evidence": [{"quote": "就定在九月一日", "speaker": "李月"}]},
                ],
                "risks": [
                    {"content": {"text": "导出超时"}, "level": "HIGH", "origin": "stated",
                     "evidence": []},
                ],
            },
        })
        decisions = result["fields"]["decisions"]
        self.assertEqual(decisions[0]["ref"], 1, "条目上必须带 ref，模型才有编号可用")
        self.assertEqual(result["fields"]["risks"][0]["ref"], 2)
        citation = registry.get(1)
        self.assertEqual((citation.kind, citation.minutes_id, citation.field, citation.item_index),
                         (citations.KIND_FACT, 3, "decisions", 0))
        # 引用文本优先取证据原句（能落回转写），而不是条目正文
        self.assertEqual(citation.quote, "就定在九月一日")
        self.assertEqual(citation.speaker, "李月")
        self.assertEqual(citation.authority, "CONFIRMED")

    def test_same_item_via_list_facts_and_get_minutes_shares_a_ref(self):
        """两条工具路径引用同一条目时编号相同：去重身份是"哪条纪要的哪一条"。"""
        registry = citations.CitationRegistry()
        registry.annotate("list_facts", {"ok": True, "kind": "decision", "facts": [
            {"meeting_id": 2, "minutes_id": 3, "field": "decisions", "item_index": 0,
             "authority": "CONFIRMED", "meeting_title": "产品发布评审会议",
             "item": {"content": {"text": "九月一日发布"}, "evidence": [{"quote": "就定在九月一日"}]}},
        ]})
        result = registry.annotate("get_minutes", {
            "ok": True, "meeting": {"id": 2, "title": "产品发布评审会议"},
            "minutes_id": 3, "authority": "CONFIRMED",
            "fields": {"decisions": [{"content": {"text": "九月一日发布"},
                                      "evidence": [{"quote": "就定在九月一日"}]}]},
        })
        self.assertEqual(result["fields"]["decisions"][0]["ref"], 1, "同一内容不该占两个编号")
        self.assertEqual(len(registry), 1)

    def test_summary_is_not_registered_as_an_item(self):
        registry = citations.CitationRegistry()
        result = registry.annotate("get_minutes", {
            "ok": True, "meeting": {"id": 2}, "minutes_id": 3, "authority": "CONFIRMED",
            "fields": {"summary": "整段摘要文本", "decisions": []},
        })
        self.assertEqual(result["fields"]["summary"], "整段摘要文本", "摘要保持字符串形态")
        self.assertEqual(len(registry), 0)
