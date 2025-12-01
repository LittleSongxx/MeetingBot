#!/usr/bin/env python3
"""RAGAS 0.4.3 official-instrument re-measurement (instrument label r1).

Reads an existing frozen packet (question/answer/contexts) + reference answers,
then scores four official RAGAS metrics offline:
  faithfulness, answer_relevancy, context_precision, context_recall.

Design rules (see evaluation/LEDGER.md §RAGAS-1):
- Only reads frozen packet + refs; never re-runs retrieval/generation.
- Never compares/subtracts with legacy self-built metrics (different instruments).
- Never imports product implementation; only talks to HTTP model endpoints.
- Writes per-case scores + reasons + errors; NaN/None is preserved (not treated as 0).
- Resumable: --out file is rewritten atomically after each completed case.

Usage (inside evaluation/):
  .venv-ragas/bin/python judges/ragas_official.py --limit 20 --run-label pilot \
      --out results/ragas_official_pilot.json --authorized-llm-calls 300
  .venv-ragas/bin/python judges/ragas_official.py --run-label run1 \
      --out results/ragas_official_run1.json --authorized-llm-calls 1200
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import re
import socket
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# WSL host resolves AAAA first but IPv6 route is a blackhole: OpenAI SDK would wait
# 60-130s per call before falling back to IPv4. Force IPv4-only getaddrinfo, same as
# evaluation/lib/model_client.py.
_ORIG_GETADDRINFO = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    infos = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    kept = [info for info in infos if info[0] == socket.AF_INET]
    return kept or infos


socket.getaddrinfo = _ipv4_getaddrinfo

EVAL_DIR = Path(__file__).resolve().parents[1]
PRODUCT_ENV = EVAL_DIR.parent / "meetingbot" / "fastapi-app" / ".env"
DEFAULT_PACKET = EVAL_DIR / "results" / "eval100_scope_fixed_packet.json"
DEFAULT_REFS = EVAL_DIR / "gold" / "qa_ref_answers.json"
DEFAULT_QA = EVAL_DIR / "results" / "qa_eval100_fresh.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE from product .env; never override already-set env; never print values."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def openai_base(base: str) -> str:
    """OpenAI SDK appends /chat/completions or /embeddings to base_url."""
    base = (base or "").rstrip("/")
    if re.search(r"/v\d+$", base):
        return base
    return base + "/v1"


def make_evidence_meta(args: argparse.Namespace) -> dict:
    load_env_file(PRODUCT_ENV)
    judge_model = args.judge_model or os.getenv("JUDGE_MODEL") or os.getenv("DEEPSEEK_MODEL") or "deepseek-flash"
    judge_base = os.getenv("JUDGE_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    emb_model = args.embedding_model or os.getenv("EMBEDDING_MODEL") or "text-embedding-v4"
    emb_base = os.getenv("LLM_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    return {
        "judge": {
            "model": judge_model,
            "base_url": judge_base,
            "temperature": 0.0,
            "provider": "openai-compatible",
        },
        "embedding": {
            "model": emb_model,
            "base_url": emb_base,
            "dim": os.getenv("EMBEDDING_DIM") or "unknown",
        },
        "prompt_language": "en-official",
        "ragas_version": importlib.metadata.version("ragas"),
        "python": sys.version.split()[0],
    }


def build_metrics(meta: dict, strictness: int):
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.embeddings.base import embedding_factory
    from ragas.metrics.collections import (
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        Faithfulness,
    )

    load_env_file(PRODUCT_ENV)
    judge_key = os.getenv("JUDGE_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    emb_key = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not judge_key:
        raise SystemExit("missing JUDGE_API_KEY / DEEPSEEK_API_KEY")
    if not emb_key:
        raise SystemExit("missing LLM_API_KEY / DASHSCOPE_API_KEY")

    judge_client = AsyncOpenAI(
        api_key=judge_key,
        base_url=openai_base(meta["judge"]["base_url"]),
        timeout=120,
        max_retries=3,
    )
    emb_client = AsyncOpenAI(
        api_key=emb_key,
        base_url=openai_base(meta["embedding"]["base_url"]),
        timeout=120,
        max_retries=3,
    )
    llm = llm_factory(meta["judge"]["model"], provider="openai", client=judge_client, temperature=0, max_tokens=8192, extra_body={"thinking": {"type": "disabled"}})
    embeddings = embedding_factory("openai", model=meta["embedding"]["model"], client=emb_client)
    return {
        "faithfulness": Faithfulness(llm=llm),
        "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings, strictness=strictness),
        "context_precision": ContextPrecision(llm=llm),
        "context_recall": ContextRecall(llm=llm),
    }


def load_inputs(packet_path: Path, refs_path: Path, qa_path: Path, limit: int):
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    refs = json.loads(refs_path.read_text(encoding="utf-8"))
    qa = json.loads(qa_path.read_text(encoding="utf-8")) if qa_path.exists() else {"cases": []}

    ref_map = {item["case_id"]: item for item in refs.get("cases", [])}
    source_map = {item["id"]: item.get("source_set") for item in qa.get("cases", [])}
    cases = []
    for case in packet.get("cases", []):
        case_id = case["case_id"]
        ref = ref_map.get(case_id)
        if ref is None or not ref.get("reference"):
            raise SystemExit(f"missing reference for case {case_id}")
        cases.append(
            {
                "case_id": case_id,
                "question": case["question"],
                "answer": case.get("answer") or "",
                "contexts": [str((ctx or {}).get("text") or "") for ctx in (case.get("contexts") or [])],
                "reference": ref["reference"],
                "source_set": source_map.get(case_id) or "unknown",
            }
        )

    if limit and limit < len(cases):
        # stratified round-robin by source_set, stable ordering
        groups: dict[str, list[dict]] = {}
        for case in cases:
            groups.setdefault(case["source_set"], []).append(case)
        picked: list[dict] = []
        order = sorted(groups)
        index = 0
        while len(picked) < limit:
            added = False
            for key in order:
                group = groups[key]
                if index < len(group) and len(picked) < limit:
                    picked.append(group[index])
                    added = True
            if not added:
                break
            index += 1
        cases = picked
    return packet, refs, cases


def estimate_calls(cases: list[dict], strictness: int) -> int:
    total = 0
    for case in cases:
        total += 2  # faithfulness
        total += strictness  # answer relevancy
        total += 1  # context recall
        total += max(1, len(case["contexts"]))  # context precision, one call per context
    return total


def safe_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


async def score_metric(name: str, metric, case: dict, semaphore: asyncio.Semaphore) -> dict:
    """Score one metric with up to 3 attempts (transient timeouts / max_tokens retries)."""
    async with semaphore:
        started = time.monotonic()
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(1, 4):
            attempts = attempt
            try:
                if name == "faithfulness":
                    result = await metric.ascore(
                        user_input=case["question"],
                        response=case["answer"],
                        retrieved_contexts=case["contexts"],
                    )
                elif name == "answer_relevancy":
                    result = await metric.ascore(user_input=case["question"], response=case["answer"])
                elif name == "context_precision":
                    result = await metric.ascore(
                        user_input=case["question"],
                        reference=case["reference"],
                        retrieved_contexts=case["contexts"],
                    )
                elif name == "context_recall":
                    result = await metric.ascore(
                        user_input=case["question"],
                        retrieved_contexts=case["contexts"],
                        reference=case["reference"],
                    )
                else:
                    raise ValueError(f"unknown metric {name}")
                return {
                    "score": safe_float(getattr(result, "value", None)),
                    "reason": str(getattr(result, "reason", "") or "")[:1500],
                    "seconds": round(time.monotonic() - started, 1),
                    "attempts": attempts,
                }
            except Exception as error:  # noqa: BLE001 - retry transient; else record and continue
                last_error = error
                if attempt < 3:
                    await asyncio.sleep(1.5 * attempt)
        return {
            "score": None,
            "reason": "",
            "seconds": round(time.monotonic() - started, 1),
            "attempts": attempts,
            "error": f"{type(last_error).__name__}: {str(last_error)[:800]}",
        }

def summarize(cases: list[dict]) -> dict:
    metric_names = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
    summary: dict[str, dict] = {}
    for name in metric_names:
        values = [c["scores"].get(name) for c in cases if c["scores"].get(name) is not None]
        summary[name] = {
            "mean": round(sum(values) / len(values), 6) if values else None,
            "n_numeric": len(values),
            "n_null": sum(1 for c in cases if c["scores"].get(name) is None),
        }
    by_source: dict[str, dict] = {}
    for case in cases:
        source = case.get("source_set") or "unknown"
        bucket = by_source.setdefault(source, {name: [] for name in metric_names})
        for name in metric_names:
            value = case["scores"].get(name)
            if value is not None:
                bucket[name].append(value)
    for source, bucket in by_source.items():
        by_source[source] = {
            name: (round(sum(values) / len(values), 6) if values else None)
            for name, values in bucket.items()
        }
        by_source[source]["n_cases"] = sum(1 for c in cases if (c.get("source_set") or "unknown") == source)
    return {"overall": summary, "by_source_set": by_source}


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


async def run(args: argparse.Namespace) -> int:
    packet_path = Path(args.packet).resolve()
    refs_path = Path(args.refs).resolve()
    out_path = Path(args.out).resolve()
    meta = make_evidence_meta(args)
    packet, refs, cases = load_inputs(packet_path, refs_path, Path(args.qa).resolve(), args.limit)
    meta.update(
        {
            "label": args.label,
            "run": args.run_label,
            "packet_path": str(packet_path),
            "packet_sha256": sha256_file(packet_path),
            "refs_path": str(refs_path),
            "refs_sha256": sha256_file(refs_path),
            "refs_human_checked": refs.get("human_checked"),
            "limit": args.limit,
            "strictness": args.strictness,
            "concurrency": args.concurrency,
            "metrics": args.metrics,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    estimated = estimate_calls(cases, args.strictness)
    meta["estimated_judge_calls"] = estimated
    print(f"[r1] cases={len(cases)} estimated_judge_calls={estimated} judge={meta['judge']['model']} embedding={meta['embedding']['model']}", flush=True)
    if estimated > args.authorized_llm_calls:
        raise SystemExit(f"estimated calls {estimated} > authorized {args.authorized_llm_calls}")
    if args.dry_run:
        print("[r1] dry-run only; no judge calls")
        return 0

    metrics = build_metrics(meta, args.strictness)
    semaphore = asyncio.Semaphore(args.concurrency)

    existing: dict[str, dict] = {}
    if out_path.exists() and args.resume:
        try:
            prior = json.loads(out_path.read_text(encoding="utf-8"))
            for case in prior.get("cases", []):
                if not case.get("errors") and all(case.get("scores", {}).get(name) is not None for name in args.metrics):
                    existing[case["case_id"]] = case
        except Exception:
            existing = {}
    results: list[dict] = [existing[c["case_id"]] for c in cases if c["case_id"] in existing]
    todo = [c for c in cases if c["case_id"] not in existing]
    print(f"[r1] resume={len(existing)} todo={len(todo)} batch_size={args.batch_size}", flush=True)
    started_at = time.monotonic()

    for batch_start in range(0, len(todo), args.batch_size):
        batch = todo[batch_start : batch_start + args.batch_size]
        pairs = [(case, name) for case in batch for name in args.metrics]
        outcomes = await asyncio.gather(
            *[score_metric(name, metrics[name], case, semaphore) for case, name in pairs]
        )
        grouped: dict[str, dict[str, dict]] = {}
        for (case, name), outcome in zip(pairs, outcomes):
            grouped.setdefault(case["case_id"], {})[name] = outcome
        for case in batch:
            outcome_map = grouped.get(case["case_id"], {})
            scores = {name: outcome_map.get(name, {}).get("score") for name in args.metrics}
            reasons = {name: outcome_map.get(name, {}).get("reason") for name in args.metrics}
            errors = {name: outcome_map[name]["error"] for name in args.metrics if outcome_map.get(name, {}).get("error")}
            results.append(
                {
                    "case_id": case["case_id"],
                    "question": case["question"],
                    "source_set": case["source_set"],
                    "n_contexts": len(case["contexts"]),
                    "scores": scores,
                    "reasons": reasons,
                    "errors": errors,
                    "timings": {
                        name: {
                            "seconds": outcome_map.get(name, {}).get("seconds"),
                            "attempts": outcome_map.get(name, {}).get("attempts"),
                        }
                        for name in args.metrics
                    },
                }
            )
        nulls = sum(1 for c in results for name in args.metrics if c["scores"].get(name) is None)
        print(
            f"[r1] {len(results)}/{len(cases)} cases done, null_metric_values={nulls}, elapsed={time.monotonic()-started_at:.0f}s",
            flush=True,
        )
        payload = dict(meta)
        payload.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "wall_seconds": round(time.monotonic() - started_at, 1),
                "n_cases_scored": len(results),
                "cases": sorted(results, key=lambda item: item["case_id"]),
                "summary": summarize(sorted(results, key=lambda item: item["case_id"])),
            }
        )
        write_json_atomic(out_path, payload)

    payload = dict(meta)
    payload.update(
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "wall_seconds": round(time.monotonic() - started_at, 1),
            "n_cases_scored": len(results),
            "cases": sorted(results, key=lambda item: item["case_id"]),
            "summary": summarize(sorted(results, key=lambda item: item["case_id"])),
        }
    )
    write_json_atomic(out_path, payload)
    print(f"[r1] wrote {out_path}", flush=True)
    print(json.dumps(payload["summary"]["overall"], ensure_ascii=False), flush=True)
    return 0

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", default=str(DEFAULT_PACKET))
    parser.add_argument("--refs", default=str(DEFAULT_REFS))
    parser.add_argument("--qa", default=str(DEFAULT_QA))
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", default="r1")
    parser.add_argument("--run-label", default="run1")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--metrics", nargs="+", default=["faithfulness", "answer_relevancy", "context_precision", "context_recall"])
    parser.add_argument("--strictness", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--authorized-llm-calls", type=int, default=1200)
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())





