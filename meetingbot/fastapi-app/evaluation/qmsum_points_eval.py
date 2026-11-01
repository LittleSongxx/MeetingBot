"""QMSum 完整 QA 评测：答案要点覆盖 + 引用证据，而不是只看字符相似度。"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.path.insert(0, "/app")
from evaluation.adapters.qmsum import load_qmsum
from evaluation.metrics import normalize_text
from evaluation.public_metrics import span_prf

CASES = load_qmsum(Path("/tmp/qmsum"))
QUERIES = []
for case in CASES:
    for query in case["gold"]["queries"]:
        if query["kind"] == "specific" and query.get("evidence_segments"):
            QUERIES.append((case, query))
print("queries", len(QUERIES))

SYSTEM = "你是会议纪要问答助手，只输出 JSON，不要输出推理过程。"
ANSWER_TEMPLATE = """下面是一场会议的转写，每行格式为 [句子ID] 说话人：内容。
{transcript}

问题：{question}

请用最多 8 条要点回答，每条单独一行并以“- ”开头，尽量覆盖材料中与该问题相关的所有方面；找不到依据就给空 answer。回答后给最多 10 条直接支持答案的引用，quote 必须逐字来自对应片段。
只输出 JSON：{{"answer": "...", "citations": [{{"segment_no": 1, "quote": "..."}}]}}
"""
JUDGE_SYSTEM = "你是严格的答案质量评审员，只输出 JSON。"
JUDGE_TEMPLATE = """参考回答：
{gold}

模型回答：
{answer}

请评估模型回答覆盖参考回答要点的比例（0 到 1），只考虑事实要点，不要求措辞一致；忽略参考中重复或次要表述。
只输出 JSON：{{"coverage": 0.0, "missing": ["..."]}}
"""
API_KEY = os.getenv("LLM_API_KEY", "")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("LLM_MODEL", "deepseek-flash")
MAX_POINTS = int(os.getenv("QMSUM_MAX_POINTS", "8"))


def post(payload, timeout=900):
    response = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    return response.json()


def answer_query(case, query):
    transcript = "\n".join(
        f"[{segment['segment_no']}] {segment['speaker']}: {segment['content']}" for segment in case["transcript"]
    )
    by_no = {segment["segment_no"]: segment["content"] for segment in case["transcript"]}
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": ANSWER_TEMPLATE.replace("最多 8 条要点", f"最多 {MAX_POINTS} 条要点").format(transcript=transcript, question=query["query"])},
        ],
        "temperature": 0,
        "max_tokens": 8192,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }
    last_error = None
    for _ in range(2):
        try:
            data = post(payload)
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            parsed = json.loads(content) if content else {}
            answer = str(parsed.get("answer") or "").strip()
            cited = []
            valid_quotes = 0
            for item in parsed.get("citations") or []:
                try:
                    segment_no = int(item.get("segment_no"))
                except (TypeError, ValueError):
                    continue
                quote = str(item.get("quote") or "").strip()
                source = by_no.get(segment_no)
                if source is None or not quote:
                    continue
                cited.append(segment_no)
                if normalize_text(quote) in normalize_text(source):
                    valid_quotes += 1
            return {"query": query["query"], "answer": answer, "cited": cited, "valid_quotes": valid_quotes, "gold": sorted(query["evidence_segments"])}
        except Exception as error:
            last_error = f"{type(error).__name__}: {str(error)[:200]}"
    return {"query": query["query"], "error": last_error}


def judge_coverage(item, gold_answer):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": JUDGE_TEMPLATE.format(gold=gold_answer, answer=item["answer"])},
        ],
        "temperature": 0,
        "max_tokens": 1024,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }
    data = post(payload, timeout=300)
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    parsed = json.loads(content) if content else {}
    try:
        coverage = max(0.0, min(1.0, float(parsed.get("coverage"))))
    except (TypeError, ValueError):
        coverage = 0.0
    return {"coverage": coverage, "missing": [str(x)[:120] for x in parsed.get("missing") or []]}


items = []
with ThreadPoolExecutor(max_workers=4) as executor:
    futures = [executor.submit(answer_query, case, query) for case, query in QUERIES]
    for future in as_completed(futures):
        item = future.result()
        items.append(item)
        print(item.get("query", "")[:40], "answer", bool(item.get("answer")), "cites", len(item.get("cited") or []), "error", item.get("error"))

# 按原顺序补回 gold 答案，方便 judge
gold_by_query = {}
for case, query in QUERIES:
    gold_by_query[(case["id"], query["query"])] = query["answer"]
items_by_query = {}
for item in items:
    if "error" not in item:
        items_by_query.setdefault(item["query"], item)
for item in items:
    if "error" in item:
        continue
    # gold answer 用 query 匹配；同 query 可能重复，取第一个对应
    for (_, query_text), gold_answer in gold_by_query.items():
        if query_text == item["query"]:
            try:
                item.update(judge_coverage(item, gold_answer))
            except Exception as error:
                item["coverage"] = 0.0
                item["judge_error"] = str(error)[:200]
            break

rows = [item for item in items if "error" not in item]
summary = {
    "queries": len(rows),
    "answered_rate": round(sum(1 for row in rows if row["answer"]) / len(rows), 4) if rows else 0,
    "mean_keypoint_coverage": round(sum(row.get("coverage", 0.0) for row in rows) / len(rows), 4) if rows else 0,
    "mean_evidence_f1": round(sum(span_prf(set(row["gold"]), set(row["cited"]))["f1"] for row in rows) / len(rows), 4) if rows else 0,
    "valid_quote_rate": round(sum(row["valid_quotes"] for row in rows) / max(1, sum(len(row["cited"]) for row in rows)), 4),
}
print("SUMMARY", json.dumps(summary, ensure_ascii=False))
Path("/tmp/qmsum_keypoint.json").write_text(json.dumps({"summary": summary, "details": items}, ensure_ascii=False, indent=2), encoding="utf-8")
print("written /tmp/qmsum_keypoint.json")
