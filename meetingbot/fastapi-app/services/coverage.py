"""覆盖计划与证据状态（CoveragePlan / EvidenceState）。

把原先散在 `chat_service` 里的“跨录音正则 + 提示词规则”收成可测试的数据结构：
- 问题需要哪些来源（会议/录音段）；
- 回答需要哪些槽位（负责人/截止时间/时间点/数量）；
- **系列/状态问题需要查哪些状态类型**（decision / action_item / recording）；
- 本轮已经覆盖了哪些来源、槽位、状态类型，哪些仍缺。

它是**确定性**的：默认不额外调模型。query plan 开启时用它的意图/子问题；
否则用集中在这里的动作标记。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# 强跨录音标记：与产品现有口径一致（裸“分别”不算，避免把同一段里的多问句
# 误判成跨录音，把另一段的噪声带进来）。
_CROSS = re.compile(r"两段|两场|各自|一致|上午.*下午|下午.*上午")
_SLOT_WORDS = {
    "owner": ("谁负责", "负责人", "谁来做", "由谁", "owner"),
    "deadline": ("截止", "什么时候完成", "何时完成", "deadline", "期限"),
}
# 状态/系列线索：命中的类型会成为“本轮应检查的状态类型”。
_STATE_KINDS = {
    "decision": ("决策", "决定", "发布", "确认", "候选", "草稿", "生效", "替代", "修改"),
    "action_item": ("待办", "任务", "负责人", "截止", "状态", "开放", "进度", "行动项"),
    "recording": ("录音", "系列", "时间顺序", "场次"),
}
_CONFIRM_CUES = ("确认", "草稿", "候选", "生效")
_SERIES_CUE = "系列"
# 同一个槽位可能在多条事实里出现：任一条给出实质依据就算覆盖，
# 不能被后面一条 not_mentioned 覆盖掉。
_MISSING_BASES = {None, "missing", "not_mentioned", "open_ended", "model_inferred"}
HINT_PREFIX = "【覆盖检查】"

_STATE_HINTS = {
    "decision": "query_meeting_state(kind=decision, include_proposed=true)（区分已确认/候选）",
    "action_item": "query_meeting_state(kind=action_item, status=all)",
    "recording": "list_facts(kind=recording) 或 query_meeting_state(kind=decision) 确认系列范围",
}


@dataclass(frozen=True)
class CoveragePlan:
    intent: str = "single_fact"
    confidence: float | None = None
    requires_retrieval: bool = True
    rewritten: str | None = None
    sub_questions: tuple[str, ...] = ()
    required_sources: tuple[int, ...] = ()
    required_slots: tuple[str, ...] = ()
    # 系列/状态覆盖（state_aware 开启时才填）
    required_state_kinds: tuple[str, ...] = ()
    requires_confirmation_check: bool = False
    series_ids: tuple[int, ...] = ()
    meeting_ids: tuple[int, ...] = ()

    @property
    def is_cross_source(self) -> bool:
        return self.intent == "cross_meeting" or len(self.required_sources) >= 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent, "confidence": self.confidence,
            "requires_retrieval": self.requires_retrieval,
            "rewritten": self.rewritten, "sub_questions": list(self.sub_questions),
            "required_sources": list(self.required_sources),
            "required_slots": list(self.required_slots),
            "required_state_kinds": list(self.required_state_kinds),
            "requires_confirmation_check": self.requires_confirmation_check,
            "series_ids": list(self.series_ids),
            "meeting_ids": list(self.meeting_ids),
        }


@dataclass
class EvidenceState:
    plan: CoveragePlan
    covered_sources: set[int] = field(default_factory=set)
    slot_status: dict[str, str] = field(default_factory=dict)
    # 只有真正返回过结构化槽位的工具（list_facts/query_meeting_state）才允许发槽位提示；
    # 纯 search_knowledge 轮次不应被误判成“槽位缺失”。
    slot_observed: bool = False
    # 本轮已经通过 query_meeting_state 覆盖的状态类型
    covered_state_kinds: set[str] = field(default_factory=set)
    state_items_seen: int = 0
    minutes_observed: bool = False
    draft_observed: bool = False
    attempts: int = 0

    @property
    def missing_sources(self) -> list[int]:
        return [sid for sid in self.plan.required_sources
                if sid not in self.covered_sources]

    @property
    def missing_slots(self) -> list[str]:
        if not self.slot_observed:
            return []
        return [name for name in self.plan.required_slots
                if self.slot_status.get(name) in
                (None, "missing", "not_mentioned", "open_ended", "model_inferred")]

    @property
    def missing_state_kinds(self) -> list[str]:
        return [name for name in self.plan.required_state_kinds
                if name not in self.covered_state_kinds]

    def _confirmation_unresolved(self) -> bool:
        # 确认/草稿问题：只要查过 decision（含 proposed）或读过纪要（含草稿）就算覆盖
        return (self.plan.requires_confirmation_check
                and "decision" not in self.covered_state_kinds
                and not self.minutes_observed)

    def needs_supplement(self) -> bool:
        missing = (self.missing_sources or self.missing_slots
                   or self.missing_state_kinds or self._confirmation_unresolved())
        return bool(missing) and self.attempts < 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.as_dict(),
            "covered_sources": sorted(self.covered_sources),
            "missing_sources": self.missing_sources,
            "slot_status": dict(self.slot_status),
            "missing_slots": self.missing_slots,
            "covered_state_kinds": sorted(self.covered_state_kinds),
            "missing_state_kinds": self.missing_state_kinds,
            "state_items_seen": self.state_items_seen,
            "minutes_observed": self.minutes_observed,
            "draft_observed": self.draft_observed,
            "attempts": self.attempts,
        }


def _slots_in(question: str) -> tuple[str, ...]:
    found: list[str] = []
    for name, words in _SLOT_WORDS.items():
        if any(word in (question or "") for word in words):
            found.append(name)
    return tuple(found)


def _state_kinds_in(question: str) -> tuple[str, ...]:
    found: list[str] = []
    for kind, cues in _STATE_KINDS.items():
        if any(cue in (question or "") for cue in cues):
            found.append(kind)
    return tuple(found)


def build_plan(question: str, *, plan: dict[str, Any] | None = None,
               recordings: Iterable[int] = (), state_aware: bool = False,
               series_ids: Iterable[int] = (), meeting_ids: Iterable[int] = ()
               ) -> CoveragePlan:
    """由 query plan（可空）与录音清单构造覆盖计划。"""
    recording_ids = tuple(sorted({int(i) for i in recordings}))
    series = tuple(sorted({int(i) for i in series_ids}))
    meetings = tuple(sorted({int(i) for i in meeting_ids}))
    intent = str((plan or {}).get("intent") or "").strip()
    if not intent:
        intent = "cross_meeting" if _CROSS.search(question or "") else "single_fact"
    requires = bool((plan or {}).get("needs_retrieval", True))
    sub_questions = tuple(str(item) for item in ((plan or {}).get("sub_questions") or []))
    required_sources: tuple[int, ...] = ()
    if intent == "cross_meeting" and len(recording_ids) >= 2:
        required_sources = recording_ids
    state_kinds: tuple[str, ...] = ()
    confirmation = False
    if state_aware:
        state_kinds = _state_kinds_in(question)
        # 系列问题：没写“状态”也至少要求确认系列范围（recording 类）
        if _SERIES_CUE in (question or "") and "recording" not in state_kinds:
            state_kinds = tuple(dict.fromkeys((*state_kinds, "recording")))
        confirmation = any(cue in (question or "") for cue in _CONFIRM_CUES)
    return CoveragePlan(
        intent=intent,
        confidence=(plan or {}).get("confidence"),
        requires_retrieval=requires,
        rewritten=(plan or {}).get("rewritten"),
        sub_questions=sub_questions,
        required_sources=required_sources,
        required_slots=_slots_in(question),
        required_state_kinds=state_kinds,
        requires_confirmation_check=confirmation,
        series_ids=series,
        meeting_ids=meetings,
    )


def observe_tool_result(state: EvidenceState, tool_name: str,
                        result: dict[str, Any]) -> EvidenceState:
    """从工具结果更新覆盖状态。只认结构化字段，不解析自然语言。"""
    if not result.get("ok"):
        return state
    if tool_name == "search_knowledge":
        for hit in result.get("hits") or []:
            if hit.get("chunk_type") != "TRANSCRIPT_WINDOW":
                continue
            sid = hit.get("source_id")
            if sid is not None:
                state.covered_sources.add(int(sid))
    elif tool_name == "get_transcript_excerpt":
        task_id = result.get("task_id")
        if task_id is not None and result.get("segments"):
            state.covered_sources.add(int(task_id))
    elif tool_name == "list_facts":
        state.slot_observed = True
        for fact in result.get("facts") or []:
            status = fact.get("slot_status") or {}
            for name, value in status.items():
                if not isinstance(value, dict):
                    continue
                basis = str(value.get("basis") or "missing")
                current = state.slot_status.get(name)
                if current is None or (current in _MISSING_BASES and basis not in _MISSING_BASES):
                    state.slot_status[name] = basis
    elif tool_name == "query_meeting_state":
        kind = str(result.get("kind") or "").strip()
        items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
        if kind:
            state.covered_state_kinds.add(kind)
        state.state_items_seen += len(items)
        if items:
            state.slot_observed = True
        for item in items:
            owner = item.get("owner_name") or item.get("owner")
            deadline = item.get("deadline_text") or item.get("deadline")
            status = item.get("status")
            if owner:
                state.slot_status["owner"] = "explicit"
            if deadline:
                state.slot_status["deadline"] = "explicit"
            if status:
                state.slot_status["status"] = "explicit"
    elif tool_name == "get_minutes":
        state.minutes_observed = bool(result.get("minutes") or result.get("items")
                                      or result.get("decisions") or result.get("pending_items"))
        statuses = [str(item.get("status") or "") for item in (result.get("items") or [])
                    if isinstance(item, dict)]
        if any(status.upper() == "DRAFT" for status in statuses):
            state.draft_observed = True
    return state


def supplement_hint(state: EvidenceState) -> str:
    """把缺口写成一条可执行的提示（不替模型下结论）。"""
    parts: list[str] = []
    if state.missing_sources:
        parts.append(
            "本轮尚未覆盖录音来源 " + "、".join(str(i) for i in state.missing_sources)
            + "；请用 search_knowledge 的 source_ids 分别检索这些录音，再合并回答。")
    if state.missing_slots:
        parts.append(
            "以下槽位在已返回资料里缺失："
            + "、".join(state.missing_slots)
            + "；请用 search_knowledge 到转写原话补查，不要猜。")
    if state.missing_state_kinds:
        hints = "；".join(_STATE_HINTS.get(kind, kind) for kind in state.missing_state_kinds)
        scope = (f"（本会话系列范围 series_id="
                 f"{','.join(str(i) for i in state.plan.series_ids)}）"
                 if state.plan.series_ids else "")
        parts.append(f"本问题需要核对状态，但还没查：{hints}{scope}。")
    if state._confirmation_unresolved():
        parts.append(
            "问题涉及“已确认/候选/草稿/生效”：请用 query_meeting_state(kind=decision, "
            "include_proposed=true) 区分已确认与候选，并用 get_minutes(include_draft=true) "
            "核对草稿状态；不要把草稿当已确认事实。")
    return f"{HINT_PREFIX} {' '.join(parts)}" if parts else ""


__all__ = ["CoveragePlan", "EvidenceState", "build_plan", "observe_tool_result",
           "supplement_hint", "HINT_PREFIX"]
