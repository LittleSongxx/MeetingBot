"""S3：仲裁金标 + 证据优先抽取（10 场，跨族 AI，非人工）。"""
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
LOOSE = EVAL / "gold/vcsum_action_items_ai.json"
V2 = EVAL / "gold/vcsum_action_items_ai_v2.json"
GOLD_OUT = EVAL / "gold/vcsum_action_items_adjudicated.json"
EXTRACT_OUT = EVAL / "results/action_extract_v3.json"
RUN = EVAL / "benchmarks/results/vcsum-test26-preservation-20261005"

ADJUDICATE = """你是会议行动项仲裁员。输入：一场会议转写 + 两位标注员的候选行动项。
请给出最终清单：
- 只保留“未来需要执行的具体事项”（承诺/分配/明确的后续安排）；
- 排除：会议流程（合影、下一场）、纯观点/建议、过去事实、没有动作的泛泛目标；
- 合并重复项；content 用一句话动宾结构；
- owner/deadline 只在原文明确出现时填，否则 null；
- evidence_quote 必须是转写原文逐字摘录（≤120 字）。
只输出 JSON：{"items":[{"content","owner","deadline","evidence_quote"}]}。"""

EXTRACT = """你是会议行动项抽取器。请在内部先扫描转写中所有“未来要执行/承诺/后续安排”的句子，
再整理成行动项：
- 每条必须有 evidence_quote（逐字来自转写，≤120 字）；
- owner/deadline 只在原文明确时填，否则 null；
- 排除会议流程（合影/下一场）、纯观点/建议、过去事实、无动作的泛泛目标；
- content 一句话动宾结构。
只输出 JSON：{"items":[{"content","owner","deadline","evidence_quote"}]}。"""


def call(provider: str, system: str, user: str) -> list[dict]:
    url, model, key = model_client.endpoint(provider)
    parsed = model_client.call_json(url, model, key, system, user,
                                    timeout=240, total_deadline=360,
                                    max_tokens=4000, thinking=False)
    payload = parsed.get("parsed") or {}
    items = payload.get("items") if isinstance(payload, dict) else []
    return items if isinstance(items, list) else []


def norm(text: object) -> str:
    return textsim.normalize(str(text or ""))


def match(left: list[dict], right: list[dict]) -> list[tuple[int, int]]:
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


def product_items(case_id: str) -> list[dict]:
    path = RUN / f"{case_id}.json"
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [item for item in ((((payload.get("revised") or {}).get("output") or {})
                               .get("pending_items")) or []) if isinstance(item, dict)]


def main() -> int:
    manifest = {case["id"]: case for case in
                json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]}
    loose = {case["case_id"]: case.get("items") or []
             for case in json.loads(LOOSE.read_text(encoding="utf-8"))["cases"]}
    qwen = {case["case_id"]: case.get("qwen") or []
            for case in json.loads(V2.read_text(encoding="utf-8"))["cases"]
            if case.get("qwen") is not None}
    gold_cases, extract_cases = [], []
    for case_id in sorted(set(loose) & set(manifest)):
        transcript = manifest[case_id]["transcript"]
        candidates = {
            "标注员A（deepseek 宽松）": loose.get(case_id) or [],
            "标注员B（qwen 严格）": qwen.get(case_id) or [],
        }
        try:
            gold_items = call("deepseek", ADJUDICATE,
                              f"会议 {case_id} 转写：\n\n{transcript}\n\n候选："
                              + json.dumps(candidates, ensure_ascii=False))
        except model_client.ModelClientError as error:
            gold_cases.append({"case_id": case_id, "error": str(error)[:200]})
            continue
        try:
            extracted = call("qwen", EXTRACT, f"会议 {case_id} 转写：\n\n{transcript}")
        except model_client.ModelClientError:
            extracted = []
        gold_cases.append({"case_id": case_id, "items": gold_items})
        extract_cases.append({"case_id": case_id, "items": extracted})
        print(case_id, "gold", len(gold_items), "extract", len(extracted))

    GOLD_OUT.write_text(json.dumps({"label_source": "ai_adjudicated",
                                    "human_checked": False,
                                    "rubric": ADJUDICATE,
                                    "cases": gold_cases}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    EXTRACT_OUT.write_text(json.dumps({"method": "qwen evidence-first rubric",
                                       "rubric": EXTRACT, "cases": extract_cases},
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def evaluate(predictions: dict[str, list[dict]]) -> dict:
        total_gold = total_pred = matched = 0
        for case in gold_cases:
            case_id = case["case_id"]
            gold_items = case.get("items") or []
            pred = predictions.get(case_id) or []
            pairs = match(gold_items, pred)
            total_gold += len(gold_items)
            total_pred += len(pred)
            matched += len(pairs)
        precision = matched / total_pred if total_pred else None
        recall = matched / total_gold if total_gold else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision and recall and precision + recall > 0 else None)
        return {"gold": total_gold, "pred": total_pred, "matched": matched,
                "precision": precision, "recall": recall, "f1": f1}

    product = {case_id: product_items(case_id) for case_id in loose if case_id in manifest}
    result = {
        "instrument": "跨族仲裁 gold（deepseek 仲裁，输入 deepseek 宽松 + qwen 严格候选）",
        "human_checked": False,
        "product": evaluate(product),
        "extractor_v3": evaluate({case["case_id"]: case.get("items") or []
                                  for case in extract_cases}),
        "target_f1": 0.70,
        "limitation": "AI 仲裁不是人工金标；只能作方向信号，不能外推",
    }
    result["product"]["pass"] = bool(result["product"]["f1"] and result["product"]["f1"] >= 0.70)
    result["extractor_v3"]["pass"] = bool(result["extractor_v3"]["f1"]
                                         and result["extractor_v3"]["f1"] >= 0.70)
    out = EVAL / "results/action_item_f1_v3.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
