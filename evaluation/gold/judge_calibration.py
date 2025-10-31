"""AI 替代人工标注（2026-09-26）：三个独立模型族当"标注者"，跑 verification 协议。

## 定位与诚实边界

`annotation.py` 的标注包原本为众包人工设计（≥3 人、α ≥ 0.7 发表门槛）。本驱动把
**标注者换成三个互相独立的模型族**（当前仅 qwen——glm 硬配额 429、kimi key 401，已按决定移除；与产品主模型 deepseek-flash
均不同族），产出两类东西：

1. **裁判校准（轴一，validity）**：对 88 条带模型裁决标签的条目做**盲标**——
   标注者用与原裁决**逐字相同**的冻结标准提示词独立打标签，本地再与原标签比对。
   刻意不给"模型判了什么"（避免锚定），因此它同时测：
   a) 标签可复现性（三个独立族之间的 α）；b) 原裁判与独立族的逐条一致率。
2. **owner/deadline 金标（轴二）**：对契约 v2 的 72 条 decisions/pending_items，
   按 `ANNOTATION_GUIDE.md` 的 correct/wrong/missing/na/unjudgeable 核对产品给的
   负责人与期限（窗口 = 条目在转写中的定位段）。

**这仍然不是人工金标。** 三个模型族一致只说明任务定义清晰、标签可复现，
不构成对人类有效性的证据；产物里 `substitution_disclosure` 写明这一点。
所有数字的引用格式是"AI 标注（三独立族多数票）"，不得写成"人工标注"。

## 用法

    PYTHONPATH=evaluation python evaluation/metrics/ai_annotate.py validity \
        --providers qwen --authorized-llm-calls 100
    PYTHONPATH=evaluation python evaluation/metrics/ai_annotate.py owner-deadline \
        --providers qwen --authorized-llm-calls 80
    PYTHONPATH=evaluation python evaluation/metrics/ai_annotate.py report
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from judges import packets as adjudication, item_judge as ai_adjudicate  # type: ignore
    from gold import annotation as anno  # type: ignore
    from lib import corpora as run_metrics, model_client  # type: ignore
else:
    from judges import packets as adjudication, item_judge as ai_adjudicate
    from gold import annotation as anno
    from lib import corpora as run_metrics, model_client  # type: ignore

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"
VALIDITY_OUT = RESULTS_DIR / "ai_validity_labels.json"
OWNER_OUT = RESULTS_DIR / "ai_owner_deadline_labels.json"
CALIBRATION_OUT = RESULTS_DIR / "ai_annotation_calibration.json"

OWNER_DEADLINE_SYSTEM = """你是会议纪要的独立核对者。给你一条产品抽出的决策/待办（content）、
它建议的负责人与期限、以及转写中对应的原文窗口。对照窗口逐项核对。

标签（owner 与 deadline 各选一个，互斥）：
- correct：候选对——窗口里能找到这个负责人/期限；逐字或同义都算（「张经理」vs「老张」算对）。
- wrong：候选编造——窗口里没有依据，或归错了人/时间（窗口明确说了别人，或根本没说）。
- missing：该有但漏了——窗口里明确说了负责人/期限，候选却是空/「待确认」。
- na：本条内容本来就没有负责人/期限可言。
- unjudgeable：判不了（乱码、指代不清、窗口不足）。宁选这个也不要猜。

missing 与 wrong 的分界：missing = 候选为空但原文有；wrong = 候选非空但原文不支持。

只输出一个 JSON 对象，不要解释文字：
{"owner_label": "...", "deadline_label": "...", "note": "一句话中文理由（可选）"}"""

OWNER_LABELS = {"correct", "wrong", "missing", "na", "unjudgeable"}


def _providers(spec: str) -> list[str]:
    names = [name.strip() for name in spec.split(",") if name.strip()]
    for name in names:
        model_client.endpoint(name)  # 早失败：缺凭据/未知名直接报错
    return names


def _load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def validity_items() -> list[dict[str, Any]]:
    source = json.loads((RESULTS_DIR / "adjudication_packet_v2.json").read_text(encoding="utf-8"))
    return [item for item in source["items"] if item.get("label")]


def owner_deadline_items() -> list[dict[str, Any]]:
    cases = run_metrics.load_new_contract_cases()
    entries: list[dict[str, Any]] = []
    for case in cases:
        for field in ("decisions", "pending_items"):
            for item in (case["baseline_output"].get(field) or []):
                if not isinstance(item, Mapping) or not str(item.get("content") or "").strip():
                    continue
                owner = item.get("owner") or {}
                deadline = item.get("deadline") or {}
                def _slot_text(value: Any) -> str:
                    if isinstance(value, Mapping):
                        return str(value.get("text") or value.get("raw_span") or value.get("normalized") or "").strip()
                    return str(value or "").strip()
                window = adjudication._transcript_window(case["transcript"], str(item.get("content")))
                if not window:
                    window = str(case["transcript"][:1200])
                entries.append({
                    "case_id": case["case_id"], "field": field,
                    "content": str(item.get("content")),
                    "owner_candidate": _slot_text(owner),
                    "deadline_candidate": _slot_text(deadline),
                    "transcript_window": window,
                })
    return entries


def run_validity(providers: Sequence[str], authorized: int) -> None:
    items = validity_items()
    state = _load_state(VALIDITY_OUT)
    state.setdefault("labels", {})
    calls = 0
    for item in items:
        anon = item["anonymous_id"]
        user = (f"【转写窗口】\n{item['transcript_window']}\n\n"
                f"【纪要条目】\n{item['item_text']}\n\n"
                "请给出你的标签。")
        for provider in providers:
            key = f"{anon}:{provider}"
            if state["labels"].get(key):
                continue
            if calls >= authorized:
                print(f"停止：授权 {authorized} 次已用尽（完成 {calls} 次新调用）", file=sys.stderr)
                _save_state(VALIDITY_OUT, state)
                return
            url, model, api_key = model_client.endpoint(provider)
            try:
                reply = model_client.call_json(url, model, api_key,
                                               ai_adjudicate.SYSTEM_PROMPT, user)
                label, operation, reason = ai_adjudicate._parse_label(reply["parsed"])
                state["labels"][key] = {"label": label, "operation": operation,
                                        "reason": reason}
            except (model_client.ModelClientError, ValueError) as error:
                state["labels"][key] = {"error": str(error)[:200]}
            calls += 1
            time.sleep(0.15)
        _save_state(VALIDITY_OUT, state)
    state["calls_last_run"] = calls
    _save_state(VALIDITY_OUT, state)
    print(f"validity 完成：{calls} 次新调用，共 {len(state['labels'])} 条标签")


def run_owner_deadline(providers: Sequence[str], authorized: int) -> None:
    entries = owner_deadline_items()
    state = _load_state(OWNER_OUT)
    state.setdefault("labels", {})
    calls = 0
    for position, entry in enumerate(entries, start=1):
        anon = f"od_{position:03d}"
        user = (f"【转写窗口】\n{entry['transcript_window']}\n\n"
                f"【产品抽出的条目】（{entry['field']}）\n{entry['content']}\n\n"
                f"【负责人候选】{entry['owner_candidate'] or '（空/待确认）'}\n"
                f"【期限候选】{entry['deadline_candidate'] or '（空/待确认）'}\n\n"
                "请分别核对负责人与期限。")
        for provider in providers:
            key = f"{anon}:{provider}"
            if state["labels"].get(key):
                continue
            if calls >= authorized:
                print(f"停止：授权 {authorized} 次已用尽（完成 {calls} 次新调用）", file=sys.stderr)
                _save_state(OWNER_OUT, state)
                return
            url, model, api_key = model_client.endpoint(provider)
            try:
                reply = model_client.call_json(url, model, api_key,
                                               OWNER_DEADLINE_SYSTEM, user)
                parsed = reply["parsed"]
                owner_label = str(parsed.get("owner_label") or "").strip().lower()
                deadline_label = str(parsed.get("deadline_label") or "").strip().lower()
                record: dict[str, Any] = {}
                if owner_label in OWNER_LABELS:
                    record["owner_label"] = owner_label
                else:
                    record["owner_label"] = "unjudgeable"
                    record["owner_parse_note"] = f"非法标签 {owner_label!r}，降级 unjudgeable"
                if deadline_label in OWNER_LABELS:
                    record["deadline_label"] = deadline_label
                else:
                    record["deadline_label"] = "unjudgeable"
                    record["deadline_parse_note"] = f"非法标签 {deadline_label!r}，降级 unjudgeable"
                record["note"] = str(parsed.get("note") or "")[:200]
                state["labels"][key] = record
            except model_client.ModelClientError as error:
                state["labels"][key] = {"error": str(error)[:200]}
            calls += 1
            time.sleep(0.15)
        _save_state(OWNER_OUT, state)
    state["calls_last_run"] = calls
    _save_state(OWNER_OUT, state)
    print(f"owner/deadline 完成：{calls} 次新调用，共 {len(state['labels'])} 条标签")


def _majority(values: Sequence[str | None]) -> str | None:
    usable = [v for v in values if v]
    if not usable:
        return None
    top, count = Counter(usable).most_common(1)[0]
    return top if count * 2 > len(usable) else "split"


def build_report() -> dict[str, Any]:
    report: dict[str, Any] = {
        "purpose": "AI 替代人工标注（三独立族）：裁判校准 + owner/deadline 核对",
        "substitution_disclosure": (
            "标注者为 qwen 单族（glm/kimi 已移除；与产品主模型 deepseek-flash 不同族）。"
            "三族一致说明标签可复现，**不构成人类有效性证据**；引用时必须写"
            "「AI 标注（三独立族多数票）」，不得写成人工金标。"
        ),
    }
    # ---- 轴一：效度 ----
    validity_path = RESULTS_DIR / "adjudication_packet_v2.json"
    if VALIDITY_OUT.exists() and validity_path.exists():
        model_labels = {i["anonymous_id"]: i["label"] for i in validity_items()}
        state = _load_state(VALIDITY_OUT)
        providers = sorted({key.split(":")[-1] for key in state["labels"]})
        per_provider: dict[str, Any] = {}
        units: list[list[str | None]] = []
        majority_labels: dict[str, str | None] = {}
        disagreements: list[dict[str, str]] = []
        for anon, model_label in model_labels.items():
            row: list[str | None] = []
            for provider in providers:
                entry = state["labels"].get(f"{anon}:{provider}") or {}
                row.append(entry.get("label"))
            units.append(row)
            maj = _majority(row)
            majority_labels[anon] = maj
            if maj and maj != model_label:
                disagreements.append({
                    "anonymous_id": anon, "model_label": model_label,
                    "majority_label": maj,
                    "per_provider": dict(zip(providers, row)),
                })
        for provider in providers:
            hit = total = 0
            for anon, model_label in model_labels.items():
                entry = state["labels"].get(f"{anon}:{provider}") or {}
                if entry.get("label"):
                    total += 1
                    hit += 1 if entry["label"] == model_label else 0
            per_provider[provider] = {"agreement_with_model": f"{hit}/{total}",
                                      "rate": round(hit / total, 4) if total else None}
        maj_hit = sum(1 for anon, ml in model_labels.items()
                      if majority_labels.get(anon) == ml)
        maj_total = sum(1 for anon in model_labels
                        if majority_labels.get(anon) in
                        {"supported", "derivable", "partial", "unsupported"})
        report["validity_axis"] = {
            "providers": providers,
            "alpha_among_annotators": anno.krippendorff_alpha(units),
            "pabak": anno.pabak(units),
            "per_provider_agreement_with_original_adjudication": per_provider,
            "majority_vs_original": f"{maj_hit}/{maj_total}",
            "majority_agreement_rate": round(maj_hit / maj_total, 4) if maj_total else None,
            "majority_label_distribution": dict(Counter(
                v for v in majority_labels.values() if v)),
            "n_disagreements": len(disagreements),
            "disagreements_sample": disagreements[:15],
            "protocol": "盲标：标注者用与原裁决逐字相同的冻结标准提示词独立打标签，"
                        "本地比对（避免出示模型标签造成锚定）",
        }
    # ---- 轴二：owner/deadline ----
    if OWNER_OUT.exists():
        state = _load_state(OWNER_OUT)
        providers = sorted({key.split(":")[-1] for key in state["labels"]})
        per_field: dict[str, Any] = {}
        for field in ("owner_label", "deadline_label"):
            units = []
            anons = sorted({key.split(":")[0] for key in state["labels"]})
            for anon in anons:
                row = [(state["labels"].get(f"{anon}:{p}") or {}).get(field)
                       for p in providers]
                units.append(row)
            dist = Counter()
            for row in units:
                maj = _majority(row)
                if maj:
                    dist[maj] += 1
            per_field[field] = {
                "alpha_among_annotators": anno.krippendorff_alpha(units),
                "pabak": anno.pabak(units),
                "majority_distribution": dict(dist),
                "n_items": len(units),
            }
        report["owner_deadline_axis"] = {
            "providers": providers,
            "per_field": per_field,
            "corpus": "契约 v2 的 26 场 decisions/pending_items 全量（72 条）",
        }
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("validity", "owner-deadline", "report"))
    parser.add_argument("--providers", default="qwen")
    parser.add_argument("--authorized-llm-calls", type=int, default=0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.mode == "validity":
        if args.authorized_llm_calls < 1:
            print("拒绝执行：--authorized-llm-calls 必须 ≥ 1", file=sys.stderr)
            return 2
        run_validity(_providers(args.providers), args.authorized_llm_calls)
    elif args.mode == "owner-deadline":
        if args.authorized_llm_calls < 1:
            print("拒绝执行：--authorized-llm-calls 必须 ≥ 1", file=sys.stderr)
            return 2
        run_owner_deadline(_providers(args.providers), args.authorized_llm_calls)
    else:
        report = build_report()
        CALIBRATION_OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items()
                          if k not in ("validity_axis", "owner_deadline_axis")},
                         ensure_ascii=False, indent=2))
        for axis in ("validity_axis", "owner_deadline_axis"):
            if axis in report:
                print(f"\n== {axis} ==")
                print(json.dumps(report[axis], ensure_ascii=False, indent=2)[:2500])
        print(f"\nwrote {CALIBRATION_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
