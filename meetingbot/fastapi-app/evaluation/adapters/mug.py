"""MUG / AliMeeting4MUG adapter.

数据来源与字段：
https://www.modelscope.cn/datasets/modelscope/Alimeeting4MUG
样例结构（ModelScope README）：
  {
    "meeting_key": "M0115",
    "topic_segment_ids": [{"id": 79, "candidate": [{"title": "...", "key_sentence": ["4","54"]}]}],
    "candidate": [{"key_sentence": ["4", ...], "key_word": [...]}],
    "sentences": [{"id": 1, "speaker": "no.0", "start_time": "4.636", "end_time": "23.743", "s": "..."}],
    "action_ids": [{"id": 715}],
    ...
  }

本 adapter 支持单对象、JSON 数组、JSONL；句子 `id` 作为 segment_no，action_ids 是
句级 action item 的 gold；key_sentence 是抽取式摘要/证据的 gold。
"""

from __future__ import annotations

from pathlib import Path

from evaluation.adapters import EvalCase, load_json_or_jsonl


def _unique_ints(values) -> list[int]:
    result = []
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("id")
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return result


def _candidate_key_sentences(candidates: list[dict] | None) -> list[int]:
    result = []
    for candidate in candidates or []:
        if isinstance(candidate, dict):
            result.extend(_unique_ints(candidate.get("key_sentence")))
    return list(dict.fromkeys(result))


def normalize_mug(raw: dict, source: str = "mug") -> EvalCase:
    sentences = []
    for sentence in raw.get("sentences") or []:
        if not isinstance(sentence, dict):
            continue
        try:
            segment_no = int(sentence.get("id"))
        except (TypeError, ValueError):
            continue
        content = str(sentence.get("s") or "").strip()
        if not content:
            continue
        start_ms = None
        try:
            if sentence.get("start_time") is not None:
                start_ms = int(float(sentence["start_time"]) * 1000)
        except (TypeError, ValueError):
            start_ms = None
        sentences.append({
            "segment_no": segment_no,
            "speaker": str(sentence.get("speaker") or ""),
            "content": content,
            "start_ms": start_ms,
        })

    action_ids = _unique_ints(raw.get("action_ids"))

    session_candidates = raw.get("candidate") or []
    if isinstance(session_candidates, dict):
        session_candidates = [session_candidates]
    session_key_sentences = _candidate_key_sentences(session_candidates)

    topics = []
    for topic in raw.get("topic_segment_ids") or []:
        if not isinstance(topic, dict):
            continue
        candidates = topic.get("candidate") or []
        if isinstance(candidates, dict):
            candidates = [candidates]
        titles = [str(candidate.get("title") or "").strip() for candidate in candidates if candidate.get("title")]
        topics.append({
            "id": topic.get("id"),
            "titles": titles,
            "key_sentences": _candidate_key_sentences(candidates),
        })

    return {
        "dataset": "mug",
        "id": str(raw.get("meeting_key") or raw.get("id") or source),
        "language": "zh",
        "transcript": sentences,
        "gold": {
            "action_segments": action_ids,
            "key_sentences": session_key_sentences,
            "topics": topics,
        },
        "metadata": {"source": source},
    }


def load_mug(path: Path) -> list[EvalCase]:
    raw_rows = load_json_or_jsonl(path)
    cases = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            continue
        cases.append(normalize_mug(raw, source=f"{path.name}#{index}"))
    return cases


def validate_mug(raw: dict) -> list[str]:
    errors = []
    if not raw.get("sentences"):
        errors.append("missing sentences")
    if not raw.get("action_ids"):
        errors.append("missing action_ids")
    return errors
