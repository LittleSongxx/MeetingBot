"""Prepare a reproducible VCSum overall-summary evaluation manifest.

The official dev split supplies reference *overall summaries*, whereas the
transcripts live in a separate file. References in the emitted manifest are
for scoring only: the model runner must omit ``reference_overall`` from its
request payload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
import os
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
SOURCE_DIR = HERE / "source" / "vcsum"
DEFAULT_MANIFEST = HERE / "manifest.json"
DEFAULT_TEST_MANIFEST = HERE / "manifest_test_repo26.json"
REVISION = "8e51d72e4e6f3af4074fd5b260b6f6d2e75e86c7"
REPOSITORY_URL = "https://github.com/hahahawu/VCSum"
RAW_BASE = f"https://raw.githubusercontent.com/hahahawu/VCSum/{REVISION}"
SOURCE_FILES = {
    "LICENSE": "cb4b732082a845af53cc3beaacb9f1ed7c124f6406daa547d664b36b65f36e88",
    "vcsum_data/long_dev.txt": "02d346d43f1358faf03fb8a154d14dbf3f0272806f735fd455a6c27cd194f4f4",
    "vcsum_data/overall_context.txt": "12814cb3f20bde67ef785fad89b7720e21761e3f8d8d2d3efc7dedccb2dd76af",
}
TEST_REFERENCE_FILE = "vcsum_data/long_test.txt"
TEST_REFERENCE_SHA256 = "ee564707cf7c487e51973027971490fa0471cfad9e6325d85c61c91b32c40ccd"
QUANTILES = (0.10, 0.50, 0.90)
# 分块上限必须与产品一致：这个值同时决定预算估算（每次生成要几次调用）。
# 以前这里自带一份字面量，与产品各写一份——产品把上限从 12000 调到 6000 后，
# 估算器仍按 12000 预留，运行到一半报 "Actual product chunking exceeds reserved budget"。
# 现在两侧读**同一个环境声明**，只有一处真源。
MAX_AGENT_ROUNDS_FOR_BUDGET = 2
DEFAULT_AGENT_REVIEW_LIMIT_CHARS = 20000


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def ensure_sources(source_dir: Path, fetch: bool = False) -> None:
    """Verify pinned official files; optionally fetch only the required files."""
    for relative, expected in SOURCE_FILES.items():
        path = source_dir / relative
        if not path.is_file():
            if not fetch:
                raise FileNotFoundError(f"Missing {path}; rerun with --fetch")
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"{RAW_BASE}/{relative}"
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read()
            if sha256_bytes(data) != expected:
                raise ValueError(f"Official download checksum mismatch: {url}")
            path.write_bytes(data)
        actual = sha256_bytes(path.read_bytes())
        if actual != expected:
            raise ValueError(f"Source checksum mismatch: {path}; expected {expected}, got {actual}")


def ensure_test_source(source_dir: Path, fetch: bool = False) -> None:
    """Verify the same pinned repository's test summary file."""
    ensure_sources(source_dir, fetch=fetch)
    path = source_dir / TEST_REFERENCE_FILE
    if not path.is_file():
        if not fetch:
            raise FileNotFoundError(f"Missing {path}; rerun with --fetch")
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{RAW_BASE}/{TEST_REFERENCE_FILE}"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        if sha256_bytes(data) != TEST_REFERENCE_SHA256:
            raise ValueError(f"Official download checksum mismatch: {url}")
        path.write_bytes(data)
    actual = sha256_bytes(path.read_bytes())
    if actual != TEST_REFERENCE_SHA256:
        raise ValueError(f"Source checksum mismatch: {path}; expected {TEST_REFERENCE_SHA256}, got {actual}")


def read_jsonl(path: Path) -> dict[str, tuple[dict[str, Any], str]]:
    records: dict[str, tuple[dict[str, Any], str]] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            meeting_id = str(row["id"])
            if meeting_id in records:
                raise ValueError(f"Duplicate VCSum id {meeting_id} in {path}")
            records[meeting_id] = (row, sha256_text(line.rstrip("\r\n")))
    return records


def transcript_size(row: dict[str, Any]) -> int:
    return sum(len(utterance) for turn in row["context"] for utterance in turn)


def _canonical_estimator():
    """取得唯一的打包实现（bench_common），本模块不自带一份。"""
    import bench_common  # type: ignore
    return bench_common


def estimated_generation_chunks(segments: list[dict[str, Any]]) -> int:
    """历史元数据字段：单遍长上下文下恒为 1（旧 manifest 里的多块值是冻结证据）。"""
    return 1


def serialize_case(
    reference: dict[str, Any],
    reference_line_sha256: str,
    source: dict[str, Any],
    source_line_sha256: str,
    *,
    split: str = "dev",
    review_cap_chars: int = DEFAULT_AGENT_REVIEW_LIMIT_CHARS,
) -> dict[str, Any]:
    if review_cap_chars <= 0:
        raise ValueError("Agent review context cap must be positive")
    meeting_id = str(reference["id"])
    if str(source["id"]) != meeting_id or source["av_num"] != reference["av_num"]:
        raise ValueError(f"VCSum id/av_num mismatch: {meeting_id}")
    turns, speakers = source["context"], source["speaker"]
    if not isinstance(turns, list) or len(turns) != len(speakers) or not turns:
        raise ValueError(f"Invalid context/speaker alignment: {meeting_id}")
    segments: list[dict[str, Any]] = []
    transcript_lines: list[str] = []
    utterance_count = 0
    for turn_index, (utterances, speaker_id) in enumerate(zip(turns, speakers), 1):
        # The pinned test file has one empty utterance (id 97, turn 10).
        # Keep it in position so the adapted transcript remains source-faithful.
        if not isinstance(utterances, list) or not utterances or not all(
            isinstance(item, str) for item in utterances
        ) or not any(item.strip() for item in utterances):
            raise ValueError(f"Invalid utterance list: {meeting_id}, turn {turn_index}")
        speaker = f"发言人{speaker_id}"
        text = "\n".join(utterances)
        # VCSum has no utterance timestamps. These indices preserve ordering
        # for AIMeeting's required segment schema, not elapsed meeting time.
        segments.append(
            {
                "start_ms": (turn_index - 1) * 1000,
                "end_ms": turn_index * 1000,
                "speaker": speaker,
                "source_speaker_id": speaker_id,
                "text": text,
                "utterances": utterances,
                "turn_index": turn_index,
            }
        )
        transcript_lines.append(f"[第{turn_index}轮][{speaker}] {text}")
        utterance_count += len(utterances)
    transcript = "\n".join(transcript_lines)
    summary = reference["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError(f"Missing overall summary: {meeting_id}")
    chunk_count = estimated_generation_chunks(segments)
    # 调用数同样委托，避免与分块数各算一遍而分叉
    generation_calls = _canonical_estimator().estimated_generation_calls(
        {"id": f"vcsum_{meeting_id}", "segments": segments, "transcript": transcript}
    )
    return {
        "id": f"vcsum_{meeting_id}",
        "source_meeting_id": meeting_id,
        "av_num": source["av_num"],
        "split": split,
        "title": f"VCSum {meeting_id}",
        "transcript": transcript,
        "segments": segments,
        "reference_overall": summary,
        "transcript_sha256": sha256_text(transcript),
        "reference_sha256": sha256_text(summary),
        "source_sha256": source_line_sha256,
        "source_context_record_sha256": source_line_sha256,
        "source_reference_record_sha256": reference_line_sha256,
        "original_utterance_chars": transcript_size(source),
        "transcript_chars": len(transcript),
        "speaker_count": len(set(speakers)),
        "turn_count": len(turns),
        "utterance_count": utterance_count,
        "estimated_generation_chunks": chunk_count,
        "estimated_generation_model_calls": generation_calls,
        "estimated_total_model_calls_max_2_agent_rounds": generation_calls + 2 * MAX_AGENT_ROUNDS_FOR_BUDGET,
        "timestamp_kind": "synthetic_turn_index",
        "timestamps_synthetic_or_indexed": True,
        "agent_review_input_limit_chars": review_cap_chars,
        "agent_review_transcript_over_limit": len(transcript) > review_cap_chars,
        "reference_scope": {
            "overall_summary": True,
            "decision_facts": False,
            "action_owner": False,
            "action_deadline": False,
            "human_adoption": False,
        },
    }


def build_manifest(
    source_dir: Path = SOURCE_DIR, *, all_dev: bool = False,
    review_cap_chars: int = DEFAULT_AGENT_REVIEW_LIMIT_CHARS,
) -> dict[str, Any]:
    ensure_sources(source_dir)
    context = read_jsonl(source_dir / "vcsum_data/overall_context.txt")
    references = read_jsonl(source_dir / "vcsum_data/long_dev.txt")
    candidates: list[tuple[int, int, dict[str, Any], str, dict[str, Any], str]] = []
    for meeting_id, (reference, reference_hash) in references.items():
        if meeting_id not in context:
            raise ValueError(f"No transcript for VCSum dev id {meeting_id}")
        source, source_hash = context[meeting_id]
        if source["av_num"] != reference["av_num"]:
            raise ValueError(f"VCSum av_num mismatch: {meeting_id}")
        candidates.append(
            (transcript_size(source), int(meeting_id), reference, reference_hash, source, source_hash)
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    if all_dev:
        selected = candidates
        rank_indices = list(range(len(candidates)))
        selection_method = "all_official_dev_records"
    else:
        rank_indices = sorted({round((len(candidates) - 1) * quantile) for quantile in QUANTILES})
        selected = [candidates[index] for index in rank_indices]
        selection_method = "fixed_length_quantiles_0.10_0.50_0.90"
    cases = [
        serialize_case(reference, reference_hash, source, source_hash, review_cap_chars=review_cap_chars)
        for _, _, reference, reference_hash, source, source_hash in selected
    ]
    source_files = [
        {
            "path": relative,
            "url": f"{RAW_BASE}/{relative}",
            "sha256": digest,
        }
        for relative, digest in SOURCE_FILES.items()
    ]
    manifest = {
        "schema_version": 1,
        "dataset_id": "vcsum_long_dev",
        "synthetic": False,
        "language": "zh-CN",
        "task": "overall_meeting_summary",
        "source": {
            "repository_url": REPOSITORY_URL,
            "revision": REVISION,
            "split": "dev",
            "repository_license": "MIT",
            "license_note": "Repository LICENSE is MIT; no separate per-record rights statement is provided.",
            "files": source_files,
            "conversation_structure": "Each outer context item is a speaker turn; its inner utterances retain original order.",
        },
        "selection": {
            "method": selection_method,
            "pool_count": len(candidates),
            "selected_count": len(cases),
            "rank_indices_zero_based": rank_indices,
            "selected_meeting_ids": [case["source_meeting_id"] for case in cases],
            "estimated_total_model_calls_max_2_agent_rounds": sum(
                case["estimated_total_model_calls_max_2_agent_rounds"] for case in cases
            ),
            "pool_original_utterance_chars_min": candidates[0][0],
            "pool_original_utterance_chars_max": candidates[-1][0],
        },
        "limitations": [
            "References are overall summaries, not atomic decision/action/owner/deadline gold labels.",
            "The official transcript has no timestamps; segment milliseconds are turn indices only.",
            "Reference similarity cannot establish meeting-level human adoption or factual accuracy.",
        ],
        "cases": cases,
    }
    if review_cap_chars != DEFAULT_AGENT_REVIEW_LIMIT_CHARS:
        manifest["agent_review_input_limit_chars"] = review_cap_chars
    return manifest


def build_test_manifest(
    source_dir: Path = SOURCE_DIR, *,
    review_cap_chars: int = DEFAULT_AGENT_REVIEW_LIMIT_CHARS,
) -> dict[str, Any]:
    """Prepare every record in the pinned GitHub long_test.txt (26 rows)."""
    ensure_test_source(source_dir)
    context = read_jsonl(source_dir / "vcsum_data/overall_context.txt")
    references = read_jsonl(source_dir / TEST_REFERENCE_FILE)
    candidates: list[tuple[int, int, dict[str, Any], str, dict[str, Any], str]] = []
    for meeting_id, (reference, reference_hash) in references.items():
        if meeting_id not in context:
            raise ValueError(f"No transcript for VCSum repository test id {meeting_id}")
        source, source_hash = context[meeting_id]
        if source["av_num"] != reference["av_num"]:
            raise ValueError(f"VCSum av_num mismatch: {meeting_id}")
        candidates.append(
            (transcript_size(source), int(meeting_id), reference, reference_hash, source, source_hash)
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    cases = [
        serialize_case(reference, reference_hash, source, source_hash, split="test", review_cap_chars=review_cap_chars)
        for _, _, reference, reference_hash, source, source_hash in candidates
    ]
    test_source_files = {
        "LICENSE": SOURCE_FILES["LICENSE"],
        "vcsum_data/overall_context.txt": SOURCE_FILES["vcsum_data/overall_context.txt"],
        TEST_REFERENCE_FILE: TEST_REFERENCE_SHA256,
    }
    manifest = {
        "schema_version": 1,
        "dataset_id": "vcsum_long_test_repo26",
        "synthetic": False,
        "language": "zh-CN",
        "task": "overall_meeting_summary",
        "source": {
            "repository_url": REPOSITORY_URL,
            "revision": REVISION,
            "split": "test",
            "repository_license": "MIT",
            "license_note": "Repository LICENSE is MIT; no separate per-record rights statement is provided.",
            "files": [
                {"path": relative, "url": f"{RAW_BASE}/{relative}", "sha256": digest}
                for relative, digest in test_source_files.items()
            ],
            "conversation_structure": "Each outer context item is a speaker turn; its inner utterances retain original order.",
        },
        "selection": {
            "method": "all_pinned_repository_long_test_records",
            "pool_count": len(candidates),
            "selected_count": len(cases),
            "rank_indices_zero_based": list(range(len(candidates))),
            "selected_meeting_ids": [case["source_meeting_id"] for case in cases],
            "estimated_total_model_calls_max_2_agent_rounds": sum(
                case["estimated_total_model_calls_max_2_agent_rounds"] for case in cases
            ),
            "pool_original_utterance_chars_min": candidates[0][0],
            "pool_original_utterance_chars_max": candidates[-1][0],
        },
        "paper_split_comparison": {
            "paper_url": "https://aclanthology.org/2023.findings-acl.377/",
            "paper_overall_test_summary_count": 21,
            "pinned_repository_long_test_record_count": len(candidates),
            "note": "The paper reports 193/25/21 overall train/dev/test summaries, but this pinned repository's long_test.txt has 26 records. This manifest reports the repository's 26-record test file; equivalence to the paper's 21-record evaluation split is unverified.",
        },
        "limitations": [
            "References are overall summaries, not atomic decision/action/owner/deadline gold labels.",
            "The official transcript has no timestamps; segment milliseconds are turn indices only.",
            "Reference similarity cannot establish meeting-level human adoption or factual accuracy.",
            "The pinned repository test count differs from the original paper's 21 overall test summaries.",
        ],
        "cases": cases,
    }
    if review_cap_chars != DEFAULT_AGENT_REVIEW_LIMIT_CHARS:
        manifest["agent_review_input_limit_chars"] = review_cap_chars
    return manifest


def load_manifest(path: Path | str = DEFAULT_MANIFEST) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("dataset_id") not in {
        "vcsum_long_dev", "vcsum_long_test_repo26"
    }:
        raise ValueError("Not a supported VCSum manifest")
    for case in data.get("cases", []):
        if sha256_text(case["transcript"]) != case["transcript_sha256"]:
            raise ValueError(f"Transcript checksum mismatch: {case['id']}")
        if sha256_text(case["reference_overall"]) != case["reference_sha256"]:
            raise ValueError(f"Reference checksum mismatch: {case['id']}")
    return data


def load_references(path: Path | str = DEFAULT_MANIFEST) -> dict[str, str]:
    return {case["id"]: case["reference_overall"] for case in load_manifest(path)["cases"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--fetch", action="store_true", help="Fetch pinned official files if missing")
    parser.add_argument("--all-dev", action="store_true", help="Include all 22 official dev records")
    parser.add_argument("--agent-review-limit-chars", type=int, default=DEFAULT_AGENT_REVIEW_LIMIT_CHARS)
    args = parser.parse_args()
    if args.agent_review_limit_chars <= 0:
        parser.error("--agent-review-limit-chars must be positive")
    if args.split == "test":
        if args.all_dev:
            parser.error("--all-dev applies only to --split dev")
        ensure_test_source(args.source_dir, fetch=args.fetch)
        manifest = build_test_manifest(args.source_dir, review_cap_chars=args.agent_review_limit_chars)
        output_path = args.manifest or DEFAULT_TEST_MANIFEST
    else:
        ensure_sources(args.source_dir, fetch=args.fetch)
        manifest = build_manifest(
            args.source_dir, all_dev=args.all_dev, review_cap_chars=args.agent_review_limit_chars
        )
        output_path = args.manifest or DEFAULT_MANIFEST
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "manifest": str(output_path),
                "source_revision": REVISION,
                "split": args.split,
                "pool_count": manifest["selection"]["pool_count"],
                "selected_ids": manifest["selection"]["selected_meeting_ids"],
                "transcript_chars": [case["transcript_chars"] for case in manifest["cases"]],
                "estimated_total_model_calls_max_2_agent_rounds": manifest["selection"]["estimated_total_model_calls_max_2_agent_rounds"],
                "agent_review_over_limit": [case["id"] for case in manifest["cases"] if case["agent_review_transcript_over_limit"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()


def serialize_holdout_case(
    source: dict[str, Any],
    source_line_sha256: str,
    *,
    review_cap_chars: int = DEFAULT_AGENT_REVIEW_LIMIT_CHARS,
) -> dict[str, Any]:
    """留出集 case：**没有参考摘要**（test/dev 之外从未被消费过的会议）。

    转写构造与 `serialize_case` 逐行同构（同样的轮次/说话人对齐与 sha 口径），
    唯一区别是 reference_overall=None——留出集服务于不需要参考摘要的仪器
    （完整性清单、会议级判读、契约/引用指标），以及未来任何配置变更的
    "未见数据"验证。ROUGE 不适用（无参考）。
    """
    meeting_id = str(source["id"])
    turns, speakers = source["context"], source["speaker"]
    if not isinstance(turns, list) or len(turns) != len(speakers) or not turns:
        raise ValueError(f"Invalid context/speaker alignment: {meeting_id}")
    segments: list[dict[str, Any]] = []
    transcript_lines: list[str] = []
    utterance_count = 0
    for turn_index, (utterances, speaker_id) in enumerate(zip(turns, speakers), 1):
        if not isinstance(utterances, list) or not utterances or not all(
            isinstance(item, str) for item in utterances
        ) or not any(item.strip() for item in utterances):
            raise ValueError(f"Invalid utterance list: {meeting_id}, turn {turn_index}")
        speaker = f"发言人{speaker_id}"
        text = "\n".join(utterances)
        segments.append({
            "start_ms": (turn_index - 1) * 1000,
            "end_ms": turn_index * 1000,
            "speaker": speaker,
            "source_speaker_id": speaker_id,
            "text": text,
            "utterances": utterances,
            "turn_index": turn_index,
        })
        transcript_lines.append(f"[第{turn_index}轮][{speaker}] {text}")
        utterance_count += len(utterances)
    transcript = "\n".join(transcript_lines)
    chunk_count = estimated_generation_chunks(segments)
    generation_calls = _canonical_estimator().estimated_generation_calls(
        {"id": f"vcsum_{meeting_id}", "segments": segments, "transcript": transcript}
    )
    return {
        "id": f"vcsum_{meeting_id}",
        "source_meeting_id": meeting_id,
        "av_num": source["av_num"],
        "split": "holdout",
        "title": f"VCSum {meeting_id}",
        "transcript": transcript,
        "segments": segments,
        "reference_overall": None,
        "transcript_sha256": sha256_text(transcript),
        "reference_sha256": None,
        "source_sha256": source_line_sha256,
        "source_context_record_sha256": source_line_sha256,
        "source_reference_record_sha256": None,
        "original_utterance_chars": transcript_size(source),
        "transcript_chars": len(transcript),
        "speaker_count": len(set(speakers)),
        "turn_count": len(turns),
        "utterance_count": utterance_count,
        "estimated_generation_chunks": chunk_count,
        "estimated_generation_model_calls": generation_calls,
        "estimated_total_model_calls_max_2_agent_rounds": generation_calls + 2 * MAX_AGENT_ROUNDS_FOR_BUDGET,
        "timestamp_kind": "synthetic_turn_index",
        "timestamps_synthetic_or_indexed": True,
        "agent_review_input_limit_chars": review_cap_chars,
        "agent_review_transcript_over_limit": len(transcript) > review_cap_chars,
        "reference_scope": {
            "overall_summary": False,
            "decision_facts": False,
            "action_owner": False,
            "action_deadline": False,
            "human_adoption": False,
        },
    }


def build_holdout_manifest(
    source_dir: Path = SOURCE_DIR, *,
    count: int = 6,
    seed: int = 20260926,
    review_cap_chars: int = DEFAULT_AGENT_REVIEW_LIMIT_CHARS,
) -> dict[str, Any]:
    """从 239 场里排除 dev22+test26 已消费的 48 场，固定种子抽留出集。

    为什么必须有这个：2026-09-26 审计发现 dev22/test26 都在同一调试周期内被
    反复消费（换模型、改分块、加门控都在 test 上观察效果），VCSum 侧没有任何
    留出集。此后**任何配置变更的泛化声称必须先在这批未见会议上验证**。
    """
    import random

    ensure_test_source(source_dir)
    context = read_jsonl(source_dir / "vcsum_data/overall_context.txt")
    dev_references = read_jsonl(source_dir / "vcsum_data/long_dev.txt")
    test_references = read_jsonl(source_dir / TEST_REFERENCE_FILE)
    consumed = set(dev_references) | set(test_references)
    pool = {
        meeting_id: record for meeting_id, record in context.items()
        if meeting_id not in consumed
    }
    if not pool:
        raise ValueError("留出池为空：dev/test 之外没有剩余会议")
    rng = random.Random(seed)
    picked = sorted(rng.sample(sorted(pool), min(count, len(pool))), key=int)
    cases = [
        serialize_holdout_case(record, line_sha256, review_cap_chars=review_cap_chars)
        for meeting_id in picked
        for record, line_sha256 in [pool[meeting_id]]
    ]
    manifest = {
        "schema_version": 1,
        "dataset_id": f"vcsum_long_holdout{len(cases)}",
        "synthetic": False,
        "language": "zh-CN",
        "task": "overall_meeting_summary",
        "source": {
            "repository_url": REPOSITORY_URL,
            "revision": REVISION,
            "split": "holdout",
            "repository_license": "MIT",
            "license_note": "Repository LICENSE is MIT; no separate per-record rights statement is provided.",
            "files": [
                {"path": relative, "url": f"{RAW_BASE}/{relative}", "sha256": digest}
                for relative, digest in SOURCE_FILES.items()
            ],
            "conversation_structure": "Each outer context item is a speaker turn; its inner utterances retain original order.",
        },
        "selection": {
            "method": f"seeded_sample_excluding_consumed_dev22_test26_seed{seed}",
            "consumed_meeting_ids_excluded": sorted(consumed, key=int),
            "pool_count": len(pool),
            "selected_count": len(cases),
            "selected_meeting_ids": [case["source_meeting_id"] for case in cases],
            "estimated_total_model_calls_max_2_agent_rounds": sum(
                case["estimated_total_model_calls_max_2_agent_rounds"] for case in cases
            ),
            "note": "这些会议从未进入任何一次运行；用于配置变更的未见数据验证。"
                    "无参考摘要：ROUGE 不适用，契约/完整性/会议级仪器可用。",
        },
        "cases": cases,
    }
    return manifest
