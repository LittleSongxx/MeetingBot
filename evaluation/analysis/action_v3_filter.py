"""S3：对 v3 抽取结果做零调用质检过滤后的 F1（不调模型）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
REPO = EVAL.parent
sys.path.insert(0, str(REPO / "meetingbot/fastapi-app"))

from common import textsim  # noqa: E402
from services import action_validate  # noqa: E402

GOLD = EVAL / "gold/vcsum_action_items_adjudicated.json"
EXTRACT = EVAL / "results/action_extract_v3.json"
OUT = EVAL / "results/action_item_f1_v3_filtered.json"


def norm(text) -> str:
    return textsim.normalize(str(text or ""))


def match(left, right):
    candidates = []
    for li, litem in enumerate(left):
        for ri, ritem in enumerate(right):
            a, b = norm(litem.get("content")), norm(ritem.get("content"))
            if textsim.is_same_item(a, b):
                ratio, _ = textsim.similarity(a, b)
                candidates.append((ratio, li, ri))
    candidates.sort(reverse=True)
    used_l, used_r, pairs = set(), set(), []
    for _, li, ri in candidates:
        if li in used_l or ri in used_r:
            continue
        used_l.add(li)
        used_r.add(ri)
        pairs.append((li, ri))
    return pairs


def main() -> int:
    gold = {c["case_id"]: c.get("items") or []
            for c in json.loads(GOLD.read_text(encoding="utf-8"))["cases"]}
    extracted = {c["case_id"]: c.get("items") or []
                 for c in json.loads(EXTRACT.read_text(encoding="utf-8"))["cases"]}
    manifest = json.loads((EVAL / "benchmarks/manifest_test_chunk6000.json")
                          .read_text(encoding="utf-8"))
    transcripts = {c["id"]: c.get("transcript") or "" for c in manifest["cases"]}
    kept_counts = {}
    filtered = {}
    for case_id, items in extracted.items():
        text = transcripts.get(case_id, "")
        kept = []
        for item in items:
            content = str(item.get("content") or "").strip()
            if len(norm(content)) < 8:
                continue
            typed = {
                "content": content,
                "owner": {"text": item.get("owner")} if item.get("owner") else None,
                "deadline": {"text": item.get("deadline")} if item.get("deadline") else None,
                "evidence": [{"quote": item.get("evidence_quote")}]
                            if item.get("evidence_quote") else [],
            }
            check = action_validate.validate_item(typed, text)
            if check["evidence_status"] != "located":
                continue
            if any(textsim.is_same_item(norm(content), norm(prev.get("content")))
                   for prev in kept):
                continue
            kept.append(item)
        filtered[case_id] = kept
        kept_counts[case_id] = (len(items), len(kept))
    total_gold = total_pred = matched = 0
    for case_id, gold_items in gold.items():
        pred = filtered.get(case_id) or []
        pairs = match(gold_items, pred)
        total_gold += len(gold_items)
        total_pred += len(pred)
        matched += len(pairs)
    precision = matched / total_pred if total_pred else None
    recall = matched / total_gold if total_gold else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall and precision + recall > 0 else None)
    result = {"gold": total_gold, "pred": total_pred, "matched": matched,
              "precision": precision, "recall": recall, "f1": f1,
              "kept_counts": kept_counts,
              "filter": "evidence 必须能在转写定位（LCS≥10）+ content≥8字 + 去重",
              "pass": bool(f1 and f1 >= 0.70)}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in
                      ("gold", "pred", "matched", "precision", "recall", "f1", "pass")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
