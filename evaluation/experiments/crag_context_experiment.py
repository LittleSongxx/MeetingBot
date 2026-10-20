"""CRAG 式检索评估器离线实验：先判断每条上下文能否回答问题，再过滤并重判检索指标。

不调用产品接口；用 qwen 做评估器、DeepSeek 做独立 judge，避免同族自评。
只验证“过滤上下文”能否提高 context precision 且不伤 recall，不在此脚本里改产品。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from lib import model_client  # noqa: E402

PACKET = EVAL / "results/opt_base_packet.json"
QA = EVAL / "results/qa_cross_30_final.json"
OUT = EVAL / "results/crag_context_experiment.json"

EVALUATOR_SYSTEM = """你是检索质量评估器。给你一个问题、若干条候选上下文，逐条判断：
这条上下文是否**可能包含**回答问题所需的信息（可以是部分信息、线索或原文片段）。
保守原则：只要可能包含所需信息就保留；只有完全无关、纯背景/纯寒暄才删除。
不要为了简洁而删掉可能有用但表述较长的上下文。
只依据上下文，不要用外部知识补全。输出 JSON：
{"keep": [1, 2], "reasons": [{"index": 1, "useful": true, "why": "一句话"}]}
keep 只放至少可能包含所需信息的上下文编号；至少保留 1 条。"""


def _load_env() -> None:
    for line in (EVAL.parent / "meetingbot/fastapi-app/.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    os.environ.setdefault("QWEN_BASE_URL", os.environ.get("LLM_BASE_URL", ""))
    os.environ.setdefault("QWEN_MODEL", os.environ.get("LLM_MODEL", ""))


def _judge_module():
    spec = importlib.util.spec_from_file_location("rag_judge", EVAL / "judges/rag.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contexts(case):
    return [(int(ctx.get("ref") or i + 1), str(ctx.get("text") or ""))
            for i, ctx in enumerate(case.get("contexts") or [])]


def _judge_contexts(judge, question, facts, contexts):
    url, model, key = model_client.endpoint("deepseek")
    listing = "\n".join(f"[{ref}] {text[:1500]}" for ref, text in contexts)
    facts_listing = "\n".join(f"{i}. {fact}" for i, fact in enumerate(facts, 1))
    prompt = (f"【问题】\n{question}\n\n【标准事实】\n{facts_listing}\n\n"
              f"【检索到的上下文】\n{listing or '（没有检索到上下文）'}\n\n请按格式输出 JSON。")
    parsed = model_client.call_json(url, model, key, judge.CONTEXT_SYSTEM, prompt,
                                    timeout=120, total_deadline=240, thinking=False)
    parsed = parsed.get("parsed") if isinstance(parsed.get("parsed"), dict) else parsed
    useful = [item for item in parsed.get("contexts") or [] if item.get("useful")]
    found = [item for item in parsed.get("facts") or [] if item.get("found")]
    return useful, found


def _evaluate(question, facts, contexts):
    url, model, key = model_client.endpoint("qwen")
    listing = "\n".join(f"[{ref}] {text[:1200]}" for ref, text in contexts)
    prompt = (f"【问题】\n{question}\n\n【候选上下文】\n{listing}\n\n"
              "请逐条判断并输出 JSON。")
    parsed = model_client.call_json(url, model, key, EVALUATOR_SYSTEM, prompt,
                                    timeout=120, total_deadline=240, thinking=False)
    parsed = parsed.get("parsed") if isinstance(parsed.get("parsed"), dict) else parsed
    keep = {int(item) for item in parsed.get("keep") or []}
    kept = [(ref, text) for ref, text in contexts if ref in keep]
    return kept or contexts[:1], parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, default=PACKET)
    parser.add_argument("--qa", type=Path, default=QA)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    _load_env()
    judge = _judge_module()
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    qa = json.loads(args.qa.read_text(encoding="utf-8"))
    facts_by_id = {case["id"]: case["expected_facts"] for case in qa["cases"]}
    rows = []
    for index, case in enumerate(packet["cases"], 1):
        facts = facts_by_id.get(case["case_id"]) or case.get("expected_facts") or []
        contexts = _contexts(case)
        kept, evaluator = _evaluate(case["question"], facts, contexts)
        useful, found = _judge_contexts(judge, case["question"], facts, contexts)
        useful_kept, found_kept = _judge_contexts(judge, case["question"], facts, kept)
        rows.append({
            "case_id": case["case_id"],
            "baseline": {"contexts": len(contexts), "useful": len(useful),
                         "facts_found": len(found), "facts_total": len(facts)},
            "filtered": {"contexts": len(kept), "useful": len(useful_kept),
                         "facts_found": len(found_kept), "facts_total": len(facts)},
            "evaluator_keep": [ref for ref, _ in kept],
            "evaluator_reasons": evaluator.get("reasons") or [],
        })
        print(f"[{index}/{len(packet['cases'])}] {case['case_id']} "
              f"{len(contexts)}→{len(kept)} contexts", flush=True)
    def summarize(key):
        contexts = sum(row[key]["contexts"] for row in rows)
        useful = sum(row[key]["useful"] for row in rows)
        found = sum(row[key]["facts_found"] for row in rows)
        total = sum(row[key]["facts_total"] for row in rows)
        return {"contexts": contexts, "precision": useful / contexts if contexts else None,
                "facts_found": found, "facts_total": total,
                "recall": found / total if total else None}
    report = {"rows": rows, "summary": {"baseline": summarize("baseline"),
                                        "filtered": summarize("filtered")}}
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
