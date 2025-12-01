#!/usr/bin/env python3
"""Per-case-scope QA capture for prompt A/B (M1).

Creates one session per case using the case's meeting_ids/series_ids, asks the
question through the product API, reads back the persisted assistant message
(answer + retrieved contexts), and writes:
  - answers artifact (resumable)
  - a packet compatible with judges/ragas_official.py

Usage:
  .venv-ragas/bin/python judges/rag_capture_case.py --limit 10 --out-prefix results/m1_pilot --workers 3 --authorized-llm-calls 400
  .venv-ragas/bin/python judges/rag_capture_case.py --out-prefix results/m1 --workers 3 --authorized-llm-calls 4000
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

EVAL = Path(__file__).resolve().parents[1]
ROOT = EVAL.parent
ENV_PATH = ROOT / "meetingbot" / "fastapi-app" / ".env"
QA_SET = EVAL / "results" / "qa_eval100_fresh.json"
BASE_URL = "http://127.0.0.1:19091"

_lock = threading.Lock()


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_PATH.exists():
        return env
    for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def http_json(url: str, *, method: str = "GET", payload: dict | None = None,
              token: str = "", timeout: float = 300.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json;charset=utf-8")
    if token:
        req.add_header("token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"{method} {url} HTTP {error.code}: {detail}") from error


def login(env: dict[str, str]) -> str:
    result = http_json(BASE_URL + "/login", method="POST",
                       payload={"username": env.get("APP_ADMIN_USERNAME", ""),
                                "password": env.get("APP_ADMIN_PASSWORD", "")})
    return result["data"]["token"]


def ask_case(case: dict, token: str) -> dict:
    session = http_json(BASE_URL + "/chat/session", method="POST", token=token,
                        payload={"meeting_ids": case.get("meeting_ids"),
                                 "series_ids": case.get("series_ids")})
    session_id = session["data"]["id"]
    body = json.dumps({"session_id": session_id, "question": case["question"]}).encode("utf-8")

    def _stream_once() -> None:
        req = urllib.request.Request(BASE_URL + "/chat/stream", data=body, method="POST")
        req.add_header("Content-Type", "application/json;charset=utf-8")
        req.add_header("token", token)
        with urllib.request.urlopen(req, timeout=300) as resp:
            resp.read()

    for attempt in range(1, 8):
        try:
            _stream_once()
            break
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 7:
                wait = 10
                try:
                    wait = int(error.headers.get("Retry-After") or 10)
                except Exception:
                    pass
                time.sleep(max(5, wait))
                continue
            raise
    history = http_json(f"{BASE_URL}/chat/messages?session_id={session_id}&limit=50", token=token)
    rows = (history.get("data") or {}).get("list") or []
    if not rows or rows[-1].get("role") != "ASSISTANT":
        raise RuntimeError("no assistant message after ask")
    message = rows[-1]
    contexts = []
    for item in (message.get("retrieved") or []):
        text = str(item.get("source_text") or item.get("quote") or "").strip()
        if not text:
            continue
        contexts.append({
            "ref": item.get("ref"),
            "kind": item.get("kind"),
            "meeting_id": item.get("meeting_id"),
            "text": text,
        })
    return {
        "case_id": case["id"],
        "question": case["question"],
        "expected_facts": case.get("expected_facts") or [],
        "unanswerable": bool(case.get("unanswerable")),
        "answer": str(message.get("content") or ""),
        "contexts": contexts,
        "context_source": "retrieved" if contexts else "none",
        "has_answer": bool(str(message.get("content") or "").strip()),
        "stop_reason": message.get("stop_reason"),
        "tools": message.get("tools") or [],
    }


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    global BASE_URL
    parser = argparse.ArgumentParser()
    parser.add_argument("--qa-set", type=Path, default=QA_SET)
    parser.add_argument("--out-prefix", default="results/m1")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--authorized-llm-calls", type=int, default=0)
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()
    BASE_URL = args.base_url
    if not args.authorized_llm_calls:
        raise SystemExit("--ask triggers real LLM calls; need --authorized-llm-calls")
    env = load_env()
    token = login(env)
    qa = json.loads(args.qa_set.read_text(encoding="utf-8"))
    cases = qa["cases"][: args.limit] if args.limit else qa["cases"]
    out_prefix = Path(args.out_prefix)
    answers_path = out_prefix.with_name(out_prefix.name + "_answers.json")
    packet_path = out_prefix.with_name(out_prefix.name + "_packet.json")

    done: dict[str, dict] = {}
    if answers_path.exists():
        try:
            prior = json.loads(answers_path.read_text(encoding="utf-8"))
            done = {c["case_id"]: c for c in prior.get("cases", [])}
        except Exception:
            done = {}
    todo = [c for c in cases if c["id"] not in done]
    print(f"[capture] cases={len(cases)} done={len(done)} todo={len(todo)} workers={args.workers}", flush=True)
    failures: list[dict] = []
    started = time.monotonic()

    def run(case: dict) -> tuple[dict | None, dict | None]:
        try:
            return ask_case(case, token), None
        except Exception as error:  # noqa: BLE001 - record and continue
            return None, {"case_id": case["id"], "error": f"{type(error).__name__}: {str(error)[:400]}"}

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run, case): case for case in todo}
        for index, future in enumerate(as_completed(futures), start=1):
            case = futures[future]
            record, failure = future.result()
            if record:
                done[record["case_id"]] = record
            if failure:
                failures.append(failure)
            with _lock:
                write_json_atomic(answers_path, {"artifact_version": 1, "cases": sorted(done.values(), key=lambda x: x["case_id"])})
            print(f"[capture] {index}/{len(todo)} {case['id']} {'ERR' if failure else 'ok'} elapsed={time.monotonic()-started:.0f}s", flush=True)

    packet_cases = []
    for case in cases:
        record = done.get(case["id"])
        if record is None:
            continue
        packet_cases.append(record)
    packet = {
        "packet_version": 1,
        "purpose": "M1 answer-grounding prompt A/B",
        "qa_set": "mixed100",
        "case_count": len(cases),
        "answered_case_count": sum(1 for c in packet_cases if c.get("has_answer")),
        "cases": packet_cases,
    }
    write_json_atomic(packet_path, packet)
    write_json_atomic(answers_path, {"artifact_version": 1, "cases": sorted(done.values(), key=lambda x: x["case_id"])})
    print(f"[capture] wrote {packet_path} ({len(packet_cases)} cases); failures={len(failures)}")
    if failures:
        print(json.dumps(failures[:5], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
