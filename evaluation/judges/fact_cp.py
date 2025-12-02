#!/usr/bin/env python3
"""Custom fact-aligned context precision (label: r1-fact; NOT official CP aggregation).

For each question, split the reference into expected_facts (single facts) and run the
official RAGAS ContextPrecision per fact, then average across facts per case. This
avoids the official CP's implicit "one context must support the whole multi-fact
reference" requirement, while using the same judge/model/prompt per fact.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from openai import AsyncOpenAI
from ragas.llms import llm_factory
from ragas.metrics.collections import ContextPrecision

EVAL = Path(__file__).resolve().parents[1]
ROOT = EVAL.parent
ENV_PATH = ROOT / "meetingbot" / "fastapi-app" / ".env"


def load_env() -> None:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_llm() -> Any:
    api_key = os.environ.get("JUDGE_API_KEY") or os.environ["DEEPSEEK_API_KEY"]
    client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1",
                         timeout=120, max_retries=2)
    return llm_factory("deepseek-flash", provider="openai", client=client, temperature=0,
                       max_tokens=8192, extra_body={"thinking": {"type": "disabled"}})


async def score_case(metric: ContextPrecision, case: dict, facts: list[str], sem: asyncio.Semaphore) -> dict:
    ctx = [c["text"] for c in case.get("contexts") or [] if c.get("text")]
    if not ctx:
        return {"case_id": case["case_id"], "score": None, "error": "no contexts", "facts": []}
    async with sem:
        rows = []
        for fact in facts:
            best = None
            for attempt in range(3):
                try:
                    r = await metric.ascore(user_input=case["question"], reference=fact, retrieved_contexts=ctx)
                    best = float(r.value)
                    break
                except Exception as error:  # noqa: BLE001
                    if attempt == 2:
                        rows.append({"fact": fact, "score": None, "error": f"{type(error).__name__}: {str(error)[:200]}"})
                    else:
                        await asyncio.sleep(2 * (attempt + 1))
            if best is not None:
                rows.append({"fact": fact, "score": best})
        vals = [r["score"] for r in rows if r.get("score") is not None]
        return {"case_id": case["case_id"], "score": statistics.mean(vals) if vals else None,
                "n_facts": len(facts), "n_scored": len(vals), "facts": rows}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", default="results/m1d_packet.json")
    parser.add_argument("--refs", default="gold/qa_ref_answers.json")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--out", default="results/fact_cp_m1d.json")
    args = parser.parse_args()
    load_env()
    packet = json.loads((EVAL / args.packet).read_text(encoding="utf-8"))
    cases = packet["cases"][: args.limit] if args.limit else packet["cases"]
    llm = build_llm()
    metric = ContextPrecision(llm=llm)
    sem = asyncio.Semaphore(max(1, args.concurrency))
    started = time.monotonic()
    tasks = []
    for case in cases:
        facts = [str(x) for x in (case.get("expected_facts") or []) if str(x).strip()]
        if not facts:
            tasks.append(asyncio.sleep(0, result={"case_id": case["case_id"], "score": None, "error": "no facts"}))
        else:
            tasks.append(score_case(metric, case, facts, sem))
    results = []
    for i, coro in enumerate(asyncio.as_completed(tasks), start=1):
        results.append(await coro)
        print(f"[fact-cp] {i}/{len(tasks)} elapsed={time.monotonic()-started:.0f}s", flush=True)
    valid = [r["score"] for r in results if r.get("score") is not None]
    out = {"instrument": "r1-fact", "note": "official ContextPrecision per expected_fact, averaged per case; NOT official CP aggregation",
           "packet": args.packet, "n_cases": len(results), "n_scored": len(valid),
           "mean": statistics.mean(valid) if valid else None,
           "median": statistics.median(valid) if valid else None,
           "cases": results}
    out_path = EVAL / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("n_cases", "n_scored", "mean", "median")}, ensure_ascii=False))
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
