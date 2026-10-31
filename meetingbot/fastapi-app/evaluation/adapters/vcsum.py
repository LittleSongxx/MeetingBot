"""VCSUM adapter（中文会议摘要 + highlight 显著句）。

数据来源与格式：
https://github.com/hahahawu/VCSum
目录：
  overall_context.txt    每行 {id, av_num, eos_index, context, speaker}
  overall_highlights.txt 每行 {id, av_num, eos_index, highlights}
  long_{split}.txt       每行 {id, av_num, eos_index, summary}
  short_{split}.txt      每行 {id, av_num, context, speaker, discussion, agenda, highlights}

其中 context 是“段落 -> 句子”嵌套；highlights 与 context 同构，叶子是字符级 0/1
标签（个别样本可能长度不一致，按 min 长度截断）。本 adapter 把 transcript 展平为
句子级 segment，并把任一字符被标亮的句子作为 gold evidence segment。
"""

from __future__ import annotations

import json
from pathlib import Path

from evaluation.adapters import EvalCase


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _flatten_context(context: list, speaker: list, highlights: list | None) -> tuple[list[dict], list[int]]:
    transcript = []
    evidence_segments = []
    for paragraph_index, paragraph in enumerate(context or []):
        if not isinstance(paragraph, list):
            paragraph = [paragraph]
        paragraph_highlights = []
        if highlights and paragraph_index < len(highlights) and isinstance(highlights[paragraph_index], list):
            paragraph_highlights = highlights[paragraph_index]
        for sentence_index, sentence in enumerate(paragraph):
            content = str(sentence or "").strip()
            if not content:
                continue
            segment_no = len(transcript)
            speaker_value = None
            if isinstance(speaker, list) and paragraph_index < len(speaker):
                speaker_value = speaker[paragraph_index]
            transcript.append({
                "segment_no": segment_no,
                "speaker": str(speaker_value) if speaker_value is not None else "",
                "content": content,
                "start_ms": None,
            })
            labels = []
            if sentence_index < len(paragraph_highlights) and isinstance(paragraph_highlights[sentence_index], list):
                labels = paragraph_highlights[sentence_index]
            highlighted_chars = 0
            for label in labels[:len(content)]:
                if int(label or 0) == 1:
                    highlighted_chars += 1
            if highlighted_chars:
                evidence_segments.append(segment_no)
    return transcript, sorted(set(evidence_segments))


def normalize_vcsum_long(
    meeting: dict,
    context_row: dict,
    source: str = "vcsum",
) -> EvalCase:
    transcript, evidence_segments = _flatten_context(
        context_row.get("context") or [],
        context_row.get("speaker") or [],
        (context_row.get("highlights") or context_row.get("_highlights")),
    )
    return {
        "dataset": "vcsum",
        "id": str(meeting.get("id")),
        "language": "zh",
        "transcript": transcript,
        "gold": {
            "summary": str(meeting.get("summary") or ""),
            "evidence_segments": evidence_segments,
            "title": "",
            "kind": "long",
            "topic_boundaries": context_row.get("eos_index") or [],
        },
        "metadata": {"source": source, "av_num": meeting.get("av_num")},
    }


def normalize_vcsum_short(meeting: dict, source: str = "vcsum") -> EvalCase:
    transcript, evidence_segments = _flatten_context(
        meeting.get("context") or [],
        meeting.get("speaker") or [],
        meeting.get("highlights") or [],
    )
    return {
        "dataset": "vcsum",
        "id": str(meeting.get("id")),
        "language": "zh",
        "transcript": transcript,
        "gold": {
            "summary": str(meeting.get("discussion") or ""),
            "evidence_segments": evidence_segments,
            "title": str(meeting.get("agenda") or ""),
            "kind": "short",
            "topic_boundaries": [],
        },
        "metadata": {"source": source, "av_num": meeting.get("av_num")},
    }


def load_vcsum(root: Path, split: str = "test", kind: str = "long") -> list[EvalCase]:
    if kind == "short":
        rows = _read_jsonl(root / f"short_{split}.txt")
        return [normalize_vcsum_short(row, source=f"short_{split}#{index}") for index, row in enumerate(rows)]

    meetings = _read_jsonl(root / f"long_{split}.txt")
    contexts = {str(row.get("id")): row for row in _read_jsonl(root / "overall_context.txt")}
    highlights = {str(row.get("id")): row for row in _read_jsonl(root / "overall_highlights.txt")}
    cases = []
    for index, meeting in enumerate(meetings):
        identifier = str(meeting.get("id"))
        context_row = dict(contexts.get(identifier) or {})
        highlight_row = highlights.get(identifier) or {}
        # overall_highlights 的 highlights 与 overall_context 的 context 同构，合并后交给 flatten
        if highlight_row.get("highlights") is not None:
            context_row["_highlights"] = highlight_row["highlights"]
        if not context_row.get("context"):
            continue
        cases.append(normalize_vcsum_long(meeting, context_row, source=f"long_{split}#{index}"))
    return cases


def validate_vcsum(root: Path) -> list[str]:
    required = ["overall_context.txt", "overall_highlights.txt", "long_test.txt", "short_test.txt"]
    return [name for name in required if not (root / name).exists()]
