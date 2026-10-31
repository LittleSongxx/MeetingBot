"""QA 引用支持度评测：对每条回答逐句判断 cited evidence 是否真的支持该 claim。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from models import MeetingMinutes, TranscriptSegment
from settings import TORTOISE_ORM
from tortoise import Tortoise

SYSTEM = "你是严格的证据审计员，只输出 JSON，不要输出推理过程。"
TEMPLATE = """问题：{question}
模型回答：{answer}

引用片段：
{evidence}

请把回答拆成 claims，逐条判断引用片段是否**直接支持**该 claim。不要因为“同主题/相关”就判支持；必须能从片段中找到依据。
只输出 JSON：{{"claims": [{{"claim": "...", "supported": true, "reason": "..."}}]}}
"""
API_KEY = os.getenv("LLM_API_KEY", "")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("LLM_MODEL", "deepseek-flash")


def split_claims(answer: str) -> list[str]:
    parts = re.split(r"[。！？；\n]", answer or "")
    return [part.strip() for part in parts if len(part.strip()) >= 4]


def judge(row: dict, segments: dict[int, str]) -> dict:
    evidence = "\n".join(
        f"[{no}] {segments.get(no, '')}" for no in row.get("citations") or []
    )
    prompt = TEMPLATE.format(question=row["question"], answer=row.get("answer") or "", evidence=evidence)
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 8192,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=300,
    )
    data = response.json()
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    parsed = json.loads(content) if content else {}
    claims = []
    for item in parsed.get("claims") or []:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()
        if not claim:
            continue
        claims.append({"claim": claim, "supported": bool(item.get("supported")), "reason": str(item.get("reason") or "")[:200]})
    return {"id": row["id"], "question": row["question"], "answer": row.get("answer") or "", "claims": claims}


async def load_segments() -> dict[int, str]:
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        minutes = await MeetingMinutes.get(id=1)
        segments = await TranscriptSegment.filter(task_id=minutes.source_task_id)
        return {segment.segment_no: segment.content or "" for segment in segments}
    finally:
        await Tortoise.close_connections()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qa-results", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    qa = json.loads(Path(args.qa_results).read_text(encoding="utf-8"))
    segments = asyncio.run(load_segments())
    rows = [row for row in qa["details"] if row.get("found") and row.get("citations")]
    results = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(judge, row, segments) for row in rows]
        for future in as_completed(futures):
            item = future.result()
            results.append(item)
            print(item["id"], "claims", [(claim["claim"][:24], claim["supported"]) for claim in item["claims"]])

    claims = [claim for item in results for claim in item["claims"]]
    supported = sum(1 for claim in claims if claim["supported"])
    fully = [item for item in results if item["claims"] and all(claim["supported"] for claim in item["claims"])]
    summary = {
        "answers": len(results),
        "claims": len(claims),
        "supported_claims": supported,
        "claim_support_rate": round(supported / len(claims), 4) if claims else None,
        "fully_supported_answers": len(fully),
        "fully_supported_rate": round(len(fully) / len(results), 4) if results else None,
        "unsupported_samples": [claim for claim in claims if not claim["supported"]][:5],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps({"summary": summary, "details": results}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written {args.output}")


if __name__ == "__main__":
    main()
