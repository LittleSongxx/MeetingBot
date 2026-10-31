"""QMSum adapter（英文，query-based meeting summarization + relevant_text_span）。

数据来源与格式：
https://github.com/Yale-LILY/QMSum
示例：
  {
    "meeting_transcripts": [{"speaker": "...", "content": "..."}],
    "topic_list": [{"topic": "...", "relevant_text_span": [["0","19"]]}],
    "general_query_list": [{"query": "...", "answer": "..."}],
    "specific_query_list": [{"query": "...", "answer": "...", "relevant_text_span": [["137","150"]]}]
  }

本 adapter 保留 meeting 级 transcript，并把每个 query 转成带 gold evidence segment
集合的 case；general query 的 evidence_segments 为 None（对应整场会议）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluation.adapters import EvalCase


def _spans_to_segments(spans: list[list[Any]] | None, inclusive: bool = True) -> list[int]:
    result = []
    for span in spans or []:
        if not isinstance(span, (list, tuple)) or len(span) < 2:
            continue
        try:
            start = int(span[0])
            end = int(span[1])
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        stop = end + (1 if inclusive else 0)
        result.extend(range(start, stop))
    return sorted(set(result))


def normalize_qmsum(raw: dict, source: str = "qmsum") -> EvalCase:
    transcript = []
    for index, utterance in enumerate(raw.get("meeting_transcripts") or []):
        if not isinstance(utterance, dict):
            continue
        content = str(utterance.get("content") or "").strip()
        if not content:
            continue
        transcript.append({
            "segment_no": index,
            "speaker": str(utterance.get("speaker") or ""),
            "content": content,
            "start_ms": None,
        })

    queries = []
    for query in raw.get("general_query_list") or []:
        if isinstance(query, dict) and query.get("query"):
            queries.append({
                "query": str(query.get("query")),
                "answer": str(query.get("answer") or ""),
                "evidence_segments": None,
                "kind": "general",
            })
    for query in raw.get("specific_query_list") or []:
        if isinstance(query, dict) and query.get("query"):
            queries.append({
                "query": str(query.get("query")),
                "answer": str(query.get("answer") or ""),
                "evidence_segments": _spans_to_segments(query.get("relevant_text_span")),
                "kind": "specific",
            })

    topics = []
    for topic in raw.get("topic_list") or []:
        if isinstance(topic, dict):
            topics.append({
                "topic": str(topic.get("topic") or ""),
                "evidence_segments": _spans_to_segments(topic.get("relevant_text_span")),
            })

    return {
        "dataset": "qmsum",
        "id": str(raw.get("meeting_id") or source),
        "language": "en",
        "transcript": transcript,
        "gold": {"queries": queries, "topics": topics},
        "metadata": {"source": source},
    }


def load_qmsum(path: Path) -> list[EvalCase]:
    if path.is_dir():
        files = sorted(path.rglob("*.json"))
        cases = []
        for file in files:
            cases.extend(load_qmsum(file))
        return cases
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_rows = payload if isinstance(payload, list) else [payload]
    return [
        normalize_qmsum(row, source=f"{path.name}#{index}")
        for index, row in enumerate(raw_rows)
        if isinstance(row, dict) and row.get("meeting_transcripts")
    ]


def validate_qmsum(raw: dict) -> list[str]:
    errors = []
    if not raw.get("meeting_transcripts"):
        errors.append("missing meeting_transcripts")
    if not (raw.get("specific_query_list") or raw.get("general_query_list")):
        errors.append("missing query lists")
    return errors
