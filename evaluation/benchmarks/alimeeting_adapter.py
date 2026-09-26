"""Adapt pinned AliMeeting4MUG archives without exposing action labels to a model.

``build_manifest`` and ``load_cases`` contain only transcript-side information.
The official ``action_ids`` are read separately by ``load_action_labels`` for
offline scoring.  The source ZIPs are checked against source-manifest.json on
every read; no network access or ModelScope credentials are needed.
"""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterator
import zipfile


HERE = Path(__file__).resolve().parent
SOURCE_DIR = HERE / "source" / "alimeeting4mug"
DATASET_ID = "modelscope/Alimeeting4MUG"
SPLIT_ARCHIVES = {"dev": "dev.zip", "test": "except_TS_test1.zip"}
DEFAULT_REVIEW_LIMIT_CHARS = 100_000


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(value: object) -> str:
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8"))


def _splits(splits: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    result = tuple(splits)
    if not result or len(set(result)) != len(result) or any(s not in SPLIT_ARCHIVES for s in result):
        raise ValueError("splits must be a nonempty unique selection of dev and test")
    return result


def _source_manifest(source_dir: Path) -> tuple[dict[str, Any], str]:
    raw = (source_dir / "source-manifest.json").read_bytes()
    source = json.loads(raw)
    if source.get("dataset_id") != DATASET_ID or not isinstance(source.get("archives"), dict):
        raise ValueError("Invalid AliMeeting4MUG source manifest")
    return source, sha256_bytes(raw)


def _archive_records(
    source_dir: Path, source: dict[str, Any], split: str,
) -> Iterator[tuple[int, dict[str, Any], str, str]]:
    filename = SPLIT_ARCHIVES[split]
    descriptor = source["archives"].get(filename)
    if not isinstance(descriptor, dict):
        raise ValueError(f"Missing source manifest entry for {filename}")
    archive_path = source_dir / filename
    archive_bytes = archive_path.read_bytes()
    archive_sha = sha256_bytes(archive_bytes)
    if (archive_sha != descriptor.get("sha256") or
            len(archive_bytes) != descriptor.get("bytes")):
        raise ValueError(f"Source ZIP checksum or length mismatch: {archive_path}")
    expected_member = filename.removesuffix(".zip") + ".csv"
    if descriptor.get("zip_members") != [expected_member]:
        raise ValueError(f"Unexpected source manifest ZIP members for {filename}")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        if archive.namelist() != [expected_member]:
            raise ValueError(f"Unexpected ZIP members in {archive_path}")
        # One JSON cell can contain an entire >1 MB meeting. csv's 128 KB
        # default field limit is too small for the official TSV-style CSV.
        previous_limit = csv.field_size_limit()
        csv.field_size_limit(sys.maxsize)
        try:
            with archive.open(expected_member) as raw_stream:
                with io.TextIOWrapper(raw_stream, encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream, delimiter="\t")
                    if reader.fieldnames != ["idx", "content"]:
                        raise ValueError(f"Unexpected CSV columns in {expected_member}")
                    for row_number, row in enumerate(reader):
                        if row.get("idx") != str(row_number) or not isinstance(row.get("content"), str):
                            raise ValueError(f"Invalid row index/content in {expected_member}: {row_number}")
                        raw_content = row["content"]
                        record = json.loads(raw_content)
                        if not isinstance(record, dict):
                            raise ValueError(f"Invalid JSON object in {expected_member}: {row_number}")
                        yield row_number, record, sha256_bytes(raw_content.encode("utf-8")), archive_sha
        finally:
            csv.field_size_limit(previous_limit)


def _milliseconds(raw: object, *, case_id: str, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
        raise ValueError(f"{case_id}: invalid {field}")
    try:
        seconds = Decimal(str(raw))
        if not seconds.is_finite() or seconds < 0:
            raise ValueError
        millis = int((seconds * 1000).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError(f"{case_id}: invalid {field}") from exc
    if millis > 2**63 - 1:
        raise ValueError(f"{case_id}: {field} exceeds millisecond range")
    return millis


def _sentence_id(raw: object, *, case_id: str) -> int:
    if type(raw) is not int or raw <= 0:
        raise ValueError(f"{case_id}: sentence/action IDs must be positive integers")
    return raw


def _action_labels(record: dict[str, Any], sentence_ids: set[int], *, case_id: str) -> list[str]:
    actions = record.get("action_ids")
    if not isinstance(actions, list):
        raise ValueError(f"{case_id}: missing action_ids list")
    labels: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError(f"{case_id}: invalid action label")
        sentence_id = _sentence_id(action.get("id"), case_id=case_id)
        if sentence_id not in sentence_ids or str(sentence_id) in labels:
            raise ValueError(f"{case_id}: action ID is missing from transcript or repeated")
        labels.append(str(sentence_id))
    return labels


def _serialize_case(
    record: dict[str, Any], *, split: str, row_number: int,
    archive_sha: str, record_sha: str, review_cap_chars: int,
) -> dict[str, Any]:
    meeting_key = record.get("meeting_key")
    if not isinstance(meeting_key, str) or not re.fullmatch(r"M\d{4}", meeting_key):
        raise ValueError(f"Invalid AliMeeting4MUG meeting_key at {split} row {row_number}")
    case_id = f"alimug_{meeting_key}"
    sentences = record.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        raise ValueError(f"{case_id}: missing sentences")
    segments: list[dict[str, Any]] = []
    transcript_lines: list[str] = []
    sentence_map: dict[str, int] = {}
    prior_sentence_id = 0
    prior_start_ms = -1
    for index, sentence in enumerate(sentences):
        if not isinstance(sentence, dict):
            raise ValueError(f"{case_id}: invalid sentence {index}")
        sentence_id = _sentence_id(sentence.get("id"), case_id=case_id)
        if sentence_id <= prior_sentence_id:
            raise ValueError(f"{case_id}: sentence IDs must increase without duplicates")
        prior_sentence_id = sentence_id
        start_ms = _milliseconds(sentence.get("start_time"), case_id=case_id, field="start_time")
        end_ms = _milliseconds(sentence.get("end_time"), case_id=case_id, field="end_time")
        if end_ms <= start_ms or start_ms < prior_start_ms:
            raise ValueError(f"{case_id}: invalid or out-of-order sentence times")
        prior_start_ms = start_ms
        speaker = sentence.get("speaker")
        text = sentence.get("s")
        if not isinstance(speaker, str) or not speaker.strip() or len(speaker) > 50:
            raise ValueError(f"{case_id}: invalid speaker")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{case_id}: empty sentence text")
        segments.append({"start_ms": start_ms, "end_ms": end_ms,
                         "speaker": speaker, "text": text,
                         "source_sentence_id": sentence_id})
        sentence_map[str(sentence_id)] = index
        transcript_lines.append(f"[{start_ms}ms][{speaker}] {text}")
    # Validate gold references while keeping their values out of the case.
    _action_labels(record, set(map(int, sentence_map)), case_id=case_id)
    transcript = "\n".join(transcript_lines)
    return {
        "id": case_id,
        "source_meeting_id": meeting_key,
        "split": split,
        "official_split": "dev" if split == "dev" else "except_TS_test1",
        "title": f"AliMeeting4MUG {meeting_key}",
        "transcript": transcript,
        "transcript_sha256": sha256_bytes(transcript.encode("utf-8")),
        "segments": segments,
        "segments_sha256": _canonical_sha256(segments),
        "sentence_id_to_segment_index": sentence_map,
        "sentence_count": len(segments),
        "speaker_count": len({segment["speaker"] for segment in segments}),
        "source_file": SPLIT_ARCHIVES[split],
        "source_archive_sha256": archive_sha,
        "source_record_sha256": record_sha,
        "source_row_index": row_number,
        "timestamp_kind": "official_seconds",
        "timestamps_synthetic_or_indexed": False,
        "agent_review_input_limit_chars": review_cap_chars,
        "agent_review_transcript_over_limit": len(transcript) > review_cap_chars,
    }


def build_manifest(
    source_dir: Path = SOURCE_DIR, *, splits: tuple[str, ...] | list[str] = ("dev", "test"),
    review_cap_chars: int = DEFAULT_REVIEW_LIMIT_CHARS,
) -> dict[str, Any]:
    """Build a deterministic, label-free manifest from checksum-verified ZIPs."""
    source_dir = Path(source_dir)
    selected = _splits(splits)
    if type(review_cap_chars) is not int or not 1 <= review_cap_chars <= 1_000_000:
        raise ValueError("Agent review input limit must be a positive integer <= 1,000,000")
    source, source_manifest_sha = _source_manifest(source_dir)
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    for split in selected:
        filename = SPLIT_ARCHIVES[split]
        descriptor = source["archives"][filename]
        files.append({"split": split, "path": filename, "sha256": descriptor["sha256"],
                      "bytes": descriptor["bytes"]})
        for row_number, record, record_sha, archive_sha in _archive_records(source_dir, source, split):
            case = _serialize_case(record, split=split, row_number=row_number,
                                   archive_sha=archive_sha, record_sha=record_sha,
                                   review_cap_chars=review_cap_chars)
            if case["id"] in seen:
                raise ValueError(f"Duplicate meeting key across archives: {case['id']}")
            seen.add(case["id"])
            cases.append(case)
    return {
        "schema_version": 1,
        "dataset_id": DATASET_ID,
        "synthetic": False,
        "language": "zh-CN",
        "task": "meeting_action_sentence_detection",
        "splits": list(selected),
        "agent_review_input_limit_chars": review_cap_chars,
        "source": {
            "url": source["source_url"],
            "metadata_git_revision": source["metadata_git_revision"],
            "source_manifest_sha256": source_manifest_sha,
            "files": files,
            "test_subset_note": "test means official except_TS_test1.zip, not the entire test partition",
        },
        "selection": {"method": "all_records_in_selected_archives", "selected_count": len(cases),
                      "split_counts": {split: sum(case["split"] == split for case in cases)
                                       for split in selected}},
        "cases": cases,
    }


def load_cases(
    source_dir: Path = SOURCE_DIR, *, splits: tuple[str, ...] | list[str] = ("dev", "test"),
    review_cap_chars: int = DEFAULT_REVIEW_LIMIT_CHARS,
) -> list[dict[str, Any]]:
    return build_manifest(source_dir, splits=splits, review_cap_chars=review_cap_chars)["cases"]


def load_action_labels(
    source_dir: Path = SOURCE_DIR, *, splits: tuple[str, ...] | list[str] = ("dev", "test"),
) -> dict[str, list[str]]:
    """Read gold sentence IDs only on the scoring side; never attach them to cases."""
    source_dir = Path(source_dir)
    source, _ = _source_manifest(source_dir)
    labels: dict[str, list[str]] = {}
    for split in _splits(splits):
        for row_number, record, record_sha, archive_sha in _archive_records(source_dir, source, split):
            case = _serialize_case(record, split=split, row_number=row_number,
                                   archive_sha=archive_sha, record_sha=record_sha,
                                   review_cap_chars=DEFAULT_REVIEW_LIMIT_CHARS)
            if case["id"] in labels:
                raise ValueError(f"Duplicate meeting key across archives: {case['id']}")
            sentence_ids = set(map(int, case["sentence_id_to_segment_index"]))
            labels[case["id"]] = _action_labels(record, sentence_ids, case_id=case["id"])
    return labels


def load_manifest(path: Path | str, *, source_dir: Path = SOURCE_DIR) -> dict[str, Any]:
    """Reject altered cases, leaked labels, changed ZIPs, or a changed source lock."""
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("dataset_id") != DATASET_ID:
        raise ValueError("Not a supported AliMeeting4MUG manifest")
    expected = build_manifest(source_dir, splits=manifest.get("splits", ()),
                              review_cap_chars=manifest.get("agent_review_input_limit_chars"))
    if manifest != expected:
        raise ValueError("AliMeeting4MUG manifest differs from verified sources or contains unexpected fields")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--splits", nargs="+", choices=tuple(SPLIT_ARCHIVES), default=list(SPLIT_ARCHIVES))
    parser.add_argument("--agent-review-limit-chars", type=int, default=DEFAULT_REVIEW_LIMIT_CHARS)
    args = parser.parse_args()
    manifest = build_manifest(args.source_dir, splits=args.splits,
                              review_cap_chars=args.agent_review_limit_chars)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(args.manifest), "case_count": len(manifest["cases"]),
                      "split_counts": manifest["selection"]["split_counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
