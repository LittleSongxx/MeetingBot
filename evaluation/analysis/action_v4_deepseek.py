"""S3 备选：deepseek 证据优先抽取 vs 仲裁 gold（10 场，同族需注明）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
REPO = EVAL.parent
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(REPO / "meetingbot/fastapi-app"))

from common import textsim  # noqa: E402
from lib import model_client  # noqa: E402

MANIFEST = EVAL / "benchmarks/manifest_test_chunk6000.json"
GOLD = EVAL / "gold/vcsum_action_items_adjudicated.json"
OUT = EVAL / "results/action_extract_v4_deepseek.json"
EXTRACT = """你是会议行动项抽取器。请在内部先扫描转写中所有“未来要执行/承诺/后续安排”的句子，
再整理成行动项：
- 每条必须有 evidence_quote（逐字来自转写，≤120 字）；
- owner/deadline 只在原文明确时填，否则 null；
- 排除会议流程（合影/下一场）、纯观点/建议、过去事实、无动作的泛泛目标；
- content 一句话动宾结构。
只输出 JSON：{"items":[{"content","owner","deadline","evidence_quote"}]}。"""


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
    manifest = {case["id"]: case for case in
                json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]}
    gold = {case["case_id"]: case.get("items") or []
            for case in json.loads(GOLD.read_text(encoding="utf-8"))["cases"]}
    url, model, key = model_client.endpoint("deepseek")
    extracted = {}
    for case_id in sorted(gold):
        try:
            parsed = model_client.call_json(
                url, model, key, EXTRACT,
                f"会议 {case_id} 转写：\n\n{manifest[case_id]['transcript']}",
                timeout=240, total_deadline=360, max_tokens=4000, thinking=False)
        except model_client.ModelClientError as error:
            print("FAIL", case_id, error)
            extracted[case_id] = []
            continue
        payload = parsed.get("parsed") or {}
        items = payload.get("items") if isinstance(payload, dict) else []
        extracted[case_id] = items if isinstance(items, list) else []
        print(case_id, len(extracted[case_id]))
    total_gold = total_pred = matched = 0
    for case_id, gold_items in gold.items():
        pairs = match(gold_items, extracted.get(case_id) or [])
        total_gold += len(gold_items)
        total_pred += len(extracted.get(case_id) or [])
        matched += len(pairs)
    precision = matched / total_pred if total_pred else None
    recall = matched / total_gold if total_gold else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall and precision + recall > 0 else None)
    result = {"method": "deepseek 证据优先抽取 vs 仲裁 gold（同族，gold 也由 deepseek 仲裁）",
              "gold": total_gold, "pred": total_pred, "matched": matched,
              "precision": precision, "recall": recall, "f1": f1,
              "target_f1": 0.70, "pass": bool(f1 and f1 >= 0.70),
              "limitation": "同族评测会高估；只能作上限参考，不能当独立验证",
              "items": extracted}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in
                      ("gold", "pred", "matched", "precision", "recall", "f1", "pass")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
