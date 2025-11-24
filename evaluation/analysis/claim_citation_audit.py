"""P3 原型：逐句检查“有引用的事实断言”里的数字/日期是否真的出现在被引上下文里。

零模型调用，只做可判定部分；用于量化 claim-citation 缺口，不替代 LLM judge。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SENTENCE = re.compile(r"[。！？\n]+")
CITATION = re.compile(r"\[(\d{1,3})\]")
NUMBER = re.compile(r"\d+年\d+月(?:\d+日)?|\d+月\d+日|\d+(?:\.\d+)?(?:[:：/-]\d+)*(?:%|％)?")


def claims(answer: str) -> list[tuple[str, list[int]]]:
    rows = []
    for sentence in SENTENCE.split(answer or ""):
        sentence = sentence.strip()
        if not sentence:
            continue
        refs = [int(match.group(1)) for match in CITATION.finditer(sentence)]
        if refs:
            rows.append((sentence, refs))
    return rows


def audit_case(case: dict) -> dict:
    source_items = case.get("citations") or case.get("contexts") or []
    citation_by_ref = {int(item.get("ref")): item for item in source_items
                       if item.get("ref") is not None}
    unsupported_cited = []
    unsupported_any = []
    total_numbers = 0
    all_contexts = "\n".join(
        str(item.get(key) or "")
        for item in (case.get("contexts") or [])
        for key in ("source_text", "quote", "text"))
    for sentence, refs in claims(case.get("answer") or ""):
        texts = []
        for ref in refs:
            item = citation_by_ref.get(ref) or {}
            for key in ("source_text", "quote", "text"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    texts.append(value)
        haystack = "\n".join(texts)
        for token in dict.fromkeys(NUMBER.findall(sentence)):
            if token.isdigit() and len(token) <= 2:
                continue
            total_numbers += 1
            if token not in haystack:
                unsupported_cited.append({"token": token, "refs": refs,
                                          "sentence": sentence[:180]})
                if token not in all_contexts:
                    unsupported_any.append({"token": token, "refs": refs,
                                            "sentence": sentence[:180]})
    return {"case_id": case.get("case_id"),
            "claims_with_citations": len(claims(case.get("answer") or "")),
            "numbers_checked": total_numbers,
            "unsupported_cited": unsupported_cited,
            "unsupported_any": unsupported_any}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    rows = [audit_case(case) for case in packet.get("cases") or []]
    total = sum(row["numbers_checked"] for row in rows)
    bad_cited = sum(len(row["unsupported_cited"]) for row in rows)
    bad_any = sum(len(row["unsupported_any"]) for row in rows)
    result = {"packet": str(args.packet), "cases": len(rows), "numbers_checked": total,
              "unsupported_cited_numbers": bad_cited,
              "unsupported_any_numbers": bad_any,
              "cited_gap_rate": (bad_cited / total) if total else None,
              "not_in_any_context_rate": (bad_any / total) if total else None,
              "rows": rows}
    print(json.dumps({k: result[k] for k in ("packet", "cases", "numbers_checked",
                                              "unsupported_cited_numbers",
                                              "unsupported_any_numbers",
                                              "cited_gap_rate",
                                              "not_in_any_context_rate")},
                     ensure_ascii=False, indent=2))
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
