#!/usr/bin/env python3
"""Public meeting corpora -> AIMeeting baseline/reflection benchmark; dry-run by default.

No previous evaluation allowance authorizes a new live run. Live requires both
--mode live and a separately authorized --authorized-llm-calls argument.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from bench_common import (api_request, credentials, estimated_generation_calls,
                                 normalized_segments, schema_errors, write_json)

# 授权上限的护栏值（防失控），**不是**预算审批——审批由 --authorized-llm-calls 承担。
#
# 取值依据（2026-09-25 收尾时按实测重定；测试期曾临时取 600 以便任务一次跑完）：
#   * 记录在案的最大单次计划预留 = **202 次**（test26 全量 26 场 × 6000 字分块：
#     98 次分块 + 104 次自检，test_public_runner 有断言）；
#   * 单次运行实际派发的最大 = 119 次（`vcsum-20260925T130415Z-2f6c5270`，21 场）；
#   * **300 = 202 × 1.49**：容得下 26 场全量再加约五成返工，同时仍能在失控时叫停。
# 计划预留本身是硬约束（逐场写前记账、失败不退预算），实际派发不会超过预留，
# 因此这个值只需覆盖"最大的合法计划"。
# 抬升时必须同时更新 `manage_fixture.py` 的同类上界（PUBLIC_RUN_MAX_CALLS）
# 与 test_public_runner 的交叉校验——两处不一致会让护栏静默失效。
MAX_CALLS = 300
LEGACY_AGENT_CONTEXT_CHARS = 20000
VCSUM_DATASET_IDS = frozenset({"vcsum_long_dev", "vcsum_long_test_repo26"})
ALIMEETING_DATASET_ID = "modelscope/Alimeeting4MUG"


def dataset_name(manifest: dict) -> str:
    dataset_id = manifest.get("dataset_id")
    if dataset_id in VCSUM_DATASET_IDS:
        return "VCSum"
    if dataset_id == ALIMEETING_DATASET_ID:
        return "AliMeeting4MUG"
    raise ValueError(f"Unsupported public benchmark dataset_id: {dataset_id!r}")


def metric_field(manifest: dict) -> str:
    return "reference_metrics" if dataset_name(manifest) == "VCSum" else "quality_metrics"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def agent_context_limit(manifest: dict) -> int:
    """Read the cap from a new manifest while preserving locked legacy manifests."""
    limit = manifest.get("agent_review_input_limit_chars", LEGACY_AGENT_CONTEXT_CHARS)
    if type(limit) is not int or not 1 <= limit <= 1_000_000:
        raise ValueError("Manifest agent review input limit must be a positive integer")
    return limit


def load_inputs(path: Path, selected: list[str]) -> tuple[dict, list[dict], str]:
    if not path.resolve().is_relative_to(PROJECT):
        raise ValueError("Manifest must be inside this project")
    raw = path.read_bytes()
    header = json.loads(raw)
    if not isinstance(header, dict):
        raise ValueError("Public benchmark manifest must be a JSON object")
    if header.get("dataset_id") in VCSUM_DATASET_IDS:
        from vcsum_adapter import load_manifest
    elif header.get("dataset_id") == ALIMEETING_DATASET_ID:
        from alimeeting_adapter import load_manifest
    else:
        raise ValueError(f"Unsupported public benchmark dataset_id: {header.get('dataset_id')!r}")
    manifest = load_manifest(path)
    name = dataset_name(manifest)
    expected_limit = agent_context_limit(manifest)
    cases = manifest.get("cases", [])
    ids = set()
    for case in cases:
        case_id = case.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", case_id) or case_id in ids:
            raise ValueError("Manifest case IDs must be unique safe identifiers")
        ids.add(case_id)
        if not isinstance(case.get("transcript"), str) or not case["transcript"].strip():
            raise ValueError(f"{case_id}: missing public transcript")
        if name == "VCSum" and (not isinstance(case.get("reference_overall"), str)
                                  or not case["reference_overall"].strip()):
            raise ValueError(f"{case_id}: missing official reference summary")
        checks = [("transcript_sha256", "transcript")]
        if name == "VCSum":
            checks.append(("reference_sha256", "reference_overall"))
        for field, source in checks:
            expected = case.get(field)
            actual = sha256(case[source].encode("utf-8"))
            if not expected or expected != actual:
                raise ValueError(f"{case_id}: {field} is missing or differs from the manifest content")
        case_limit = case.get("agent_review_input_limit_chars", LEGACY_AGENT_CONTEXT_CHARS)
        if type(case_limit) is not int or case_limit != expected_limit:
            raise ValueError(f"{case_id}: agent review input limit differs from manifest")
        if case.get("agent_review_transcript_over_limit") is not (len(case["transcript"]) > expected_limit):
            raise ValueError(f"{case_id}: agent review truncation flag differs from transcript length")
        normalized_segments(case)
    chosen = [case for case in cases if not selected or case["id"] in selected]
    if not chosen or set(selected) - ids:
        raise ValueError("Unknown or empty --case-id selection")
    return manifest, chosen, sha256(raw)


def make_plan(cases: list[dict], rounds: int, allowance: int | None) -> dict:
    limits = {case.get("agent_review_input_limit_chars", LEGACY_AGENT_CONTEXT_CHARS) for case in cases}
    if len(limits) != 1:
        raise ValueError("All selected cases must use one agent review input limit")
    context_limit = limits.pop()
    rows = []
    for case in cases:
        generation = estimated_generation_calls(case)
        length = len(case["transcript"])
        row = {
            "case_id": case["id"], "split": case.get("split"),
            "transcript_sha256": case["transcript_sha256"],
            "input_char_count": length, "generation_max_calls": generation,
            # 每轮自检现在是 **3** 次调用：审查 + 依据支撑复核 + 重写。
            # 支撑复核是 2026-09-26 加的一层（依据"能定位"不等于"能支撑该判定"，
            # 独立模型族在业界口径下只认可 25% 的 UNSUPPORTED 意见）。
            # 实测教训：计划仍按 2 次/轮预留时，逐场预算会在第二轮复核处耗尽，
            # 报 "Case LLM budget exhausted before dispatch" —— 看起来像模型失败，
            # 实际是**预算没跟着调用结构更新**。
            "agent_max_calls": 3 * rounds,
            # 每场 +2 的重试余量：invoke_json 的瞬态退避与 re-ask 都是真实调用
            # （GuardedModel 逐派发计费），预留只按理想路径算会在超时重试处耗尽
            # （实测：R6 首场 AGENT_REFINE 超时重试被拒，"budget exhausted"）。
            "reserved_calls": generation + 3 * rounds + 2,
            "agent_context_limit_chars": context_limit,
            "agent_source_context_chars": min(length, context_limit),
            "agent_context_truncated_by_product": length > context_limit,
            "timestamp_kind": case.get("timestamp_kind", "not_specified"),
            "timestamps_synthetic_or_indexed": case.get("timestamps_synthetic_or_indexed"),
        }
        if "reference_sha256" in case:
            row["reference_sha256"] = case["reference_sha256"]
        rows.append(row)
    total = sum(row["reserved_calls"] for row in rows)
    return {"cases": rows, "max_rounds": rounds, "reserved_llm_calls": total,
            "agent_context_limit_chars": context_limit,
            "max_llm_calls": allowance, "absolute_runner_cap": MAX_CALLS,
            "within_authorized_budget": None if allowance is None else total <= allowance,
            "requires_new_explicit_authorization_for_live": allowance is None,
            "live_calls_made": 0,
            "scope_note": f"Full adapted transcript goes to minutes generation; reflection receives at most the first {context_limit} characters after product-cap preflight. No ASR is run."}


def source_metadata(manifest: dict, case: dict, manifest_hash: str) -> dict:
    # Allowlisted provenance: no official reference text crosses into the model process.
    source = manifest.get("source")
    revision = (source.get("revision") or source.get("metadata_git_revision")) if isinstance(source, dict) else manifest.get("source_revision")
    if dataset_name(manifest) == "AliMeeting4MUG":
        return {"dataset_name": "AliMeeting4MUG", "dataset_kind": "official_public_corpus",
                "dataset_id": ALIMEETING_DATASET_ID, "manifest_sha256": manifest_hash,
                "case_id": case["id"], "split": case.get("split"),
                "transcript_sha256": case["transcript_sha256"],
                "source_archive_sha256": case.get("source_archive_sha256"),
                "source_record_sha256": case.get("source_record_sha256"),
                "timestamp_kind": case.get("timestamp_kind"),
                "timestamps_synthetic_or_indexed": case.get("timestamps_synthetic_or_indexed"),
                "input_char_count": len(case["transcript"]),
                "speaker_count": case.get("speaker_count"), "source_revision": revision}
    return {"dataset_name": "VCSum", "dataset_kind": "official_public_corpus",
            "manifest_sha256": manifest_hash, "case_id": case["id"],
            "split": case.get("split"),
            "source_context_record_sha256": case.get("source_context_record_sha256"),
            "source_reference_record_sha256": case.get("source_reference_record_sha256"),
            "transcript_sha256": case["transcript_sha256"],
            "reference_sha256": case["reference_sha256"],
            "timestamp_kind": case.get("timestamp_kind"),
            "timestamps_synthetic_or_indexed": case.get("timestamps_synthetic_or_indexed"),
            "input_char_count": len(case["transcript"]),
            "speaker_count": case.get("speaker_count"),
            "source_revision": revision}


def case_request(manifest: dict, case: dict, manifest_hash: str, *, token: str,
                 run_id: str, meeting_id: int, rounds: int, case_limit: int,
                 run_limit: int, results_subdir: str, timeout_seconds: int) -> dict:
    """Allowlist model-side inputs; reference text is reserved for local scoring."""
    return {"action": "run_case", "token": token, "run_id": run_id, "case_id": case["id"],
            "meeting_id": meeting_id, "source": source_metadata(manifest, case, manifest_hash),
            "transcript": case["transcript"], "segments": normalized_segments(case),
            "agent_context_limit_chars": agent_context_limit(manifest),
            "max_rounds": rounds, "max_llm_calls": case_limit,
            "run_max_llm_calls": run_limit, "results_subdir": results_subdir,
            "timeout_seconds": timeout_seconds}


def management_request(args: argparse.Namespace, payload: dict) -> dict:
    source = (HERE / "manage_fixture.py").read_text(encoding="utf-8")
    command = ["docker", "exec", "-i", "--workdir", args.container_app_dir]
    # --arm-env 注入的环境变量对管理进程生效（minutes_service 等在导入时读 env），
    # 用于受控机制臂；键值对会经 redacted_arm_env 留痕进报告
    for pair in getattr(args, "arm_env", None) or []:
        command += ["-e", pair]
    command += [args.container, "python", "-c", source]
    completed = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True)
    marker = "AIMEETING_PUBLIC_EVAL_RESULT="
    lines = [line[len(marker):] for line in completed.stdout.splitlines() if line.startswith(marker)]
    if not lines:
        raise RuntimeError(f"Management process returned no result (exit {completed.returncode}); no retry performed. Inspect durable public benchmark artifacts.")
    result = json.loads(lines[-1])
    if "error" in result:
        raise RuntimeError(f"Management precondition failed: {result['error']}")
    return result


def validate_arm_env(pairs: list[str]) -> None:
    """--arm-env 必须是 KEY=VAL：键为合法环境变量名，值允许为空（显式置空也是口径）。"""
    for pair in pairs:
        key, sep, _value = pair.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"--arm-env 必须是 KEY=VAL 形式（收到 {pair!r}）")


def redacted_arm_env(pairs: list[str]) -> dict[str, str]:
    """留痕用：开关类原样记录 KEY=VAL；凭据类（_KEY/_TOKEN/_SECRET 结尾）只记键名。

    机制臂的环境注入影响测量口径，必须随报告留痕（评测宪法第 5/6 条）；
    但 AGENT_REVIEW_API_KEY 这类值不能落盘。
    """
    redacted: dict[str, str] = {}
    for pair in pairs:
        key, _sep, value = pair.partition("=")
        redacted[key] = ("<redacted>" if key.endswith(("_KEY", "_TOKEN", "_SECRET")) else value)
    return redacted


def telemetry_summary(records: list[dict]) -> dict:
    keys = ("actual_llm_dispatch_count", "input_tokens", "output_tokens", "total_tokens", "llm_elapsed_ms_sum")
    return {"case_count": len(records), "completed_case_count": sum(r.get("status") == "completed" for r in records),
            **{key: sum(r.get("telemetry", {}).get(key, 0) for r in records) for key in keys}}


def score_ali_records(records: list[dict], cases: list[dict]) -> dict:
    """Score only observable processing and schema outcomes; action gold needs alignment."""
    planned = {case["id"] for case in cases}
    if not planned:
        raise ValueError("AliMeeting4MUG scoring needs at least one planned case")
    seen = set()
    completed = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("AliMeeting4MUG case results must be objects")
        case_id = record.get("case_id")
        if case_id not in planned or case_id in seen:
            raise ValueError(f"Unknown or duplicate AliMeeting4MUG case result: {case_id!r}")
        seen.add(case_id)
        if record.get("status") == "completed":
            completed.append(record)
    structure = {}
    for stage in ("baseline", "revised"):
        valid = 0
        for record in completed:
            block = record.get(stage)
            output = block.get("output") if isinstance(block, dict) else None
            valid += not schema_errors(output)
        structure[stage] = {"schema_valid_count": valid,
                            "schema_valid_rate_among_completed": valid / len(completed) if completed else None,
                            "schema_valid_rate_among_planned": valid / len(planned)}
    return {
        "benchmark": "AliMeeting4MUG product-processing diagnostic",
        "gold_scoring_status": "gold_not_scored_without_human_alignment",
        "interpretation": "Completion and output schema validity only; no action-item precision, recall, F1, or factuality claim.",
        "processing": {"planned_case_count": len(planned), "attempted_case_count": len(records),
                       "completed_case_count": len(completed), "completion_rate": len(completed) / len(planned)},
        "structure": structure,
    }


def score_existing(records: list[dict], cases: list[dict], manifest: dict | None = None) -> dict:
    if manifest is not None and dataset_name(manifest) == "AliMeeting4MUG":
        return score_ali_records(records, cases)
    from scoring import score_records
    references = {case["id"]: case["reference_overall"] for case in cases}
    return score_records(records, references)


def add_structure_checks(record: dict) -> dict:
    for stage in ("baseline", "revised"):
        block = record.get(stage)
        if block and block.get("output") is not None:
            errors = schema_errors(block["output"])
            raw_errors = schema_errors(block.get("raw_model_output"))
            block["structure"] = {"schema_valid": not errors, "errors": errors,
                                  "raw_model_schema_valid": not raw_errors, "raw_errors": raw_errors}
    return record


def recover_durable_results(report_path: Path, cases: list[dict], manifest_hash: str,
                            manifest: dict | None = None) -> Path:
    """Merge completed durable results without network, DB writes, or re-execution.

    Preserve the original report and dispatch ledger. An incomplete claimed case
    is explicitly unresolved, never retried or labelled completed by this mode.
    """
    directory = report_path.resolve().parent
    if not directory.is_relative_to((HERE / "results").resolve()):
        raise ValueError("Recovery is restricted to benchmarks/results")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if manifest is None:
        manifest = {"dataset_id": ALIMEETING_DATASET_ID if report.get("dataset_name") == "AliMeeting4MUG"
                    else "vcsum_long_dev"}
    if report.get("manifest_sha256") != manifest_hash:
        raise ValueError("Recovery manifest hash differs from the locked run")
    by_id = {case["id"]: case for case in cases}
    planned = [row["case_id"] for row in report["plan"]["cases"]]
    if set(planned) - set(by_id):
        raise ValueError("Recovery needs all originally planned cases in the supplied manifest")
    records = {record["case_id"]: record for record in report.get("records", [])}
    if len(records) != len(report.get("records", [])) or set(records) - set(planned):
        raise ValueError("Original report contains duplicate or unplanned cases")
    ledger_path = directory / "dispatch-ledger.json"
    ledger_bytes = ledger_path.read_bytes() if ledger_path.exists() else b""
    ledger = json.loads(ledger_bytes) if ledger_bytes else {"dispatches": []}
    if ledger_bytes and (ledger.get("run_id") != report["run_id"]
                         or ledger.get("max_llm_calls") != report["plan"]["max_llm_calls"]):
        raise ValueError("Durable ledger does not belong to this run/budget")
    recovered = []
    for case_id in planned:
        raw_path = directory / f"{case_id}.raw-result.json"
        if case_id in records or not raw_path.is_file():
            continue
        record = json.loads(raw_path.read_text(encoding="utf-8"))
        if (record.get("case_id") != case_id or record.get("source", {}).get("manifest_sha256") != manifest_hash
                or record.get("source", {}).get("transcript_sha256") != by_id[case_id]["transcript_sha256"]):
            raise ValueError(f"{case_id}: durable result identity mismatch")
        dispatches = [row for row in ledger["dispatches"] if row["case_id"] == case_id]
        recorded_ids = [row["dispatch_id"] for row in record.get("model_calls", [])]
        if (recorded_ids != [row["dispatch_id"] for row in dispatches]
                or record.get("telemetry", {}).get("actual_llm_dispatch_count") != len(dispatches)):
            raise ValueError(f"{case_id}: durable result and dispatch ledger disagree")
        record["recovery"] = {"source": raw_path.name, "source_sha256": sha256(raw_path.read_bytes()),
                              "model_calls_during_recovery": 0, "reexecution": False}
        records[case_id] = add_structure_checks(record)
        recovered.append(case_id)
    unresolved = [case_id for case_id in planned if case_id not in records and
                  ((directory / f"{case_id}.claim").exists()
                   or any(row["case_id"] == case_id for row in ledger["dispatches"]))]
    unstarted = [case_id for case_id in planned if case_id not in records and case_id not in unresolved]
    report["records"] = [records[case_id] for case_id in planned if case_id in records]
    report["summary"] = telemetry_summary(report["records"])
    report[metric_field(manifest)] = score_existing(report["records"], cases, manifest)
    all_completed = len(records) == len(planned) and all(r.get("status") == "completed" for r in records.values())
    report["status"] = "recovered_completed" if all_completed else "recovered_partial"
    report["recovery"] = {"original_report": report_path.name, "newly_merged_cases": recovered,
                          "unresolved_started_case_ids": unresolved, "not_recorded_as_started_case_ids": unstarted,
                          "dispatch_ledger_sha256": sha256(ledger_bytes),
                          "durable_dispatch_reservation_count": len(ledger["dispatches"]),
                          "model_calls_during_recovery": 0, "original_report_and_ledger_modified": False,
                          "warning": "Wait until the helper has stopped or completed before recovery. Unresolved claimed cases must be reconciled; do not delete claims or retry them."}
    output = directory / "recovered-report.json"
    if output.resolve() == report_path.resolve():
        raise ValueError("Use the original report.json as recovery input, not recovered-report.json")
    write_json(output, report)
    if ledger_path.exists() and ledger_path.read_bytes() != ledger_bytes:
        raise RuntimeError("Ledger changed during recovery; helper may still be running. Treat recovered-report.json as a partial snapshot.")
    return output


def authenticate_and_preflight(args: argparse.Namespace, expected_agent_context_limit: int) -> tuple[dict, dict]:
    username, password = credentials(args)
    user = api_request(args.base_url, "/login", {"username": username, "password": password})
    metadata = management_request(args, {"action": "preflight", "token": user["token"],
                                         "expected_agent_context_limit_chars": expected_agent_context_limit})
    if metadata.get("agent_context_limit_chars") != expected_agent_context_limit:
        raise ValueError("Product agent review input limit differs from the locked manifest")
    return user, metadata


def run_live(args: argparse.Namespace, manifest: dict, cases: list[dict], manifest_hash: str, plan: dict) -> Path:
    if args.authorized_llm_calls is None:
        raise ValueError("A new public run needs --authorized-llm-calls; the previous 30-call allowance is not reused")
    if not plan["within_authorized_budget"]:
        raise ValueError("Worst-case call count exceeds the new allowance; select fewer cases or rounds before live")
    name = dataset_name(manifest)
    run_id = ("vcsum-" if name == "VCSum" else "alimeeting4mug-") + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    directory = (args.output_dir or HERE / "results" / run_id).resolve()
    try:
        relative = str(directory.relative_to((HERE / "results").resolve()))
    except ValueError:
        raise ValueError("Live output directory must be under evaluation/benchmarks/results") from None
    if not re.fullmatch(r"[A-Za-z0-9_/-]{1,180}", relative):
        raise ValueError("Unsafe result directory name")
    directory.mkdir(parents=True, exist_ok=False)
    original_bytes = args.manifest.read_bytes()
    if sha256(original_bytes) != manifest_hash:
        raise ValueError("Manifest changed after preflight")
    (directory / "manifest.lock.json").write_bytes(original_bytes)
    report = {"schema_version": 1, "run_id": run_id, "mode": "live", "dataset_name": name,
              "dataset_id": manifest["dataset_id"],
              "synthetic": False, "asr_bypassed": True, "manifest_sha256": manifest_hash,
              "status": "starting", "plan": plan, "reserved_llm_calls": 0, "records": [],
              "arm_env": redacted_arm_env(getattr(args, "arm_env", None) or []),
              "authorization": {"new_run_authorized_call_limit": args.authorized_llm_calls,
                                "does_not_reuse_previous_evaluation_allowance": True},
              "method": "HTTP login and meeting creation; budget-guarded original product services; no reference injected",
              "limitations": []}
    if name == "VCSum":
        report["limitations"] = [
            "No ASR quality measurement; turn-index timestamps are not measured meeting times.",
            f"Product Agent review source cap is {plan['agent_context_limit_chars']} characters; selected transcripts exceeding it are flagged in the plan.",
            "ROUGE compares output.summary with official overall summary; it is not factual accuracy.",
            "A full-pipeline pre/post comparison is not a causal ablation of reflection alone."]
    else:
        report["limitations"] = [
            "No ASR quality measurement; supplied transcripts bypass audio.",
            f"Product Agent review source cap is {plan['agent_context_limit_chars']} characters; selected transcripts exceeding it are flagged in the plan.",
            "Action IDs are not aligned with output decisions and pending items; no automatic action-item accuracy is reported.",
            "A full-pipeline pre/post comparison is not a causal ablation of reflection alone."]
    report_path = directory / "report.json"
    write_json(report_path, report)
    try:
        user, preflight = authenticate_and_preflight(args, plan["agent_context_limit_chars"])
        report["preflight"] = preflight
        write_json(report_path, report)
        for case, reservation in zip(cases, plan["cases"]):
            report["reserved_llm_calls"] += reservation["reserved_calls"]
            if report["reserved_llm_calls"] > args.authorized_llm_calls:
                raise RuntimeError("Call reservation invariant violated")
            report["status"] = "running"
            report["active_case"] = case["id"]
            write_json(report_path, report)
            # UI fixture date is execution time, not claimed to be a corpus meeting date.
            fixture_date = datetime.now(timezone.utc).replace(microsecond=0)
            meeting = api_request(args.base_url, "/meeting/add", {
                "title": f"[PUBLIC-EVAL:{run_id}] {case.get('title') or case['id']}",
                "start_time": fixture_date.isoformat(), "end_time": (fixture_date + timedelta(hours=1)).isoformat(),
                "host_id": user["id"], "participant_ids": [user["id"]],
                "location": f"{name} public corpus fixture; time fields are not original meeting times",
                "agenda": f"{name}; case_id={case['id']}; manifest_sha256={manifest_hash}",
            }, user["token"])
            report["active_meeting_id"] = meeting["id"]
            write_json(report_path, report)
            record = management_request(args, case_request(
                manifest, case, manifest_hash, token=user["token"], run_id=run_id,
                meeting_id=meeting["id"], rounds=args.max_rounds,
                case_limit=reservation["reserved_calls"], run_limit=args.authorized_llm_calls,
                results_subdir=relative, timeout_seconds=args.case_timeout,
            ))
            add_structure_checks(record)
            write_json(directory / f"{case['id']}.json", record)
            report["records"].append(record)
            report["summary"] = telemetry_summary(report["records"])
            # Metrics are a local deterministic computation, never a paid judge.
            report[metric_field(manifest)] = score_existing(report["records"], cases, manifest)
            write_json(report_path, report)
            print(f"{case['id']}: {record['status']}, calls={record.get('telemetry', {}).get('actual_llm_dispatch_count', 0)}", flush=True)
            if record["status"] != "completed":
                raise RuntimeError(f"{case['id']} did not complete; stopped without retry")
        report["status"] = "completed"
        report.pop("active_case", None)
        report.pop("active_meeting_id", None)
    except BaseException as error:
        report["status"] = "failed_or_interrupted"
        report["failure_type"] = type(error).__name__
        # 只记类型会让失败**不可诊断**：实测有两个运行目录留下了 0 调用、0 完成的
        # 报告，里面只有一句 "RuntimeError"，没有任何可用于判断成因的信息。
        # 这里同时记消息。消息的安全性由上游保证：容器内的管理进程只把
        # ValueError/FileExistsError 的文本回传，其余一律只回传类型名（见
        # manage_public_fixture 的入口），因此落到报告里的不会是带凭据的上游原文。
        report["failure_message"] = (str(error) or type(error).__name__)[:2000]
        write_json(report_path, report)
        raise
    write_json(report_path, report)
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry-run", "preflight", "offline", "recover", "live"), default="dry-run")
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--authorized-llm-calls", type=int,
                        help=f"separate explicit allowance, 1..{MAX_CALLS}; no live default")
    parser.add_argument("--case-timeout", type=int, default=3600)
    parser.add_argument("--base-url", default="http://127.0.0.1:19091")
    parser.add_argument("--container", default="aimeeting-local-backend-1")
    parser.add_argument("--container-app-dir", default="/app")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--username")
    parser.add_argument("--password-env", default="AIMEETING_EVAL_PASSWORD")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--arm-env", action="append", default=[], metavar="KEY=VAL",
                        help="注入管理进程的环境变量（KEY=VAL），"
                             "用于受控机制臂；键值经脱敏后留痕进报告")
    parser.add_argument("--results", type=Path, help="offline: saved public report.json")
    args = parser.parse_args(argv)
    try:
        validate_arm_env(args.arm_env)
        if not 1 <= args.max_rounds <= 5 or args.case_timeout <= 0:
            raise ValueError("max-rounds must be 1..5 and case-timeout must be positive")
        if args.authorized_llm_calls is not None and not 1 <= args.authorized_llm_calls <= MAX_CALLS:
            raise ValueError(f"The separately authorized call limit must be 1..{MAX_CALLS}")
        manifest, cases, manifest_hash = load_inputs(args.manifest, args.case_id)
        plan = make_plan(cases, args.max_rounds, args.authorized_llm_calls)
        if args.mode == "dry-run":
            print(json.dumps({"mode": "dry-run", "dataset_name": dataset_name(manifest),
                              "manifest_sha256": manifest_hash, **plan}, ensure_ascii=False, indent=2))
            return 2 if plan["within_authorized_budget"] is False else 0
        if args.mode == "preflight":
            _, metadata = authenticate_and_preflight(args, plan["agent_context_limit_chars"])
            result = {"mode": "preflight", "dataset_name": dataset_name(manifest),
                      "manifest_sha256": manifest_hash, "plan": plan,
                      "application": metadata, "model_calls": 0, "business_rows_created": 0}
            if args.output_dir:
                if not args.output_dir.resolve().is_relative_to(HERE):
                    raise ValueError("Preflight output must remain within benchmarks")
                write_json(args.output_dir / "preflight.json", result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.mode == "offline":
            if not args.results:
                raise ValueError("offline requires --results")
            report = json.loads(args.results.read_text(encoding="utf-8"))
            if report.get("manifest_sha256") != manifest_hash:
                raise ValueError("Manifest differs from the locked run")
            chosen = {case["id"] for case in cases}
            records = [row for row in report["records"] if row["case_id"] in chosen]
            result = {"mode": "offline", "dataset_name": dataset_name(manifest),
                      "manifest_sha256": manifest_hash, "summary": telemetry_summary(records)}
            result[metric_field(manifest)] = score_existing(records, cases, manifest)
            if args.output_dir:
                if not args.output_dir.resolve().is_relative_to(HERE):
                    raise ValueError("Offline output must remain within benchmarks")
                write_json(args.output_dir / "rescored.json", result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.mode == "recover":
            if not args.results:
                raise ValueError("recover requires --results pointing to the original public report.json")
            print(recover_durable_results(args.results, cases, manifest_hash, manifest))
            return 0
        print(run_live(args, manifest, cases, manifest_hash, plan))
        return 0
    except (ValueError, OSError, RuntimeError, ImportError) as error:
        print(f"Public benchmark stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
