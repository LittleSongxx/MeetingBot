"""Public meeting-corpus evaluation management command; stdin JSON, no HTTP route.

Only public_benchmark/run_public_eval.py invokes this script. The real product services run unchanged in
this process, with a persistent budget guard around their actual LLM dispatch.
It requires the application at the working directory and /evaluation mounted.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from time import perf_counter
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class BudgetExceeded(RuntimeError):
    pass


# 单次运行的调用数硬上限，与 `run_public_eval.py` 的 `MAX_CALLS` 是同一个量：
# 那边算计划、这边守执行，两个值必须相等，否则会出现"计划批准了、执行却拒绝"
# 或者更糟的"执行放行了、计划没算到"。本文件以源码字符串形式在容器内执行，
# 不能 import 同目录模块，因此这里**再声明一次**并加了交叉校验测试
# （test_public_runner.test_guardrail_matches_the_runner_value）。
# 取值依据见 run_public_eval.py 的注释：最大单次计划预留 202 次，护栏取 300。
PUBLIC_RUN_MAX_CALLS = 300


class CallBudget:
    """Write-ahead per-run budget; uncertainty consumes budget, never refunds it."""

    def __init__(self, request: dict):
        self.run_id = request["run_id"]
        self.case_id = request["case_id"]
        for value in (self.run_id, self.case_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
                raise ValueError("Unsafe evaluation identifier")
        self.case_limit = int(request["max_llm_calls"])
        self.run_limit = int(request["run_max_llm_calls"])
        if not 1 <= self.case_limit <= self.run_limit <= PUBLIC_RUN_MAX_CALLS:
            raise ValueError(
                f"Evaluation budget must be between 1 and {PUBLIC_RUN_MAX_CALLS}"
            )
        subdir = request.get("results_subdir", self.run_id)
        if not re.fullmatch(r"[A-Za-z0-9_/-]{1,180}", subdir) or ".." in subdir or subdir.startswith("/"):
            raise ValueError("Unsafe result directory")
        self.directory = Path("/evaluation/benchmarks/results") / subdir
        report = self.directory / "report.json"
        if not report.is_file():
            raise ValueError("Mount the host evaluation directory at /evaluation before live execution")
        host_ledger = json.loads(report.read_text(encoding="utf-8"))
        planned = {item["case_id"]: item["reserved_calls"] for item in host_ledger["plan"]["cases"]}
        if (host_ledger.get("run_id") != self.run_id or host_ledger["plan"]["max_llm_calls"] != self.run_limit
                or planned.get(self.case_id) != self.case_limit or host_ledger.get("active_case") != self.case_id):
            raise ValueError("Host reservation does not match management request")
        self.path = self.directory / "dispatch-ledger.json"
        self.lock_path = self.directory / "dispatch-ledger.lock"
        # Do not allow a second management process to run this case, even after a crash.
        claim = self.directory / f"{self.case_id}.claim"
        with claim.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({"case_id": self.case_id, "claimed_at": utc_now()}))
            stream.flush()
            os.fsync(stream.fileno())

    @contextmanager
    def locked(self):
        with self.lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
                "run_id": self.run_id, "max_llm_calls": self.run_limit, "dispatches": [],
            }
            if data["run_id"] != self.run_id or data["max_llm_calls"] != self.run_limit:
                raise ValueError("Persisted budget identity changed")
            yield data
            atomic_json(self.path, data)

    def reserve(self, call_type: str) -> str:
        with self.locked() as ledger:
            if len(ledger["dispatches"]) >= self.run_limit:
                raise BudgetExceeded("Run LLM budget exhausted before dispatch")
            if sum(row["case_id"] == self.case_id for row in ledger["dispatches"]) >= self.case_limit:
                raise BudgetExceeded("Case LLM budget exhausted before dispatch")
            request_id = uuid4().hex
            ledger["dispatches"].append({"dispatch_id": request_id, "case_id": self.case_id,
                                          "call_type": call_type, "status": "DISPATCH_RESERVED",
                                          "started_at": utc_now()})
        return request_id

    def finish(self, dispatch_id: str, result: dict) -> None:
        with self.locked() as ledger:
            for row in ledger["dispatches"]:
                if row["dispatch_id"] == dispatch_id:
                    row.update(result)
                    row["finished_at"] = utc_now()
                    return
            raise RuntimeError("Unknown dispatch reservation")


def usage_values(usage: dict | None) -> dict:
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens)}


def verify_agent_context_limit(expected: int, actual: int) -> None:
    """Reject a stale product image before any fixture or model dispatch."""
    if type(expected) is not int or expected <= 0 or expected != actual:
        raise ValueError("Product agent review input limit differs from the locked manifest")


async def preflight(expected_agent_context_limit_chars: int) -> dict:
    from models import AiModelConfig, AgentRun, MeetingMinutes, PromptTemplate, TranscriptionTask
    from services.minutes_service import MINUTES_SCHEMA as _MINUTES_SCHEMA  # noqa: F401  单遍生成的结构约束
    from services.agent_service import MAX_TRANSCRIPT_CHARS
    verify_agent_context_limit(expected_agent_context_limit_chars, MAX_TRANSCRIPT_CHARS)
    configs, prompts = [], []
    for purpose in ("MINUTES", "AGENT"):
        config = await AiModelConfig.filter(model_type=purpose, enabled=True).order_by("-id").first()
        if config is None or not config.api_key:
            raise ValueError(f"No enabled {purpose} configuration with credentials")
        configs.append({"purpose": purpose, "id": config.id, "model_name": config.model_name,
                        "timeout_seconds": config.timeout_seconds, "provider": config.provider})
    for code in ("MEETING_MINUTES", "MINUTES_REVIEW", "MINUTES_REFINE"):
        prompt = await PromptTemplate.get_or_none(code=code, enabled=True)
        if prompt is None:
            raise ValueError(f"Required prompt is unavailable: {code}")
        content = (prompt.system_prompt + "\n" + prompt.user_prompt).encode("utf-8")
        prompts.append({"code": code, "sha256": hashlib.sha256(content).hexdigest()})
    active = {
        "agent_runs": await AgentRun.filter(status="RUNNING").count(),
        "minutes_jobs": await MeetingMinutes.filter(status="GENERATING").count(),
        "transcription_jobs": await TranscriptionTask.filter(status__in=["PENDING", "PROCESSING"]).count(),
    }
    if any(active.values()):
        raise ValueError("Evaluation requires an idle application; active tasks exist")
    # 单遍声明守卫：预算估算器假定生成 = 每场 1 次调用。产品若回到分块/合并
    # 形态（MEETING_MINUTES_CHUNK 模板重新启用），这里必须先失败——
    # 否则预留预算与真实调用数静默分裂（与历史上 chunk 守卫同一条纪律）。
    if await PromptTemplate.filter(code="MEETING_MINUTES_CHUNK", enabled=True).exists():
        raise ValueError(
            "Product re-enabled chunked generation (MEETING_MINUTES_CHUNK); "
            "the budget estimator assumes single-pass (1 call/meeting) and must be updated first"
        )
    thinking = os.getenv("LLM_ENABLE_THINKING", "").strip().lower()
    max_tokens = os.getenv("LLM_MAX_TOKENS", "").strip()
    return {"models": configs, "prompt_hashes": prompts, "active_tasks": active,
            "generation_parameters": {"temperature": 0,
                                      "enable_thinking": thinking == "true" if thinking else None,
                                      "max_tokens": int(max_tokens) if max_tokens else None},
            "generation_mode": "single_pass_long_context", "agent_context_limit_chars": MAX_TRANSCRIPT_CHARS, "llm_probe_calls": 0}


async def run_case(request: dict, user) -> dict:
    from api.meeting import get_accessible_meeting
    from api.meeting_material import MATERIAL_DIR
    from common.model_json import parse_model_json
    from common.times import now
    from models import (AiCallLog, AiModelConfig, AgentRun, AgentStep, MeetingMaterial,
                        MeetingMinutes, TranscriptSegment, TranscriptionTask)
    from services import model_factory
    from services.agent_service import MAX_TRANSCRIPT_CHARS, execute_agent_run, minutes_content
    from services.minutes_service import execute_minutes, transcript_text

    rounds = int(request["max_rounds"])
    timeout = int(request.get("timeout_seconds", 1200))
    if not 1 <= rounds <= 5 or timeout <= 0:
        raise ValueError("Invalid rounds/timeout")
    verify_agent_context_limit(request.get("agent_context_limit_chars"), MAX_TRANSCRIPT_CHARS)
    meeting = await get_accessible_meeting(request["meeting_id"], user)
    if meeting.creator_id != user.id or not meeting.title.startswith(f"[PUBLIC-EVAL:{request['run_id']}] "):
        raise ValueError("Management command only handles this run's owned evaluation meetings")
    # Validate public input identity before creating a fixture or dispatching a model.
    if any("reference" in key.lower() or "gold" in key.lower() for key in request):
        raise ValueError("Official gold data must never enter the management payload")
    dataset = request.get("source", {}).get("dataset_name")
    if dataset not in {"VCSum", "AliMeeting4MUG"}:
        raise ValueError("Unsupported public corpus fixture")
    transcript_hash = hashlib.sha256(request["transcript"].encode("utf-8")).hexdigest()
    if transcript_hash != request.get("source", {}).get("transcript_sha256"):
        raise ValueError("Public transcript hash differs from its locked source metadata")
    budget = CallBudget(request)
    records = []
    original_builder = model_factory.build_chat_model

    def build_guarded(*args, **kwargs):
        # 转发全部参数，**不镜像产品函数的签名**：镜像签名的包装器会在产品新增参数时
        # 静默失效——本项目已经踩过一次（产品新增 prompt_chars 后，这里报
        # "got an unexpected keyword argument 'prompt_chars'"，而错误出现在模型调用路径里，
        # 看起来像模型问题）。
        config = kwargs.get("config") or (args[0] if len(args) > 0 else None)
        schema_name = str(kwargs.get("call_type") or (args[2] if len(args) > 2 else ""))
        delegate = original_builder(*args, **kwargs)
        if getattr(getattr(delegate, "bound", None), "max_retries", None) != 0:
            raise ValueError("Hard dispatch budget requires the product adapter to disable SDK retries")

        class GuardedModel:
            async def ainvoke(self, messages, *call_args, **call_kwargs):
                dispatch_id = budget.reserve(schema_name.upper())
                started = perf_counter()
                record = {"dispatch_id": dispatch_id, "call_type": schema_name.upper(),
                          "model_config_id": config.id, "model_name": config.model_name,
                          "status": "DISPATCHED"}
                records.append(record)
                try:
                    # Product ChatOpenAI sets max_retries=0: one dispatch, no hidden retries.
                    message = await delegate.ainvoke(messages, *call_args, **call_kwargs)
                    record["raw_text"] = str(message.text)
                    try:
                        record["parsed_output"] = parse_model_json(record["raw_text"])
                    except Exception:
                        record["parsed_output"] = None
                    record.update(usage_values(message.usage_metadata))
                    record["status"] = "RESPONSE_RECEIVED"
                    return message
                except BaseException as error:
                    record["status"] = "FAILED_OR_CANCELLED"
                    record["error_type"] = type(error).__name__
                    raise
                finally:
                    record["elapsed_ms"] = int((perf_counter() - started) * 1000)
                    budget.finish(dispatch_id, {key: record[key] for key in
                                  ("status", "error_type", "elapsed_ms", "input_tokens", "output_tokens", "total_tokens")
                                  if key in record})
                    # Persist raw responses in the mounted directory as each call finishes.
                    # This survives host orchestration interruption without another model call.
                    atomic_json(budget.directory / f"{budget.case_id}.responses.json",
                                {"case_id": budget.case_id, "model_calls": records})

        return GuardedModel()

    model_factory.build_chat_model = build_guarded
    minutes = run = None
    baseline = revised = None
    started = perf_counter()
    status, error_type = "failed", None
    ids = {"meeting_id": meeting.id}
    try:
        config = await AiModelConfig.filter(model_type="MINUTES", enabled=True).order_by("-id").first()
        agent_config = await AiModelConfig.filter(model_type="AGENT", enabled=True).order_by("-id").first()
        transcription_config = await AiModelConfig.filter(model_type="TRANSCRIPTION", enabled=True).order_by("-id").first()
        if config is None or agent_config is None:
            raise ValueError("Enabled model configuration disappeared")
        transcript = request["transcript"]
        encoded = transcript.encode("utf-8")
        storage_name = f"public_eval_{request['run_id']}_{request['case_id']}.txt"
        MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
        material_path = MATERIAL_DIR / storage_name
        with material_path.open("xb") as stream:
            stream.write(encoded)
        material = await MeetingMaterial.create(
            meeting_id=meeting.id, file_name=f"{dataset.upper()}_{request['case_id']}.txt", storage_name=storage_name,
            file_type="ATTACHMENT", content_type="text/plain", file_ext="txt", file_size=len(encoded),
            file_hash=hashlib.sha256(encoded).hexdigest(), uploader_id=user.id,
        )
        # This fixture bypasses ASR deliberately. It is not an ASR quality result.
        task = await TranscriptionTask.create(
            meeting_id=meeting.id, material_id=material.id,
            model_config_id=transcription_config.id if transcription_config else config.id,
            initiator_id=user.id, status="SUCCEEDED", stage="Public corpus text fixture (ASR bypassed)",
            progress=100, full_text=transcript, total_duration_ms=max(r["end_ms"] for r in request["segments"]),
            started_at=now(), finished_at=now(),
        )
        await TranscriptSegment.bulk_create([
            TranscriptSegment(task_id=task.id, meeting_id=meeting.id, segment_no=index,
                              start_ms=row["start_ms"], end_ms=row["end_ms"], speaker_label=row["speaker"],
                              original_text=row["text"], content=row["text"])
            for index, row in enumerate(request["segments"], 1)
        ])
        transcript = await transcript_text(task.id)
        if not transcript.strip():
            raise ValueError("Fixture transcript is empty")
        expected = 1 + 3 * rounds
        if expected > budget.case_limit:
            raise ValueError("Actual product pipeline exceeds reserved budget")
        minutes = await MeetingMinutes.create(
            meeting_id=meeting.id, source_task_id=task.id, model_config_id=config.id,
            status="GENERATING", stage="Evaluation generation", created_by=user.id,
        )
        ids.update({"material_id": material.id, "task_id": task.id, "minutes_id": minutes.id})

        async def pipeline():
            nonlocal minutes, run, baseline, revised, status
            generation_started = perf_counter()
            await execute_minutes(minutes.id)
            minutes = await MeetingMinutes.get(id=minutes.id)
            final_generation = next((r for r in reversed(records) if r["call_type"] == "MINUTES"), None)
            baseline = {"output": minutes_content(minutes) if minutes.status == "DRAFT" else None,
                        "raw_model_output": final_generation.get("parsed_output") if final_generation else None,
                        "status": minutes.status, "wall_elapsed_ms": int((perf_counter() - generation_started) * 1000)}
            if minutes.status != "DRAFT":
                return
            run = await AgentRun.create(
                run_no="AGR" + uuid4().hex.upper(), minutes_id=minutes.id, meeting_id=meeting.id,
                user_id=user.id, model_config_id=agent_config.id, status="RUNNING", max_rounds=rounds,
            )
            ids["agent_run_id"] = run.id
            reflection_started = perf_counter()
            await execute_agent_run(run.id)
            run = await AgentRun.get(id=run.id)
            final_refine = next((r for r in reversed(records) if r["call_type"] == "AGENT_REFINE" and r.get("parsed_output") is not None), None)
            revised = {"output": run.revised_json if run.revised_json is not None else baseline["output"],
                       "raw_model_output": final_refine["parsed_output"] if final_refine else baseline["raw_model_output"],
                       "revision_generated": run.revised_json is not None,
                       "status": run.status, "wall_elapsed_ms": int((perf_counter() - reflection_started) * 1000),
                       "diagnostic_model_score": run.score, "diagnostic_model_passed": run.passed,
                       "rounds": run.current_round, "model_issues": run.review_json,
                       "automatically_applied": False}
            if run.status in {"SUCCEEDED", "WAITING_CONFIRMATION"}:
                status = "completed"

        await asyncio.wait_for(pipeline(), timeout=timeout)
        # execute_agent_run intentionally catches cancellation. A wait_for timeout
        # can therefore return with the product row still RUNNING; close that row.
        if run is not None and status != "completed":
            await AgentRun.filter(id=run.id, status="RUNNING").update(
                status="FAILED", stage="Evaluation stopped", error_message="Evaluation did not complete", finished_at=now())
    except BaseException as error:
        error_type = type(error).__name__
        # 调用前的失败（前置条件、预算、schema 指纹等）**必须连消息一起记**：
        # 只记类型会让失败不可诊断——实测踩过一次，只知道 ValueError、
        # 不知道哪一条前置条件不满足，只能靠排除法猜。
        error_detail = (str(error) or "").strip()[:1000]
        failure_message = f"{error_type}: {error_detail}" if error_detail else error_type
        if minutes is not None:
            await MeetingMinutes.filter(id=minutes.id, status="GENERATING").update(
                status="FAILED", stage="Evaluation interrupted", error_message=failure_message)
        if run is not None:
            await AgentRun.filter(id=run.id, status="RUNNING").update(
                status="FAILED", stage="Evaluation interrupted", error_message=failure_message, finished_at=now())
    finally:
        model_factory.build_chat_model = original_builder

    logs = []
    if minutes is not None:
        logs += await AiCallLog.filter(biz_id=minutes.id, call_type="MINUTES").order_by("id")
    if run is not None:
        logs += await AiCallLog.filter(biz_id=run.id, call_type__in=["AGENT_REVIEW", "AGENT_REFINE"]).order_by("id")
    log_rows = [{"id": log.id, "request_id": log.request_id, "call_type": log.call_type,
                 "biz_id": log.biz_id, "status": log.status, "elapsed_ms": log.elapsed_ms,
                 "usage": log.usage_json, "model_name": log.model_name} for log in logs]
    steps = await AgentStep.filter(run_id=run.id).order_by("step_no") if run is not None else []
    result = {"case_id": request["case_id"], "synthetic": False, "asr_bypassed": True,
            "source": request["source"], "dataset_name": dataset,
            "context": {"input_char_count": len(request["transcript"]),
                        "agent_context_limit_chars": MAX_TRANSCRIPT_CHARS,
                        "agent_source_context_chars": min(MAX_TRANSCRIPT_CHARS, len(request["transcript"])),
                        "agent_context_truncated_by_product": len(request["transcript"]) > MAX_TRANSCRIPT_CHARS},
            "status": status, "error_type": error_type,
            "error_detail": locals().get("error_detail"), "ids": ids,
            "baseline": baseline, "revised": revised, "model_calls": records, "ai_call_logs": log_rows,
            "agent_steps": [{"step_no": s.step_no, "round_no": s.round_no, "step_type": s.step_type,
                             "status": s.status, "elapsed_ms": s.elapsed_ms, "payload": s.payload_json} for s in steps],
            "telemetry": {"actual_llm_dispatch_count": len(records), "ai_call_log_count": len(log_rows),
                          "input_tokens": sum(r.get("input_tokens", 0) for r in records),
                          "output_tokens": sum(r.get("output_tokens", 0) for r in records),
                          "total_tokens": sum(r.get("total_tokens", 0) for r in records),
                          "usage_missing_call_count": sum("total_tokens" not in r for r in records),
                          "llm_elapsed_ms_sum": sum(r["elapsed_ms"] for r in records),
                          "wall_elapsed_ms": int((perf_counter() - started) * 1000)},
            "quality_note": ("Model self-score is diagnostic only. Official references never enter model input; ROUGE is scored separately."
                             if dataset == "VCSum" else
                             "Model self-score is diagnostic only. Official gold never enters model input; action-item accuracy is not scored without human alignment.")}
    atomic_json(budget.directory / f"{budget.case_id}.raw-result.json", result)
    return result


async def main(request: dict) -> dict:
    from tortoise import Tortoise
    from common.auth import get_current_user
    from settings import TORTOISE_ORM
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        user = await get_current_user(token=request.get("token"))
        if request.get("action") == "preflight":
            return await preflight(request.get("expected_agent_context_limit_chars"))
        if request.get("action") == "run_case":
            return await run_case(request, user)
        raise ValueError("Unknown management action")
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    try:
        result = asyncio.run(main(json.load(sys.stdin)))
    except Exception as error:
        # Explicit validation messages contain no credentials; never print raw upstream errors.
        result = {"error": str(error) if isinstance(error, (ValueError, FileExistsError)) else type(error).__name__}
    print("AIMEETING_PUBLIC_EVAL_RESULT=" + json.dumps(result, ensure_ascii=False))
