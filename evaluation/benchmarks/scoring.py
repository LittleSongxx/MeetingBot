"""Offline, paired VCSum summary-overlap diagnostics.

This module never calls a model. It compares only AIMeeting's ``summary`` field
with VCSum's overall meeting summary; the other five minutes fields do not have
matching VCSum overall-summary references. Character-level ROUGE scores are
reproducible within this module, but do not measure factuality and are not
directly comparable with published word-segmented ROUGE scores.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import random
import re
import unicodedata
from typing import Any, Mapping, Sequence


METRICS = ("rouge_1", "rouge_2", "rouge_l")
SUPPORTED_VCSUM_SPLITS = {"vcsum_long_dev": "dev", "vcsum_long_test_repo26": "test"}
VCSUM_REPOSITORY_URL = "https://github.com/hahahawu/VCSum"
METRIC_METHOD = (
    "Unicode NFC; retain letter and number code points; remove whitespace, "
    "punctuation and symbols; count character unigrams/bigrams and character LCS; "
    "report precision, recall and F1"
)
INTERPRETATION = (
    "Character-level overlap with one human reference is a diagnostic for the "
    "summary field only. It cannot establish factual correctness, action-item "
    "accuracy, hallucination rate, or human-acceptance/task-success rate. "
    "This tokenization is not the published VCSum scoring protocol."
)


def characters(value: str) -> list[str]:
    """Normalize Chinese text consistently without requiring a tokenizer."""
    if not isinstance(value, str):
        raise TypeError("summary and reference must be strings")
    normalized = unicodedata.normalize("NFC", value)
    return [char for char in normalized if unicodedata.category(char)[0] in "LN"]


def _prf(overlap: int, prediction_count: int, reference_count: int) -> dict[str, float]:
    precision = overlap / prediction_count if prediction_count else 0.0
    recall = overlap / reference_count if reference_count else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def _ngram_score(prediction: Sequence[str], reference: Sequence[str], n: int) -> dict[str, float]:
    prediction_counts = Counter(tuple(prediction[i:i + n]) for i in range(max(0, len(prediction) - n + 1)))
    reference_counts = Counter(tuple(reference[i:i + n]) for i in range(max(0, len(reference) - n + 1)))
    overlap = sum((prediction_counts & reference_counts).values())
    return _prf(overlap, sum(prediction_counts.values()), sum(reference_counts.values()))


def _lcs_length(a: Sequence[str], b: Sequence[str]) -> int:
    if len(a) > len(b):
        a, b = b, a
    previous = [0] * (len(a) + 1)
    for right in b:
        current = [0]
        for index, left in enumerate(a, start=1):
            current.append(previous[index - 1] + 1 if left == right else max(current[-1], previous[index]))
        previous = current
    return previous[-1]


def character_rouge(prediction: str, reference: str) -> dict[str, dict[str, float]]:
    """Return char-level ROUGE-1/2/L P/R/F1 for one prediction/reference pair."""
    candidate = characters(prediction)
    gold = characters(reference)
    if not gold:
        raise ValueError("reference is empty after normalization")
    return {
        "rouge_1": _ngram_score(candidate, gold, 1),
        "rouge_2": _ngram_score(candidate, gold, 2),
        "rouge_l": _prf(_lcs_length(candidate, gold), len(candidate), len(gold)),
    }


def _summary(block: Any, stage: str) -> str:
    if not isinstance(block, Mapping):
        raise ValueError(f"{stage} must be an object")
    output = block.get("output")
    if not isinstance(output, Mapping) or not isinstance(output.get("summary"), str):
        raise ValueError(f"{stage}.output.summary must be a string")
    return output["summary"]


def _bootstrap_mean_interval(values: list[float], *, samples: int = 5000) -> list[float] | None:
    """Percentile paired bootstrap, resampling meetings rather than facts."""
    if len(values) < 2:
        return None
    rng = random.Random(20260925)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(samples))
    return [means[math.floor((samples - 1) * 0.025)], means[math.ceil((samples - 1) * 0.975)]]


TELEMETRY_FIELDS = (
    "actual_llm_dispatch_count", "input_tokens", "output_tokens", "total_tokens",
    "llm_elapsed_ms_sum", "estimated_cost_cny", "wall_elapsed_ms",
)


def _numeric_telemetry(payload: Any) -> dict[str, int | float]:
    if not isinstance(payload, Mapping):
        return {}
    return {field: value for field in TELEMETRY_FIELDS
            if isinstance((value := payload.get(field)), (int, float))
            and not isinstance(value, bool) and math.isfinite(value)}


def _aggregate_telemetry(cases: list[dict[str, Any]]) -> dict[str, dict[str, int | float]]:
    result: dict[str, dict[str, int | float]] = {}
    for field in TELEMETRY_FIELDS:
        values = [case["telemetry"][field] for case in cases if field in case["telemetry"]]
        if values:
            result[field] = {"observed_case_count": len(values), "sum": sum(values), "mean": sum(values) / len(values)}
    return result


def score_records(records: Sequence[Mapping[str, Any]], references: Mapping[str, str]) -> dict[str, Any]:
    """Score complete, paired AIMeeting records against VCSum overall summaries.

    ``records`` use the existing evaluator shape: ``case_id``, ``status``,
    ``baseline.output.summary``, ``revised.output.summary`` and optional
    ``telemetry``. Missing or failed stages are reported as excluded. Unknown
    reference IDs and duplicate case IDs are errors, so mismatched datasets
    cannot silently produce a plausible score.
    """
    if not isinstance(references, Mapping):
        raise TypeError("references must map case IDs to official overall summaries")
    meetings: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    attempted_telemetry: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise TypeError("every result record must be an object")
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("every result record needs a nonempty case_id")
        if case_id in seen:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen.add(case_id)
        if case_id not in references:
            raise ValueError(f"no official reference for case_id: {case_id}")
        reference = references[case_id]
        if not isinstance(reference, str) or not characters(reference):
            raise ValueError(f"empty or invalid reference for case_id: {case_id}")
        attempted_telemetry.append({"telemetry": _numeric_telemetry(record.get("telemetry"))})
        if record.get("status") != "completed":
            excluded.append({"case_id": case_id, "reason": "result status is not completed"})
            continue
        try:
            baseline_text = _summary(record.get("baseline"), "baseline")
            revised_text = _summary(record.get("revised"), "revised")
        except ValueError as exc:
            excluded.append({"case_id": case_id, "reason": str(exc)})
            continue
        baseline = character_rouge(baseline_text, reference)
        revised = character_rouge(revised_text, reference)
        delta = {name: revised[name]["f1"] - baseline[name]["f1"] for name in METRICS}
        telemetry = _numeric_telemetry(record.get("telemetry"))
        case = {
            "case_id": case_id,
            "reference_chars": len(characters(reference)),
            "baseline_chars": len(characters(baseline_text)),
            "revised_chars": len(characters(revised_text)),
            "baseline": baseline,
            "revised": revised,
            "delta_f1": delta,
            "revision_generated": (record.get("revised") or {}).get("revision_generated"),
            "telemetry": telemetry,
        }
        meetings.append(case)
    aggregates: dict[str, Any] = {}
    for name in METRICS:
        if not meetings:
            aggregates[name] = {"baseline_macro": None, "revised_macro": None, "paired_mean_delta_f1": None,
                                "paired_mean_delta_f1_bootstrap_95ci": None,
                                "improved_count": 0, "unchanged_count": 0, "worsened_count": 0}
            continue
        baseline_macro = {part: sum(case["baseline"][name][part] for case in meetings) / len(meetings)
                          for part in ("precision", "recall", "f1")}
        revised_macro = {part: sum(case["revised"][name][part] for case in meetings) / len(meetings)
                         for part in ("precision", "recall", "f1")}
        deltas = [case["delta_f1"][name] for case in meetings]
        aggregates[name] = {
            "baseline_macro": baseline_macro,
            "revised_macro": revised_macro,
            "paired_mean_delta_f1": sum(deltas) / len(deltas),
            "paired_mean_delta_f1_bootstrap_95ci": _bootstrap_mean_interval(deltas),
            "improved_count": sum(value > 1e-12 for value in deltas),
            "unchanged_count": sum(abs(value) <= 1e-12 for value in deltas),
            "worsened_count": sum(value < -1e-12 for value in deltas),
        }
    return {
        "benchmark": "VCSum overall meeting summary",
        "metric": "character-level ROUGE diagnostic",
        "protocol": METRIC_METHOD,
        "output_projection": "AIMeeting minutes.output.summary -> VCSum summary",
        "interpretation": INTERPRETATION,
        "reference_case_count": len(references),
        "result_case_count": len(records),
        "paired_case_count": len(meetings),
        "missing_case_ids": sorted(set(references) - seen),
        "excluded": excluded,
        "aggregate": aggregates,
        "telemetry": _aggregate_telemetry(attempted_telemetry),
        "scored_telemetry": _aggregate_telemetry(meetings),
        "meetings": meetings,
    }


def _load_records(path: Path, *, manifest_sha256: str | None = None) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("manifest_sha256") is not None:
        if payload["manifest_sha256"] != manifest_sha256:
            raise ValueError("result report manifest_sha256 differs from the supplied manifest")
    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("result JSON must be a list or contain a records list")
    return records


def _load_manifest_references(path: Path) -> dict[str, str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("expected a VCSum manifest with schema_version 1")
    split = SUPPORTED_VCSUM_SPLITS.get(manifest.get("dataset_id"))
    if split is None:
        raise ValueError("unsupported VCSum dataset_id")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("repository_url") != VCSUM_REPOSITORY_URL or source.get("split") != split:
        raise ValueError("VCSum source repository or split differs from dataset_id")
    revision = source.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("VCSum source revision must be a pinned Git commit")
    files = source.get("files")
    if not isinstance(files, list):
        raise ValueError("VCSum source files are missing")
    source_files = {}
    for file in files:
        if not isinstance(file, dict) or not isinstance(file.get("path"), str) or file["path"] in source_files:
            raise ValueError("VCSum source files need unique paths")
        expected_url = f"https://raw.githubusercontent.com/hahahawu/VCSum/{revision}/{file['path']}"
        if file.get("url") != expected_url or not isinstance(file.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", file["sha256"]):
            raise ValueError(f"VCSum source URL/checksum is invalid: {file['path']}")
        source_files[file["path"]] = file["sha256"]
    required_files = {"vcsum_data/overall_context.txt", f"vcsum_data/long_{split}.txt"}
    if not required_files.issubset(source_files):
        raise ValueError(f"VCSum {split} source files are incomplete")
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise ValueError("manifest JSON must contain cases list")
    references: dict[str, str] = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("every manifest case needs string id")
        if case["id"] in references:
            raise ValueError(f"duplicate manifest id: {case['id']}")
        if case.get("split") != split:
            raise ValueError(f"case split differs from VCSum source split: {case['id']}")
        reference = case.get("reference_overall")
        if not isinstance(reference, str) or not characters(reference):
            raise ValueError(f"empty or invalid official reference: {case['id']}")
        expected_hash = case.get("reference_sha256")
        actual_hash = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"reference checksum mismatch: {case['id']}")
        transcript = case.get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            raise ValueError(f"transcript is missing: {case['id']}")
        if hashlib.sha256(transcript.encode("utf-8")).hexdigest() != case.get("transcript_sha256"):
            raise ValueError(f"transcript checksum mismatch: {case['id']}")
        references[case["id"]] = reference
    return references


def format_summary_markdown(report: Mapping[str, Any]) -> str:
    """Render the numeric diagnostics without exposing reference or meeting text."""
    def number(value: Any) -> str:
        return "—" if value is None else f"{value:.4f}"

    paired = report["paired_case_count"]
    lines = [
        "# VCSum 摘要重叠诊断",
        "",
        f"已配对评分：{paired}/{report['reference_case_count']} 场；已出现结果：{report['result_case_count']} 场；"
        f"运行失败或缺少输出：{len(report['excluded'])} 场；尚无结果：{len(report['missing_case_ids'])} 场。",
        "",
        "| 指标（字符级 F1） | 初稿宏平均 | 修订稿宏平均 | 配对平均变化 | 改善 / 持平 / 退化 | 95% bootstrap 区间 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for metric in METRICS:
        row = report["aggregate"][metric]
        initial = row["baseline_macro"]
        revised = row["revised_macro"]
        ci = row["paired_mean_delta_f1_bootstrap_95ci"]
        ci_text = "—" if ci is None else f"[{number(ci[0])}, {number(ci[1])}]"
        lines.append(
            f"| {metric.upper().replace('_', '-')} | {number(initial['f1'] if initial else None)} | "
            f"{number(revised['f1'] if revised else None)} | {number(row['paired_mean_delta_f1'])} | "
            f"{row['improved_count']} / {row['unchanged_count']} / {row['worsened_count']} | {ci_text} |"
        )
    if report["missing_case_ids"]:
        lines.extend(("", "尚无结果的样例：" + ", ".join(report["missing_case_ids"])))
    if report["excluded"]:
        lines.extend(("", "未评分样例：" + "; ".join(
            f"{case['case_id']}（{case['reason']}）" for case in report["excluded"])))
    telemetry = report["telemetry"]
    if "total_tokens" in telemetry:
        item = telemetry["total_tokens"]
        lines.extend(("", f"已出现结果的 Token 合计：{item['sum']:,.0f}（{item['observed_case_count']} 场有记录，包含失败样例）。"))
    lines.extend((
        "",
        "说明：这里只比较 AIMeeting `summary` 与 VCSum 官方整体摘要的文字重叠。字符级 ROUGE "
        "与论文可能采用的分词评分口径不同，不能直接对照论文分数；公开参考摘要没有负责人、期限或人工采纳的逐项金标。"
        "需要依据原始转写进行人工盲审，才能评估这些事实与业务指标。",
        "",
    ))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Score AIMeeting results against VCSum summaries without model calls")
    parser.add_argument("--manifest", type=Path, required=True, help="locked VCSum manifest with official references")
    parser.add_argument("--results", type=Path, required=True, help="completed public benchmark result JSON")
    parser.add_argument("--output", type=Path, required=True, help="destination score JSON")
    parser.add_argument("--markdown", type=Path, help="optional concise, human-readable metric table")
    args = parser.parse_args()
    manifest_hash = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    score = score_records(_load_records(args.results, manifest_sha256=manifest_hash),
                          _load_manifest_references(args.manifest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(score, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(format_summary_markdown(score), encoding="utf-8")
    print(f"Scored {score['paired_case_count']}/{score['reference_case_count']} paired VCSum cases "
          f"({score['result_case_count']} attempted): {args.output}")


if __name__ == "__main__":
    main()
