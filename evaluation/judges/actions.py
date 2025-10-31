"""AliMeeting4MUG action-item-detection scoring (the official positive-F1 metric).

This is the only Chinese, publicly documented gold standard for meeting action
items the project has access to, and until now the downloaded corpus was unused
because no scorer existed. Building it does not require a model call: the scorer
is validated here against the gold labels themselves, so a future authorised run
can produce a number that is directly comparable to the published baselines.

The official metric is **positive F1** (F1 over the action-item class), computed
with a classification report. Accuracy must never be quoted: positives are
0.48%-0.55% of sentences, so an all-negative classifier scores above 99.4%
accuracy with a positive F1 of exactly zero.

Known limitation, already documented in ``ACCESS_NOTES.md``: the label means
"sentence related to an action item", and the product emits generative minutes
without source-sentence identifiers. Applying this scorer to product output
requires a pre-declared mapping from generated items back to sentence ids, and
manual adjudication of ambiguous matches. The scorer does not invent that
mapping.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

CSV_FIELD_LIMIT = 100 * 1024 * 1024

SOURCE_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "source" / "alimeeting4mug"
SPLITS = {
    "dev": SOURCE_DIR / "dev.zip",
    "test1": SOURCE_DIR / "except_TS_test1.zip",
}
PAPER_BASELINES = {
    "note": "published baselines are inconsistent across sources; verify against the ICASSP 2023 MUG overview",
    "positive_f1_bert": 64.76,
    "positive_f1_longformer": 65.35,
    "positive_f1_structbert": 67.84,
    "positive_f1_context_drop_dynamic": 70.82,
}


def load_records(split: str) -> list[dict[str, Any]]:
    """Read one split of the corpus into memory as JSON records."""
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}; expected one of {sorted(SPLITS)}")
    path = SPLITS[split]
    if not path.exists():
        raise FileNotFoundError(f"missing corpus archive: {path}")
    csv.field_size_limit(CSV_FIELD_LIMIT)
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".csv"))
        raw = archive.read(name).decode("utf-8")
    reader = csv.reader(io.StringIO(raw), delimiter="\t")
    header = next(reader)
    if header[:2] != ["idx", "content"]:
        raise ValueError(f"unexpected header {header!r}")
    records: list[dict[str, Any]] = []
    for row in reader:
        if len(row) < 2 or not row[1].strip():
            continue
        record = json.loads(row[1])
        record["_row_index"] = row[0]
        records.append(record)
    return records


def _action_id_set(record: Mapping[str, Any]) -> set[Any]:
    """Action ids are published as ``[{"id": 715}]``; tolerate a bare-id list too."""
    ids: set[Any] = set()
    for entry in record.get("action_ids") or []:
        if isinstance(entry, Mapping):
            if "id" in entry:
                ids.add(entry["id"])
        else:
            ids.add(entry)
    return ids


def gold_labels(record: Mapping[str, Any]) -> list[int]:
    """One binary label per sentence, in sentence order."""
    actions = _action_id_set(record)
    return [1 if sentence.get("id") in actions else 0 for sentence in record.get("sentences") or []]


def positive_f1(gold: Sequence[int], predicted: Sequence[int]) -> dict[str, Any]:
    """Precision, recall and F1 over the positive (action-item) class."""
    if len(gold) != len(predicted):
        raise ValueError("gold and predicted must have equal length")
    tp = sum(1 for g, p in zip(gold, predicted) if g == 1 and p == 1)
    fp = sum(1 for g, p in zip(gold, predicted) if g == 0 and p == 1)
    fn = sum(1 for g, p in zip(gold, predicted) if g == 1 and p == 0)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": precision,
        "recall": recall,
        "positive_f1": f1,
        "accuracy": (tp + sum(1 for g, p in zip(gold, predicted) if g == 0 and p == 0)) / len(gold)
        if gold else None,
    }


def macro_positive_f1(
    records: Sequence[Mapping[str, Any]], predictions: Sequence[Sequence[int]]
) -> dict[str, Any]:
    """Per-meeting positive F1 averaged over meetings, plus the pooled micro value."""
    if len(records) != len(predictions):
        raise ValueError("records and predictions must have equal length")
    per_meeting: list[dict[str, Any]] = []
    pooled_gold: list[int] = []
    pooled_pred: list[int] = []
    for record, predicted in zip(records, predictions):
        gold = gold_labels(record)
        if not gold:
            continue
        scores = positive_f1(gold, list(predicted))
        scores["meeting_key"] = record.get("meeting_key")
        scores["positive_sentence_count"] = sum(gold)
        per_meeting.append(scores)
        pooled_gold.extend(gold)
        pooled_pred.extend(list(predicted))
    with_positives = [m for m in per_meeting if m["positive_sentence_count"] > 0]
    macro = (
        sum(m["positive_f1"] for m in with_positives) / len(with_positives)
        if with_positives else None
    )
    return {
        "meeting_count": len(per_meeting),
        "meetings_with_positives": len(with_positives),
        "macro_positive_f1_over_meetings": macro,
        "micro_positive_f1_pooled": positive_f1(pooled_gold, pooled_pred),
        "per_meeting": per_meeting,
    }


def corpus_stats(split: str) -> dict[str, Any]:
    """Describe a split: meeting, sentence and positive counts, and the trivial floors."""
    records = load_records(split)
    sentence_total = 0
    positive_total = 0
    meetings_with_positives = 0
    for record in records:
        gold = gold_labels(record)
        sentence_total += len(gold)
        positives = sum(gold)
        positive_total += positives
        if positives:
            meetings_with_positives += 1
    all_negative = macro_positive_f1(records, [[0] * len(gold_labels(r)) for r in records])
    all_positive_gold = [[1] * len(gold_labels(r)) for r in records]
    all_positive = macro_positive_f1(records, all_positive_gold)
    return {
        "split": split,
        "meeting_count": len(records),
        "sentence_count": sentence_total,
        "positive_sentence_count": positive_total,
        "positive_rate": positive_total / sentence_total if sentence_total else None,
        "meetings_with_at_least_one_positive": meetings_with_positives,
        "trivial_baseline_all_negative": {
            "accuracy": all_negative["micro_positive_f1_pooled"]["accuracy"],
            "positive_f1": all_negative["micro_positive_f1_pooled"]["positive_f1"],
        },
        "trivial_baseline_all_positive": {
            "accuracy": all_positive["micro_positive_f1_pooled"]["accuracy"],
            "positive_f1": all_positive["micro_positive_f1_pooled"]["positive_f1"],
        },
        "published_baselines": PAPER_BASELINES,
        "interpretation": (
            "positive F1 is the official metric. Accuracy is reported only to show why it must "
            "never be quoted: the all-negative floor already exceeds 99% while its positive F1 is 0."
        ),
    }


def self_check(split: str = "dev") -> dict[str, Any]:
    """Score the gold as if it were a prediction; every value must be exactly 1.0."""
    records = load_records(split)
    predictions = [gold_labels(record) for record in records]
    result = macro_positive_f1(records, predictions)
    micro = result["micro_positive_f1_pooled"]
    return {
        "split": split,
        "meetings": result["meeting_count"],
        "macro_positive_f1": result["macro_positive_f1_over_meetings"],
        "micro_positive_f1": micro["positive_f1"],
        "micro_precision": micro["precision"],
        "micro_recall": micro["recall"],
        "verdict": "ok" if micro["positive_f1"] == 1.0 else "FAILED",
    }


def main(argv: Iterable[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    split = args[0] if args else "dev"
    print(json.dumps(self_check(split), ensure_ascii=False, indent=2))
    print(json.dumps(corpus_stats(split), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
