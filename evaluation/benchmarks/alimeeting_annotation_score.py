#!/usr/bin/env python3
"""Score fully annotated AliMeeting4MUG review sheets, offline only.

The two stages are unblinded only after every generated item has a valid human
annotation. Official sentence IDs are read here, never by the live runner or
review-sheet preparation. Sentence anchoring is a diagnostic projection of
human-reviewed output, not the official AID leaderboard evaluation.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import random
import re
import sys
from typing import Any


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import alimeeting_review as review  # noqa: E402
from alimeeting_adapter import (  # noqa: E402
    DATASET_ID, SOURCE_DIR, load_action_labels,
)


POSITIVE_ID = re.compile(r"[1-9][0-9]*\Z")
MIN_BOOTSTRAP_MEETINGS = 10
DEFAULT_BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 20260925
RATE_NAMES = (
    "grounded_correct_actionable_rate", "ungrounded_item_rate", "duplicate_item_rate",
    "gold_positive_sentence_coverage", "anchor_precision", "anchor_recall",
    "anchor_f1", "action_output_rate_on_zero_gold_meetings",
)
RATE_DENOMINATORS = {
    "grounded_correct_actionable_rate": "actionable_items",
    "ungrounded_item_rate": "generated_items",
    "duplicate_item_rate": "generated_items",
    "gold_positive_sentence_coverage": "gold_positive_sentences",
    "anchor_precision": "anchor_predicted_sentences",
    "anchor_recall": "gold_positive_sentences",
    "anchor_f1": "anchor_f1_denominator",
    "action_output_rate_on_zero_gold_meetings": "zero_gold_meetings",
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_key(path: Path, report_bytes: bytes, report: dict, records: list[dict],
              cases_by_id: dict[str, dict]) -> dict[str, dict[str, str]]:
    key = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(key, dict) or key.get("schema_version") != 1 or key.get("dataset_id") != DATASET_ID:
        raise ValueError("Stage key has an unsupported schema or dataset")
    if key.get("report_sha256") != _sha256(report_bytes):
        raise ValueError("Stage key does not match the exact live report bytes")
    if key.get("manifest_sha256") != report["manifest_sha256"]:
        raise ValueError("Stage key does not match the locked manifest")
    seed = key.get("seed")
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("Stage key has an invalid randomization seed")
    ids = {record["case_id"] for record in records}
    mappings = key.get("case_stage_map")
    if (not isinstance(mappings, dict) or set(mappings) != ids
            or mappings != review._stage_map(list(ids), seed)):
        raise ValueError("Stage key mapping is missing, invalid, or inconsistent with its seed")
    hashes = key.get("source_case_hashes")
    if not isinstance(hashes, dict) or set(hashes) != ids:
        raise ValueError("Stage key source-case hashes have a different case set")
    for case_id in ids:
        expected = {field: cases_by_id[case_id][field] for field in review.HASH_FIELDS}
        if hashes[case_id] != expected:
            raise ValueError(f"{case_id}: stage key source hashes differ from verified source")
    return mappings


def _expected_items(records: list[dict], cases_by_id: dict[str, dict],
                    mappings: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    expected: dict[str, dict[str, str]] = {}
    for record in records:
        case_id = record["case_id"]
        split = cases_by_id[case_id]["split"]
        for stage in ("A", "B"):
            output = record[mappings[case_id][stage]]["output"]
            for collection, kind in (("decisions", "decision"), ("pending_items", "pending_item")):
                for index, item in enumerate(output[collection], 1):
                    item_id = f"{case_id}-{stage}-{kind}-{index:04d}"
                    expected[item_id] = {
                        "case_id": case_id, "split": split, "stage": stage,
                        "item_id": item_id, "item_kind": kind, "item_index": str(index),
                        "content": item["content"], "basis": item.get("basis", ""),
                        "owner_suggestion": item["owner_suggestion"],
                        "deadline_suggestion": item["deadline_suggestion"],
                    }
    return expected


def _read_items(path: Path, expected: dict[str, dict[str, str]],
                cases_by_id: dict[str, dict]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows_by_case_stage: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(review.ITEM_FIELDS):
            raise ValueError("Review CSV columns differ from the prepared annotation schema")
        for line_number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Review CSV line {line_number} has missing or extra cells")
            item_id = row["item_id"]
            if item_id in seen:
                raise ValueError(f"Duplicate item_id in review CSV: {item_id}")
            seen.add(item_id)
            identity = expected.get(item_id)
            if identity is None:
                raise ValueError(f"Unexpected item_id in review CSV: {item_id}")
            for field, original in identity.items():
                if row[field] != original:
                    raise ValueError(f"{item_id}: {field} was changed from the generated output")
            flags: dict[str, bool] = {}
            for field in ("actionable", "grounded", "overstated", "duplicate"):
                raw = row[field]
                if raw not in {"0", "1"}:
                    raise ValueError(f"{item_id}: {field} must be filled with 0 or 1")
                flags[field] = raw == "1"
            evidence_raw = row["evidence_sentence_ids"]
            evidence: set[str] = set()
            if evidence_raw:
                for token in evidence_raw.split(";"):
                    if not POSITIVE_ID.fullmatch(token):
                        raise ValueError(f"{item_id}: evidence IDs must be semicolon-separated positive integers")
                    if token in evidence:
                        raise ValueError(f"{item_id}: duplicate evidence sentence ID {token}")
                    evidence.add(token)
            valid_ids = set(cases_by_id[row["case_id"]]["sentence_id_to_segment_index"])
            if not evidence.issubset(valid_ids):
                raise ValueError(f"{item_id}: evidence sentence ID is absent from its meeting")
            if flags["grounded"] and not evidence:
                raise ValueError(f"{item_id}: grounded=1 requires at least one evidence sentence ID")
            rows_by_case_stage[(row["case_id"], row["stage"])].append({
                **flags, "evidence": evidence, "item_id": item_id,
            })
    missing = set(expected) - seen
    if missing:
        raise ValueError(f"Review CSV omits {len(missing)} generated items; first: {min(missing)}")
    return rows_by_case_stage


def _case_counts(items: list[dict[str, Any]], gold: set[str]) -> dict[str, int]:
    """Return integer counts only; rates are computed after meeting aggregation."""
    actionable = [item for item in items if item["actionable"]]
    correct = [item for item in actionable if item["grounded"] and not item["overstated"]
               and not item["duplicate"]]
    # Sentence anchors come only from grounded, actionable rows. Unsupported
    # actionable rows with a cited sentence stay in the item counts and are
    # reported separately rather than silently disappearing.
    grounded_actionable = [item for item in actionable if item["grounded"]]
    anchors = (set().union(*(item["evidence"] for item in grounded_actionable))
               if grounded_actionable else set())
    correct_anchors = set().union(*(item["evidence"] for item in correct)) if correct else set()
    return {
        "meetings": 1,
        "generated_items": len(items),
        "nonaction_items": len(items) - len(actionable),
        "grounded_correct_actionable_items": len(correct),
        "ungrounded_items": sum(not item["grounded"] for item in items),
        "duplicate_items": sum(item["duplicate"] for item in items),
        "overstated_items": sum(item["overstated"] for item in items),
        "actionable_items": len(actionable),
        "unanchored_actionable_items": sum(not item["evidence"] for item in actionable),
        "excluded_ungrounded_actionable_items_with_evidence": sum(
            not item["grounded"] and bool(item["evidence"]) for item in actionable),
        "gold_positive_sentences": len(gold),
        "covered_gold_positive_sentences": len(gold & correct_anchors),
        "anchor_predicted_sentences": len(anchors),
        "anchor_gold_cited_sentences": len(anchors & gold),
        "anchor_non_gold_cited_sentences": len(anchors - gold),
        "anchor_gold_not_cited_sentences": len(gold - anchors),
        "zero_gold_meetings": int(not gold),
        "zero_gold_meetings_with_action_output": int(not gold and bool(actionable)),
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _aggregate(case_counts: list[dict[str, int]]) -> dict[str, Any]:
    counts = {key: sum(case[key] for case in case_counts)
              for key in _case_counts([], set())}
    item_count = counts["generated_items"]
    gold_count = counts["gold_positive_sentences"]
    predicted = counts["anchor_predicted_sentences"]
    tp = counts["anchor_gold_cited_sentences"]
    result: dict[str, Any] = {
        **counts,
        "grounded_correct_actionable_rate": _ratio(
            counts["grounded_correct_actionable_items"], counts["actionable_items"]),
        "nonaction_item_rate": _ratio(counts["nonaction_items"], item_count),
        "ungrounded_item_rate": _ratio(counts["ungrounded_items"], item_count),
        "duplicate_item_rate": _ratio(counts["duplicate_items"], item_count),
        "gold_positive_sentence_coverage": _ratio(counts["covered_gold_positive_sentences"], gold_count),
        "anchor_precision": _ratio(tp, predicted),
        "anchor_recall": _ratio(tp, gold_count),
        "anchor_f1": _ratio(2 * tp, predicted + gold_count),
        "action_output_rate_on_zero_gold_meetings": _ratio(
            counts["zero_gold_meetings_with_action_output"], counts["zero_gold_meetings"]),
    }
    return result


def _deltas(baseline: dict[str, Any], revised: dict[str, Any]) -> dict[str, float | None]:
    return {name: (revised[name] - baseline[name]
                   if baseline[name] is not None and revised[name] is not None else None)
            for name in RATE_NAMES}


def _percentile(values: list[float], p: float) -> float:
    values.sort()
    at = (len(values) - 1) * p
    lower = int(at)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (at - lower)


def _bootstrap_paired(by_case: dict[str, dict[str, dict[str, int]]],
                      *, draws: int = DEFAULT_BOOTSTRAP_DRAWS) -> dict[str, Any]:
    case_ids = sorted(by_case)
    if len(case_ids) < MIN_BOOTSTRAP_MEETINGS:
        return {"method": "paired meeting-cluster percentile bootstrap",
                "available": False, "reason": f"requires at least {MIN_BOOTSTRAP_MEETINGS} meetings",
                "meeting_count": len(case_ids)}
    rng = random.Random(BOOTSTRAP_SEED)
    samples: dict[str, list[float]] = {name: [] for name in RATE_NAMES}
    eligible: dict[str, dict[str, int]] = {}
    for name, denominator in RATE_DENOMINATORS.items():
        eligible[name] = {}
        for phase in ("baseline", "revised"):
            eligible[name][phase] = sum(
                ((case[phase]["anchor_predicted_sentences"]
                  + case[phase]["gold_positive_sentences"]) > 0
                 if denominator == "anchor_f1_denominator"
                 else case[phase][denominator] > 0)
                for case in by_case.values())
    for _ in range(draws):
        sampled = rng.choices(case_ids, k=len(case_ids))
        baseline = _aggregate([by_case[case_id]["baseline"] for case_id in sampled])
        revised = _aggregate([by_case[case_id]["revised"] for case_id in sampled])
        for name, delta in _deltas(baseline, revised).items():
            if delta is not None:
                samples[name].append(delta)
    return {
        "method": "paired meeting-cluster percentile bootstrap",
        "available": True, "meeting_count": len(case_ids), "draws": draws,
        "seed": BOOTSTRAP_SEED,
        "eligible_meetings_by_stage": eligible,
        "paired_delta_95_percentile_ci": {
            name: ({"lower": _percentile(values, .025),
                    "upper": _percentile(values, .975), "valid_draws": len(values)}
                   if values and min(eligible[name].values()) >= MIN_BOOTSTRAP_MEETINGS else None)
            for name, values in samples.items()
        },
        "ci_not_estimated_reason": {
            name: f"fewer than {MIN_BOOTSTRAP_MEETINGS} denominator-eligible meetings in a stage"
            for name in RATE_NAMES if min(eligible[name].values()) < MIN_BOOTSTRAP_MEETINGS
        },
    }


def _group_score(by_case: dict[str, dict[str, dict[str, int]]]) -> dict[str, Any]:
    baseline = _aggregate([case["baseline"] for case in by_case.values()])
    revised = _aggregate([case["revised"] for case in by_case.values()])
    return {"baseline": baseline, "revised": revised,
            "paired_delta_revised_minus_baseline": _deltas(baseline, revised),
            "bootstrap": _bootstrap_paired(by_case)}


def score(report_path: Path, source_dir: Path, review_items_path: Path,
          stage_key_path: Path) -> dict[str, Any]:
    """Validate all inputs and score a completed, human-annotated live run."""
    report_path = review._inside_project(Path(report_path), label="report")
    source_dir = review._inside_project(Path(source_dir), label="source directory")
    review_items_path = review._inside_project(Path(review_items_path), label="review CSV")
    stage_key_path = review._inside_project(Path(stage_key_path), label="stage key")
    if stage_key_path.is_relative_to(review_items_path.parent):
        raise ValueError("Stage key must be outside the reviewer-visible directory")
    report, report_bytes, records, cases_by_id = review._checked_inputs(report_path, source_dir)
    mapping = _read_key(stage_key_path, report_bytes, report, records, cases_by_id)
    expected = _expected_items(records, cases_by_id, mapping)
    rows_by_case_stage = _read_items(review_items_path, expected, cases_by_id)

    splits = tuple(split for split in ("dev", "test")
                   if any(cases_by_id[record["case_id"]]["split"] == split for record in records))
    # Deliberately load official action labels only after all annotation rows
    # have passed completeness and source-integrity checks.
    all_gold = load_action_labels(source_dir, splits=splits)
    case_gold = {record["case_id"]: set(all_gold[record["case_id"]]) for record in records}
    by_case: dict[str, dict[str, dict[str, int]]] = {}
    split_by_case: dict[str, str] = {}
    for record in records:
        case_id = record["case_id"]
        split_by_case[case_id] = cases_by_id[case_id]["split"]
        by_case[case_id] = {}
        for stage in ("A", "B"):
            phase = mapping[case_id][stage]
            by_case[case_id][phase] = _case_counts(rows_by_case_stage[(case_id, stage)],
                                                   case_gold[case_id])

    return {
        "schema_version": 1, "dataset_id": DATASET_ID,
        "metric_status": "human_annotation_required_completed",
        "official_aid_leaderboard_metric": False,
        "source": {"report_sha256": _sha256(report_bytes),
                   "manifest_sha256": report["manifest_sha256"],
                   "review_items_sha256": _sha256(review_items_path.read_bytes()),
                   "stage_key_sha256": _sha256(stage_key_path.read_bytes()),
                   "case_count": len(by_case), "item_count": len(expected)},
        "definitions": {
            "grounded_correct_actionable": "actionable=1, grounded=1, overstated=0, duplicate=0; denominator: generated decision and pending_item rows annotated actionable=1",
            "nonaction_item": "actionable=0; reported separately and excluded from the actionable quality denominator",
            "ungrounded_item": "grounded=0; denominator: all generated rows",
            "duplicate_item": "duplicate=1; denominator: all generated rows",
            "gold_positive_sentence_coverage": "official action sentence ID cited by at least one grounded_correct_actionable item; denominator: official positive sentence IDs",
            "anchor_diagnostic": "unique cited sentence IDs from grounded=1 and actionable=1 rows versus official action sentence IDs; unsupported actionable rows with/without citations remain in item counts; not official AID scoring",
            "action_output_on_zero_gold_meeting": "zero official action IDs but at least one generated row annotated actionable=1; a risk signal, not proof of a false claim or hallucination, requiring source-evidence review",
            "undefined_rates": "null when a metric has a zero denominator",
            "paired_delta": "revised rate minus baseline rate; pooled counts, same meetings",
        },
        "overall": _group_score(by_case),
        "splits": {split: _group_score({case_id: stages for case_id, stages in by_case.items()
                                        if split_by_case[case_id] == split})
                   for split in splits},
        "cases": {case_id: {"split": split_by_case[case_id], **stages}
                  for case_id, stages in sorted(by_case.items())},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--review-items", type=Path, required=True)
    parser.add_argument("--stage-key", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True,
                        help="write private score JSON outside the reviewer-visible directory")
    args = parser.parse_args(argv)
    try:
        output = review._inside_project(args.out, label="score output")
        review_dir = review._inside_project(args.review_items.parent, label="review directory")
        if output.is_relative_to(review_dir):
            raise ValueError("Score output must be outside the reviewer-visible directory")
        result = score(args.report, args.source_dir, args.review_items, args.stage_key)
        if not output.parent.is_dir():
            raise ValueError("Score output parent directory must already exist")
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        print(json.dumps({"out": str(output), "case_count": result["source"]["case_count"],
                          "item_count": result["source"]["item_count"],
                          "official_aid_leaderboard_metric": False}, ensure_ascii=False))
        return 0
    except (ValueError, FileNotFoundError, FileExistsError, OSError, json.JSONDecodeError) as exc:
        print(f"AliMeeting annotation scoring stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
