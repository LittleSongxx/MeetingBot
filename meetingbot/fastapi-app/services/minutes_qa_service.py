"""单场会议问答：检索本场转写片段 + 纪要内容，要求模型只依据材料回答并给出引用。"""

from __future__ import annotations

import json
import logging

from models import Meeting, MeetingMinutes

from services.evidence_service import (
    collect_evidence_segment_nos,
    load_task_evidence_context,
    normalize,
)
from services.minutes_service import render_prompt
from services.model_factory import invoke_json
from services.transcript_search import build_context, rank_segments

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 3
MAX_HISTORY_CHARS = 500
MAX_QUESTION_CHARS = 500
# 除检索命中的片段外，额外把纪要证据涉及的片段带进上下文，保证答案可引用
MAX_CONTEXT_SEGMENTS = 8
# 引用上限按问题复杂度动态决定：单点问题 1 条，多点问题 2~3 条。
MAX_QA_CITATIONS = 3

QA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "found": {"type": "boolean"},
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "maxItems": MAX_QA_CITATIONS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "segment_no": {"type": "integer"},
                    "quote": {"type": "string"},
                },
                "required": ["segment_no", "quote"],
            },
        },
    },
    "required": ["found", "answer", "citations"],
}


def minutes_payload(minutes: MeetingMinutes) -> dict:
    return {
        "summary": minutes.summary or "",
        "topics": minutes.topics_json or [],
        "viewpoints": minutes.viewpoints_json or [],
        "decisions": minutes.decisions_json or [],
        "pending_items": minutes.pending_items_json or [],
        "risks": minutes.risks_json or [],
    }


def format_history(history: list[dict] | None) -> str:
    """把最近几轮对话拼成提示词里的文本，限制轮数与长度，避免上下文膨胀。"""
    if not history:
        return "（无）"
    lines = []
    for message in history[-MAX_HISTORY_TURNS * 2:]:
        if not isinstance(message, dict):
            continue
        role = "用户" if message.get("role") == "user" else "助手"
        content = str(message.get("content") or "")[:MAX_HISTORY_CHARS]
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines) or "（无）"


def _bigrams(text: str) -> set[str]:
    normalized = normalize(text)
    return {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))}


def _citation_limit(question: str) -> int:
    """单点问题 1 条；包含“分别/各/以及/多个顿号/和”的多点问题放宽到 2~3 条。"""
    text = question or ""
    if any(marker in text for marker in ("分别", "各", "以及", "、", "；", ";")):
        return 3
    if "和" in text:
        return 2
    return 1


def _citation_relevance(citation: dict, question: str, answer: str) -> float:
    """引用与“问题 + 答案”的字符二元重叠，用来在多引用里选最直接支持答案的。"""
    target = _bigrams(f"{question} {answer}")
    if not target:
        return 0.0
    return len(target & _bigrams(str(citation.get("quote") or ""))) / len(target)


def _validate_citations(
    raw_citations,
    by_no: dict[int, dict],
    question: str = "",
    answer: str = "",
) -> list[dict]:
    citations = []
    for raw in raw_citations or []:
        if not isinstance(raw, dict):
            continue
        try:
            segment_no = int(raw.get("segment_no"))
        except (TypeError, ValueError):
            continue
        segment = by_no.get(segment_no)
        quote = str(raw.get("quote") or "").strip()
        if segment is None or len(normalize(quote)) < 2:
            continue
        if normalize(quote) not in segment["norm"]:
            continue
        citations.append(
            {
                "segment_no": segment_no,
                "start_ms": segment["start_ms"],
                "end_ms": segment["end_ms"],
                "speaker": segment["speaker"],
                "quote": quote,
            }
        )
    citations.sort(key=lambda item: _citation_relevance(item, question, answer), reverse=True)
    return citations[:min(MAX_QA_CITATIONS, _citation_limit(question))]


async def ask(
    minutes: MeetingMinutes,
    config,
    prompt,
    question: str,
    history: list[dict] | None = None,
) -> dict:
    """回答一个关于本场会议的问题；找不到依据时返回 found=false。"""
    meeting = await Meeting.get_or_none(id=minutes.meeting_id)
    segments = await load_task_evidence_context(minutes.source_task_id)
    ranked = rank_segments(question, segments)
    chosen = list(ranked)
    chosen_nos = {segment["segment_no"] for segment in chosen}
    evidence_nos = collect_evidence_segment_nos(minutes_payload(minutes))
    for segment in segments:
        if segment["segment_no"] in evidence_nos and segment["segment_no"] not in chosen_nos:
            chosen.append(segment)
            chosen_nos.add(segment["segment_no"])
    chosen = chosen[:MAX_CONTEXT_SEGMENTS]
    context = build_context(chosen)
    by_no = {segment["segment_no"]: segment for segment in chosen}

    user_prompt = render_prompt(
        prompt.user_prompt,
        meeting_title=(meeting.title if meeting else ""),
        minutes_json=json.dumps(minutes_payload(minutes), ensure_ascii=False),
        segments=context or "（本场没有可用的转写片段）",
        history=format_history(history),
        question=question[:MAX_QUESTION_CHARS],
    )
    result = await invoke_json(
        config,
        prompt.system_prompt,
        user_prompt,
        QA_SCHEMA,
        "MINUTES_QA",
        minutes.id,
        "纪要问答模型",
    )
    answer = str(result.get("answer") or "").strip()
    citations = _validate_citations(result.get("citations") or [], by_no, question, answer)
    found = bool(result.get("found"))
    if found and not citations:
        # 声称找到了但给不出可核对的引用，按“没有依据”处理，避免无出处回答
        found = False
        answer = "材料中未找到足够依据来回答这个问题。"
    if not found:
        citations = []
        answer = answer or "材料中未找到足够依据来回答这个问题。"
    return {"found": found, "answer": answer, "citations": citations}
