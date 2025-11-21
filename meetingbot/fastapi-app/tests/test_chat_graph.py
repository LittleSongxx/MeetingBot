"""问答状态图与引用登记的单测：零模型、零数据库、零向量库。

钉住的四组行为：
- **三道硬闸**都要能停：轮数、token/成本预算、停滞；且停止原因如实写进状态；
- **正常路径**：模型要工具 → 工具结果进消息 → 模型给答案 → 结束（原因 finished）；
- **引用登记**：按身份去重、编号只增不改、越界编号被识别为 unknown；
- **答案与轨迹的读取**：停在硬闸上时不许把工具结果的 JSON 当答案。
"""

from __future__ import annotations

import json
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

    async def test_stalled_turn_reports_where_it_got_stuck(self):
        """q26/q27 的形态：工具一直失败、模型原样重试，最后停在停滞闸上。

        停在闸门上是**正确**行为；这一条钉的是"停得说不清原因"这个缺陷：
        结局里必须带上是哪个工具、失败原因是什么。
        """

        async def failing_search(query: str) -> str:
            self.calls.append(query)
            return json.dumps({"ok": False, "error": "参数校验失败：meeting_ids 需要数组"},
                              ensure_ascii=False)

        graph, injected = self._graph(
            [_ai("", calls=[_call("search_knowledge", {"query": "同一个词"})], tokens=1)
             for _ in range(6)], _plain_tool(failing_search),
            budget={"max_rounds": 9, "max_tool_calls": 9, "max_tokens": 10_000,
                    "max_cost_micro": 0, "max_stall": 3})
        state = await self._run(graph, injected, [HumanMessage("下午这场会定了哪些行动项")])
        self.assertEqual(state["stop_reason"], chat_graph.STOP_STALLED)
        self.assertIn("search_knowledge", state["stop_detail"])
        self.assertIn("meeting_ids 需要数组", state["stop_detail"])
        text = chat_graph.stop_reason_text(state["stop_reason"], state["stop_detail"])
        self.assertIn("已停止继续检索", text)


class StopDetailTest(unittest.TestCase):
    """结局可读：停在硬闸上时要能说清"卡在哪"。

    实测背景（q26/q27）：停滞闸掐断后答案为空，页面上只有一句"已停止继续检索"，
    用户看不到工具其实一直卡在参数上。
    """

    def _tool_failure(self, name: str = "list_facts",
                      error: str = "参数校验失败：meeting_ids Input should be a valid list"):
        return ToolMessage(content=json.dumps({"ok": False, "error": error}),
                           name=name, tool_call_id="c1")

    def test_stall_detail_names_the_failing_tool_and_its_error(self):
        state = {"stop_reason": chat_graph.STOP_STALLED, "stall_streak": 3,
                 "messages": [HumanMessage("问"), self._tool_failure()]}
        detail = chat_graph.stop_detail(state)
        self.assertIn("list_facts", detail)
        self.assertIn("meeting_ids", detail)
        self.assertIn("3 次", detail)

    def test_stall_without_a_failing_tool_says_which_signature_repeated(self):
        state = {"stop_reason": chat_graph.STOP_STALLED, "stall_streak": 3,
                 "messages": [ToolMessage(content='{"ok": true, "hits": []}',
                                          name="search_knowledge", tool_call_id="c1")]}
        detail = chat_graph.stop_detail(state)
        self.assertIn("3 次", detail)
        self.assertNotIn("list_facts", detail)

    def test_last_tool_failure_reads_the_latest_failure_only(self):
        messages = [
            ToolMessage(content='{"ok": false, "error": "第一次失败"}',
                        name="search_knowledge", tool_call_id="c1"),
            ToolMessage(content='{"ok": true, "hits": []}', name="search_knowledge",
                        tool_call_id="c2"),
            ToolMessage(content='{"ok": false, "error": "最后一次失败"}',
                        name="search_knowledge", tool_call_id="c3"),
        ]
        self.assertEqual(chat_graph.last_tool_failure(messages)["error"], "最后一次失败")

    def test_last_tool_failure_is_none_when_nothing_failed(self):
        self.assertIsNone(chat_graph.last_tool_failure([HumanMessage("问")]))
        self.assertIsNone(chat_graph.last_tool_failure(
            [ToolMessage(content="不是 JSON", name="x", tool_call_id="c1")]))

    def test_gate_details_say_what_was_exhausted(self):
        self.assertIn("6 轮", chat_graph.stop_detail(
            {"stop_reason": chat_graph.STOP_MAX_ROUNDS, "round_no": 6}))
        self.assertIn("token", chat_graph.stop_detail(
            {"stop_reason": chat_graph.STOP_BUDGET_TOKENS, "tokens_used": 120_000}))
        self.assertEqual(chat_graph.stop_detail({"stop_reason": chat_graph.STOP_FINISHED}), "")

    def test_stop_reason_text_puts_the_cause_next_to_the_notice(self):
        text = chat_graph.stop_reason_text(chat_graph.STOP_STALLED, "工具「x」连续 3 次失败")
        self.assertIn(chat_graph.STOP_NOTICE[chat_graph.STOP_STALLED], text)
        self.assertIn("工具「x」连续 3 次失败", text)
        # 没有具体原因时就是原来那一句，不多一个空壳
        self.assertEqual(chat_graph.stop_reason_text(chat_graph.STOP_STALLED),
                         chat_graph.STOP_NOTICE[chat_graph.STOP_STALLED])


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

    def test_supplemental_hits_are_counted_in_the_summary(self):
        text = '{"ok": true, "hits": [1, 2], "coverage_hits": [{"ref": 3}]}'
        self.assertEqual(chat_graph.summarize_tool_result("search_knowledge", text),
                         "2 条切片 + 补搜 1 条")

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


class InventoryCitationTest(unittest.TestCase):
    """录音/会议清单也是可引用条目：不能所有行都合并成一个空引用。"""

    def test_recording_facts_get_unique_refs_and_period_text(self):
        registry = citations.CitationRegistry()
        registry.annotate("list_facts", {"ok": True, "kind": "recording", "facts": [
            {"task_id": 6, "meeting_id": 2, "meeting_title": "产品发布评审会议",
             "recorded_at": "2026-09-10 09:00:00", "period": "上午"},
            {"task_id": 7, "meeting_id": 2, "meeting_title": "产品发布评审会议",
             "recorded_at": "2026-09-10 14:00:00", "period": "下午"},
        ]})
        self.assertEqual(len(registry), 2, "两段录音必须占两个编号，不能合并")
        first, second = registry.get(1), registry.get(2)
        self.assertIn("task 6", first.quote)
        self.assertIn("上午", first.quote)
        self.assertIn("task 7", second.quote)
        self.assertIn("下午", second.quote)
        self.assertEqual((first.task_id, first.source_id), (6, 6))
        self.assertEqual(first.as_dict()["source_text"], first.quote)

    def test_meeting_facts_get_unique_refs(self):
        registry = citations.CitationRegistry()
        registry.annotate("list_facts", {"ok": True, "kind": "meeting", "facts": [
            {"meeting_id": 2, "title": "产品发布评审会议", "start_time": "2026-09-10 10:40:00"},
            {"meeting_id": 3, "title": "另一场会", "start_time": "2026-09-11 10:00:00"},
        ]})
        self.assertEqual(len(registry), 2)
        self.assertEqual(registry.get(1).meeting_id, 2)
        self.assertEqual(registry.get(2).meeting_id, 3)

    def test_filtered_minutes_item_keeps_original_item_index(self):
        registry = citations.CitationRegistry()
        registry.annotate("get_minutes", {
            "ok": True, "meeting": {"id": 2, "title": "产品发布评审会议"},
            "minutes_id": 3, "authority": "CONFIRMED",
            "fields": {"decisions": [
                {"content": {"text": "第九条决策"}, "item_index": 8,
                 "evidence": [{"quote": "第九条决策原话"}]},
            ]},
        })
        citation = registry.get(1)
        self.assertEqual(citation.item_index, 8, "过滤后的引用必须指回原始条第 9 条")


class FactSourceTextTest(unittest.TestCase):
    """事实条目的**完整正文**要进引用快照（判定者拿它核对槽位类断言）。

    实测背景（P1 复测）：表格里的"截止时间/负责人"来自类型化槽位，而引用快照只带
    证据原句，判定者看不到槽位 → 6 条断言被判"上下文没写"（不支撑）。
    与 `quote` 的分工是刻意的：quote 要能逐字落回原文，`source_text` 是核对用的全文。
    """

    def test_fact_citation_carries_the_item_text_with_slots(self):
        registry = citations.CitationRegistry()
        registry.annotate("list_facts", {"ok": True, "kind": "pending_item", "facts": [
            {"meeting_id": 2, "minutes_id": 3, "field": "pending_items", "item_index": 0,
             "authority": "CONFIRMED", "meeting_title": "产品发布评审会议",
             "item": {"content": {"text": "修复移动端附件下载进度问题"},
                      "owner_suggestion": "王工", "deadline_suggestion": "2023-08-30 18:00",
                      "evidence": [{"quote": "王工你这边本周五前处理掉", "speaker": "张晨"}]}},
        ]})
        citation = registry.get(1)
        self.assertEqual(citation.quote, "王工你这边本周五前处理掉", "quote 仍是证据原句")
        self.assertIn("修复移动端附件下载进度问题", citation.source_text)
        self.assertIn("负责人：王工", citation.source_text)
        self.assertIn("截止时间：2023-08-30 18:00", citation.source_text)
        # 快照里也带出去（判定包与页面都读它）
        self.assertIn("截止时间：2023-08-30 18:00", citation.as_dict()["source_text"])

    def test_minutes_items_carry_it_too(self):
        registry = citations.CitationRegistry()
        registry.annotate("get_minutes", {
            "ok": True, "meeting": {"id": 2, "title": "评审会"}, "minutes_id": 3,
            "authority": "CONFIRMED",
            "fields": {"risks": [{"content": {"text": "导出超时"}, "level": "HIGH",
                                  "evidence": [{"quote": "导出会超时"}]}]},
        })
        citation = registry.get(1)
        self.assertEqual(citation.quote, "导出会超时")
        self.assertIn("导出超时", citation.source_text)
        self.assertIn("等级：HIGH", citation.source_text)

    def test_chunk_citations_do_not_carry_a_second_copy(self):
        """切片引用的 quote 就是切片正文：再塞一份 source_text 只是把载荷翻倍。"""
        registry = citations.CitationRegistry()
        registry.annotate("search_knowledge", {"ok": True, "hits": [
            {"chunk_id": 1, "meeting_id": 2, "quote": "[51s][张晨] 灰度从 8 月 20 日开始",
             "authority": "RAW", "meeting_title": "评审会"},
        ]})
        self.assertNotIn("source_text", registry.get(1).as_dict())

    def test_empty_item_text_is_not_emitted(self):
        # 没有正文也没有槽位的条目：不写空字段（判定侧与今天的回退路径一致）
        registry = citations.CitationRegistry()
        registry.annotate("list_facts", {"ok": True, "kind": "meeting", "facts": [
            {"meeting_id": 2, "title": "", "start_time": None, "status": "FINISHED"},
        ]})
        self.assertNotIn("source_text", registry.get(1).as_dict())

    def test_entity_check_uses_the_item_text_as_haystack(self):
        """实体核查的 haystack 因此变宽——**这是有意的**：条目正文确实在检索结果里，
        表格里的数字不该被标成"检索结果里找不到"。"""
        from services import entity_check
        registry = citations.CitationRegistry()
        registry.annotate("list_facts", {"ok": True, "kind": "pending_item", "facts": [
            {"meeting_id": 2, "minutes_id": 3, "field": "pending_items", "item_index": 0,
             "authority": "CONFIRMED", "meeting_title": "评审会",
             "item": {"content": {"text": "修复下载进度问题"},
                      "deadline_suggestion": "2023-08-30 18:00"}},
        ]})
        flags = entity_check.check_entities("截止时间是 2023-08-30 18:00[1]。",
                                            registry.as_dicts())
        self.assertEqual(flags, [], "槽位里的数字就在检索结果里，不该被标注")



if __name__ == "__main__":
    unittest.main()
