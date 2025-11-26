"""P0 多轮抓取：**每个问题新建 session**，避免长历史把单题延迟越拖越高。

只走产品 HTTP；产物形状与 judges/rag.py --ask 一致，可直接给 --build/--judge 用。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from judges.rag import _ask, _http_json, _now, _product_switches, _refs_from_answer, _unknown_refs  # noqa: E402


def _env() -> dict[str, str]:
    values = {}
    for line in (EVAL.parent / "meetingbot/fastapi-app/.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qa-set", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:19091")
    parser.add_argument("--meeting-id", type=int, default=None)
    args = parser.parse_args()
    env = _env()
    login = _http_json(f"{args.base_url}/login", method="POST", token="",
                       payload={"username": env["APP_ADMIN_USERNAME"],
                                "password": env["APP_ADMIN_PASSWORD"]})
    token = (login.get("data") or {}).get("token") or login.get("token")
    switches = _product_switches(args.base_url, token)
    qa = json.loads(args.qa_set.read_text(encoding="utf-8"))
    turns = []
    for case in qa["cases"]:
        scope = case.get("meeting_ids") or ([args.meeting_id] if args.meeting_id else None)
        series_scope = case.get("series_ids")
        created = _http_json(f"{args.base_url}/chat/session",
                             payload={"meeting_ids": scope, "series_ids": series_scope},
                             token=token)
        session_id = (created.get("data") or {}).get("id")
        if not session_id:
            raise RuntimeError(f"建会话失败：{created.get('msg') or created}")
        message = _ask(args.base_url, token, session_id, case["question"])
        turns.append({
            "case_id": case["id"], "meeting_id": scope[0] if scope else None,
            "question": case["question"],
            "answer": message.get("content") or "", "message_id": message.get("id"),
            "stop_reason": message.get("stop_reason"),
            "citations": message.get("citations") or [],
            "tools": message.get("tools") or [],
            "allowed_meeting_ids": scope,
            "series_ids": series_scope,
            "used_refs": _refs_from_answer(message),
            "unknown_refs": _unknown_refs(message),
            "retrieved": message.get("retrieved") or [],
            "session_id": session_id,
        })
        print(f"[{case['id']}] {case['question'][:34]}…", flush=True)
        time.sleep(1.2)
    args.answers.parent.mkdir(parents=True, exist_ok=True)
    args.answers.write_text(json.dumps({
        "artifact_version": 1,
        "captured_from": {"base_url": args.base_url, "mode": "fresh_session_per_case",
                          "captured_at": _now(), "switches": switches},
        "turns": turns,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.answers}: {len(turns)} turns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
