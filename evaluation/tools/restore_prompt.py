#!/usr/bin/env python3
"""Restore CHAT_ANSWER prompt from a backup JSON (product API must be up).

Usage: python evaluation/tools/restore_prompt.py --file evaluation/results/prompt_CHAT_ANSWER_p1b_20261028.json
"""
import argparse, json, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ENV = ROOT / "meetingbot" / "fastapi-app" / ".env"
env = {}
for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
BASE = "http://127.0.0.1:19091"
parser = argparse.ArgumentParser()
parser.add_argument("--file", required=True)
args = parser.parse_args()
def req(path, method="GET", payload=None, token=""):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json;charset=utf-8")
    if token: r.add_header("token", token)
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.status, resp.read().decode()
_, body = req("/login", "POST", {"username": env["APP_ADMIN_USERNAME"], "password": env["APP_ADMIN_PASSWORD"]})
token = json.loads(body)["data"]["token"]
row = json.loads(Path(args.file).read_text(encoding="utf-8"))
payload = {k: row[k] for k in ("id", "code", "name", "scene_type", "system_prompt", "user_prompt", "enabled")}
code, body = req("/promptTemplate/update", "PUT", payload, token)
print("restore", code, body[:120])
