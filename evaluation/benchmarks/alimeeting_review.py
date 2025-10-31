#!/usr/bin/env python3
"""Prepare blinded, source-verifiable AliMeeting4MUG human-review sheets.

This command is offline. It does not read action labels, call a model, score
responses, or change the product. Keep stage_key.json away from annotators.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sys


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from alimeeting_adapter import (  # noqa: E402
    DATASET_ID, SOURCE_DIR, load_cases,
)
from bench_common import schema_errors  # noqa: E402


SENTENCE_FIELDS = ("case_id", "split", "sentence_id", "start_ms", "end_ms", "speaker", "text")
ITEM_FIELDS = (
    "case_id", "split", "stage", "item_id", "item_kind", "item_index",
    "content", "basis", "owner_suggestion", "deadline_suggestion",
    "evidence_sentence_ids", "actionable", "grounded", "overstated", "duplicate",
)
ANNOTATION_FIELDS = ("evidence_sentence_ids", "actionable", "grounded", "overstated", "duplicate")
HASH_FIELDS = ("transcript_sha256", "segments_sha256", "source_record_sha256", "source_archive_sha256")
SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")
SAFE_ID_RE = re.compile(r"alimug_M\d{4}\Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside_project(path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(PROJECT):
        raise ValueError(f"{label} must be inside this project")
    return resolved


def _object_from_bytes(raw: bytes, *, label: str) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _unique_case_ids(rows: object, *, label: str, key: str) -> list[str]:
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{label} must be a nonempty list")
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(key), str):
            raise ValueError(f"{label} has an invalid case ID")
        case_id = row[key]
        if not SAFE_ID_RE.fullmatch(case_id) or case_id in ids:
            raise ValueError(f"{label} has a duplicate or unsafe case ID: {case_id!r}")
        ids.append(case_id)
    return ids


def _checked_inputs(report_path: Path, source_dir: Path) -> tuple[dict, bytes, list[dict], dict[str, dict]]:
    """Verify the live report, its locked manifest, and checksum-checked ZIP cases."""
    report_bytes = report_path.read_bytes()
    report = _object_from_bytes(report_bytes, label="report")
    if (report.get("mode") != "live" or report.get("dataset_id") != DATASET_ID
            or report.get("dataset_name") != "AliMeeting4MUG"
            or report.get("synthetic") is not False or report.get("asr_bypassed") is not True
            or report.get("status") not in {"completed", "recovered_completed"}):
        raise ValueError("Only a completed, non-synthetic AliMeeting4MUG live report is accepted")
    manifest_hash = report.get("manifest_sha256")
    if not isinstance(manifest_hash, str) or not SHA256_RE.fullmatch(manifest_hash):
        raise ValueError("Report has no valid manifest SHA-256")

    locked_bytes = (report_path.parent / "manifest.lock.json").read_bytes()
    if _sha256(locked_bytes) != manifest_hash:
        raise ValueError("Locked manifest SHA-256 differs from live report")
    locked = _object_from_bytes(locked_bytes, label="locked manifest")
    if locked.get("dataset_id") != DATASET_ID or locked.get("synthetic") is not False:
        raise ValueError("Locked manifest is not AliMeeting4MUG source data")
    locked_rows = locked.get("cases")
    locked_ids = _unique_case_ids(locked_rows, label="locked cases", key="id")
    locked_by_id = dict(zip(locked_ids, locked_rows))

    plan = report.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("Live report is missing its case plan")
    planned_ids = _unique_case_ids(plan.get("cases"), label="planned cases", key="case_id")
    records = report.get("records")
    record_ids = _unique_case_ids(records, label="completed records", key="case_id")
    if set(record_ids) != set(planned_ids) or not set(record_ids).issubset(locked_by_id):
        raise ValueError("Report records, plan, and locked manifest have different case sets")

    splits: set[str] = set()
    for record in records:
        source = record.get("source")
        if not isinstance(source, dict) or source.get("split") not in {"dev", "test"}:
            raise ValueError(f"{record['case_id']}: missing dev/test source split")
        splits.add(source["split"])
    # load_cases validates pinned ZIP sizes and hashes and returns no action_ids.
    cases = load_cases(source_dir, splits=tuple(s for s in ("dev", "test") if s in splits))
    cases_by_id = {case["id"]: case for case in cases}
    if not set(record_ids).issubset(cases_by_id):
        raise ValueError("A reported case is absent from the checksum-verified source ZIPs")

    for record in records:
        case_id = record["case_id"]
        source = record["source"]
        case = cases_by_id[case_id]
        locked_case = locked_by_id[case_id]
        if record.get("status") != "completed":
            raise ValueError(f"{case_id}: incomplete case cannot be prepared for review")
        if (source.get("case_id") != case_id or source.get("manifest_sha256") != manifest_hash
                or source.get("split") != case["split"]
                or source.get("transcript_sha256") != case["transcript_sha256"]):
            raise ValueError(f"{case_id}: report source identity/hash differs from source case")
        if source.get("input_char_count") != len(case["transcript"]):
            raise ValueError(f"{case_id}: report input length differs from source case")
        for field in HASH_FIELDS:
            if locked_case.get(field) != case[field]:
                raise ValueError(f"{case_id}: locked {field} differs from source ZIP")
            if field in source and source[field] != case[field]:
                raise ValueError(f"{case_id}: report {field} differs from source ZIP")
        if locked_case.get("split") != case["split"]:
            raise ValueError(f"{case_id}: locked split differs from source ZIP")
        for phase in ("baseline", "revised"):
            block = record.get(phase)
            output = block.get("output") if isinstance(block, dict) else None
            # AliMeeting4MUG 冻结产物是 origin/evidence 溯源字段引入前的 legacy 形态，
            # 必须用 legacy 契约校验，否则产品契约升级会把已冻结证据误判成不合规
            errors = schema_errors(output, contract="legacy")
            if errors:
                raise ValueError(f"{case_id}: {phase} output violates product schema: {errors[0]}")
    return report, report_bytes, records, cases_by_id


def _stage_map(case_ids: list[str], seed: int) -> dict[str, dict[str, str]]:
    """Stable, near-balanced randomization independent of report record order."""
    ranked = sorted(case_ids, key=lambda case_id: (
        _sha256(f"{seed}\0{case_id}".encode("utf-8")), case_id))
    # For an odd number of cases, even a one-case pilot can put the baseline
    # behind either letter. The count of A/B assignments still differs by <= 1.
    extra_as_a = int(_sha256(f"{seed}\0orientation".encode("utf-8")), 16) & 1
    baseline_as_a = set(ranked[:len(ranked) // 2 + (extra_as_a if len(ranked) % 2 else 0)])
    return {case_id: ({"A": "baseline", "B": "revised"} if case_id in baseline_as_a
                      else {"A": "revised", "B": "baseline"})
            for case_id in sorted(case_ids)}


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def prepare(
    report_path: Path, source_dir: Path, review_dir: Path, stage_key_path: Path,
    *, seed: int = 20260925,
) -> dict:
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("seed must be an unsigned 64-bit integer")
    report_path = _inside_project(Path(report_path), label="report")
    source_dir = _inside_project(Path(source_dir), label="source directory")
    review_dir = _inside_project(Path(review_dir), label="review directory")
    stage_key_path = _inside_project(Path(stage_key_path), label="stage key")
    if stage_key_path.is_relative_to(review_dir):
        raise ValueError("Stage key must be outside the reviewer-visible directory")
    if stage_key_path.exists() or review_dir.exists():
        raise FileExistsError("Review directory and stage key must both be new paths")
    if not stage_key_path.parent.is_dir():
        raise ValueError("Stage key parent directory must already exist")

    report, report_bytes, records, cases_by_id = _checked_inputs(report_path, source_dir)
    by_id = {record["case_id"]: record for record in records}
    mappings = _stage_map(list(by_id), seed)
    sentence_rows: list[dict] = []
    item_rows: list[dict] = []
    source_hashes: dict[str, dict[str, str]] = {}
    for case_id in sorted(by_id):
        case = cases_by_id[case_id]
        source_hashes[case_id] = {field: case[field] for field in HASH_FIELDS}
        for segment in case["segments"]:
            sentence_rows.append({
                "case_id": case_id, "split": case["split"],
                "sentence_id": segment["source_sentence_id"],
                "start_ms": segment["start_ms"], "end_ms": segment["end_ms"],
                "speaker": segment["speaker"], "text": segment["text"],
            })
        for stage in ("A", "B"):
            output = by_id[case_id][mappings[case_id][stage]]["output"]
            for collection, kind in (("decisions", "decision"), ("pending_items", "pending_item")):
                for index, item in enumerate(output[collection], 1):
                    row = {
                        "case_id": case_id, "split": case["split"], "stage": stage,
                        "item_id": f"{case_id}-{stage}-{kind}-{index:04d}",
                        "item_kind": kind, "item_index": index,
                        "content": item["content"], "basis": item.get("basis", ""),
                        "owner_suggestion": item["owner_suggestion"],
                        "deadline_suggestion": item["deadline_suggestion"],
                    }
                    row.update({field: "" for field in ANNOTATION_FIELDS})
                    item_rows.append(row)

    stage_key = {
        "schema_version": 1, "dataset_id": DATASET_ID,
        "report_sha256": _sha256(report_bytes),
        "manifest_sha256": report["manifest_sha256"],
        "seed": seed, "randomization": "SHA256(seed,case_id) rank; SHA256(seed,orientation) odd-case tie-break",
        "case_stage_map": mappings, "source_case_hashes": source_hashes,
    }
    review_dir.parent.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(exist_ok=False)
    created_key = False
    try:
        _write_csv(review_dir / "source_sentences.csv", SENTENCE_FIELDS, sentence_rows)
        _write_csv(review_dir / "review_items.csv", ITEM_FIELDS, item_rows)
        fd = os.open(stage_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created_key = True
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(stage_key, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except BaseException:
        if created_key:
            stage_key_path.unlink(missing_ok=True)
        for filename in ("source_sentences.csv", "review_items.csv"):
            (review_dir / filename).unlink(missing_ok=True)
        review_dir.rmdir()
        raise
    return {"case_count": len(by_id), "sentence_count": len(sentence_rows),
            "item_count": len(item_rows), "review_dir": str(review_dir),
            "stage_key": str(stage_key_path), "model_calls": 0, "gold_scored": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="create blinded A/B annotation CSVs; no scoring")
    prep.add_argument("--report", type=Path, required=True)
    prep.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    prep.add_argument("--review-dir", type=Path, required=True)
    prep.add_argument("--stage-key", type=Path, required=True,
                      help="write mapping outside --review-dir; keep private from annotators")
    prep.add_argument("--seed", type=int, default=20260925)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.report, args.source_dir, args.review_dir, args.stage_key, seed=args.seed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, FileNotFoundError, FileExistsError, OSError, json.JSONDecodeError) as exc:
        print(f"AliMeeting review preparation stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
