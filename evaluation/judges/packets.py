"""Adjudication packets and inter-annotator agreement.

The offline proxies in this package are validated against each other and against
the product's own reviewer, never against an independent judgement. This module
closes as much of that gap as can be closed without a human:

* it builds a **blinded** packet — the annotator sees only an item and the
  transcript region it came from, never the reviewer's verdict, the proxy's
  verdict, or which version of the minutes the item came from;
* it defines a four-way label scheme in which ``derivable`` is kept separate,
  because "correctly inferred but not stated" is the class the product is
  supposed to remove and therefore must not be silently merged into "wrong";
* it computes agreement (raw rate plus Cohen's kappa) between any two sets of
  labels, so an AI adjudication can be reported as AI-versus-AI agreement with
  its own uncertainty rather than being passed off as human gold.

**What an AI adjudication is not.** It is a third opinion, not a gold standard.
Agreement between two model families shows the task is well defined and the
labels reproducible; it does not establish human validity, and it must never be
reported as "fact accuracy" or "human acceptance rate". Its most valuable output
is the **discordance list**: the items where the independent adjudicator and the
product's reviewer disagree are exactly the items a human should read, which
turns "label 100 items" into "read the 20 disagreements".
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in (None, ""):  # allow direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from regression import analysis, grounding as g  # type: ignore
    from lib import stats  # type: ignore
else:
    from regression import analysis, grounding as g
    from lib import stats

# Four labels. ``derivable`` is the Benign class: true and useful but absent from
# the transcript, which is precisely what the review step is meant to remove.
LABELS = ("supported", "derivable", "partial", "unsupported")
GROUNDED_LABELS = frozenset({"supported", "derivable"})
LABEL_GUIDE = {
    "supported": "核心内容可在转写中找到依据（允许改写、同义替换、压缩）",
    "derivable": "原文未直接陈述，但可由已陈述事实正确推出（清点已列参会人、解析相对时间、归纳）",
    "partial": "核心有依据，但补入了原文没有的具体细节（数字、日期、人名、范围）",
    "unsupported": "核心内容在转写中找不到依据",
}


def _transcript_window(transcript: str, item_text: str, padding: int = 400) -> str:
    located = g.find_source_window(item_text, transcript, padding=padding)
    if located:
        return located["window"]
    # No lexical anchor: give the head of the transcript so the annotator can
    # still see the register of the meeting, and say so explicitly.
    return transcript[: min(len(transcript), 1200)]


def build_packet(
    cases: Sequence[Mapping[str, Any]],
    *,
    per_stratum: int = 6,
    owner_sample: int = 6,
    seed: int = 20260925,
) -> dict[str, Any]:
    """Sample items across strata and emit a blinded adjudication packet.

    Strata are chosen so the sample can support both directions of agreement:
    items the reviewer flagged (to test precision) and items it did not (to test
    recall). Sampling is seeded, so the packet is reproducible.
    """
    rng = random.Random(seed)
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for case in cases:
        case_id = case.get("case_id")
        transcript = case.get("transcript") or ""
        record = case.get("record") or {}
        output = case.get("baseline_output")
        if not isinstance(output, Mapping):
            continue
        rounds = analysis._round_inputs(record)
        first_round_issues = (rounds[0]["review_payload"].get("issues") if rounds else None) or []
        flagged: dict[str, dict[str, Any]] = {}
        for issue in first_round_issues:
            index = issue.get("index")
            if isinstance(index, int) and index >= 0:
                flagged[(issue.get("field"), index)] = issue

        analysis_result = g.analyze_output(output, transcript)
        proxy_flagged: set[tuple[str, int]] = set()
        for field, results in analysis_result["per_field"].items():
            for index, result in enumerate(results):
                if result["has_unsupported"]:
                    proxy_flagged.add((field, index))

        for field, results in analysis_result["per_field"].items():
            for index, result in enumerate(results):
                prose = g.item_text_for_content(field, output.get(field, [None] * (index + 1))[index]
                                                if field != "summary" else output.get(field))
                if not prose or len(g.normalize(prose)) < 12:
                    continue
                entry = {
                    "case_id": case_id,
                    "field": field,
                    "item_index": index,
                    "item_text": prose,
                    "transcript_window": _transcript_window(transcript, prose),
                    "version": "baseline",
                }
                issue = flagged.get((field, index))
                if issue:
                    entry["reviewer_issue_type"] = issue.get("issue_type")
                    entry["reviewer_detail"] = issue.get("detail")
                    entry["reviewer_suggestion"] = issue.get("suggestion")
                    strata[f"reviewer_{issue.get('issue_type')}"].append(entry)
                else:
                    entry["proxy_flagged_atom"] = (field, index) in proxy_flagged
                    stratum = "proxy_only" if (field, index) in proxy_flagged else "unflagged"
                    strata[stratum].append(entry)

        for field in ("decisions", "pending_items"):
            for index, item in enumerate(g.iter_items(output, field)):
                if not isinstance(item, Mapping):
                    continue
                owner = item.get("owner_suggestion")
                if not isinstance(owner, str) or not owner.strip():
                    continue
                hard = [s for s in g.split_entities(owner) if s["class"] == "hard"]
                unsupported = [
                    s for s in hard
                    if not g.atom_is_supported(s, g.build_transcript_index(transcript))
                ]
                if unsupported:
                    strata["owner_fabricated"].append({
                        "case_id": case_id,
                        "field": field,
                        "item_index": index,
                        "item_text": f"负责人字段：{owner}",
                        "transcript_window": _transcript_window(transcript, item.get("content") or owner),
                        "version": "baseline",
                    })

    # Draw a balanced sample, then shuffle so stratum membership is not inferable
    # from position, and renumber anonymously.
    wanted = {
        "reviewer_UNSUPPORTED": per_stratum,
        "reviewer_MISSING_ITEM": max(2, per_stratum // 2),
        "reviewer_NOT_ACTIONABLE": max(2, per_stratum // 2),
        "reviewer_VAGUE": max(2, per_stratum // 2),
        "proxy_only": max(2, per_stratum // 2),
        "unflagged": per_stratum,
        "owner_fabricated": owner_sample,
    }
    sampled: list[tuple[str, dict[str, Any]]] = []
    for stratum, count in wanted.items():
        pool = sorted(strata.get(stratum, []), key=lambda e: (e["case_id"], e["field"], e["item_index"]))
        rng.shuffle(pool)
        for entry in pool[:count]:
            sampled.append((stratum, entry))
    rng.shuffle(sampled)

    items: list[dict[str, Any]] = []
    key: dict[str, dict[str, Any]] = {}
    for position, (stratum, entry) in enumerate(sampled, start=1):
        anonymous_id = f"item_{position:03d}"
        items.append({
            "anonymous_id": anonymous_id,
            "field": entry["field"],
            "item_text": entry["item_text"],
            "transcript_window": entry["transcript_window"],
            "label": None,
            "note": "",
        })
        key[anonymous_id] = {
            "stratum": stratum,
            "case_id": entry["case_id"],
            "item_index": entry["item_index"],
            "reviewer_issue_type": entry.get("reviewer_issue_type"),
            "reviewer_detail": entry.get("reviewer_detail"),
            "proxy_flagged_atom": entry.get("proxy_flagged_atom"),
            "version": entry["version"],
        }
    return {
        "packet_version": 1,
        "seed": seed,
        "blinding": (
            "items are shuffled and anonymously numbered; the reviewer's verdict, the proxy's "
            "verdict and the version (draft or revised) are withheld until labels are recorded"
        ),
        "label_guide": LABEL_GUIDE,
        "labels_allowed": list(LABELS),
        "available_by_stratum": {k: len(v) for k, v in sorted(strata.items())},
        "items": items,
        "_key": key,
    }


def build_packet_for_v2(
    cases: Sequence[Mapping[str, Any]],
    *,
    unflagged_sample: int = 30,
    proxy_only_sample: int = 20,
    seed: int = 20260925,
) -> dict[str, Any]:
    """契约 v2 产物的盲态包：**覆盖全部被标记条目**，另抽两组成对照。

    为什么另写一个而不是给 `build_packet` 加参数：

    1. `build_packet` 只看**第一轮**审查意见、且只取 baseline。v2 的 REVIEW 步分布在两轮上
       （第 1 轮审初稿、第 2 轮审修订稿），只看第一轮会把"审查提了什么"砍掉一多半，
       于是"审查 precision"的分母是错的。
    2. `build_packet` 的负责人分层读 `owner_suggestion`——那是契约 v1 的字段名。
       v2 把负责人换成了类型化槽位 `owner.text`，于是那一层在 v2 产物上**静默为空**
       （实测：`available_by_stratum` 里根本没有它）。这正是本项目反复出现的"无声的零"。

    因此这里：遍历**所有** REVIEW 步，按 `round_no` 判定它审的是初稿还是修订稿
    （第 1 轮审初稿，其后审上一轮的修订稿），把所有被标记条目**全数**纳入，
    另抽未标记与"只有词法代理标记"两组作反向对照。
    """
    rng = random.Random(seed)
    flagged: list[dict[str, Any]] = []
    unflagged: list[dict[str, Any]] = []
    proxy_only: list[dict[str, Any]] = []

    for case in cases:
        case_id = case.get("case_id")
        transcript = case.get("transcript") or ""
        record = case.get("record") or {}
        outputs = {"baseline": case.get("baseline_output"), "revised": case.get("revised_output")}
        seen: set[tuple[str, str, int]] = set()

        # 审查意见：按轮次归到它审的那一版
        for step in record.get("agent_steps") or []:
            if not isinstance(step, Mapping) or step.get("step_type") != "REVIEW":
                continue
            round_no = int(step.get("round_no") or 1)
            version = "baseline" if round_no <= 1 else "revised"
            output = outputs.get(version)
            if not isinstance(output, Mapping):
                continue
            for issue in (step.get("payload") or {}).get("issues") or []:
                field = issue.get("field")
                index = issue.get("index")
                if not isinstance(field, str) or not isinstance(index, int) or index < 0:
                    continue
                key = (version, field, index)
                if key in seen:
                    continue
                seen.add(key)
                items = output.get(field) or []
                if field == "summary":
                    item = output.get("summary")
                elif index < len(items):
                    item = items[index]
                else:
                    continue
                prose = g.item_text_for_content(field, item)
                if not prose or len(g.normalize(prose)) < 12:
                    continue
                flagged.append({
                    "case_id": case_id,
                    "field": field,
                    "item_index": index,
                    "item_text": prose,
                    "transcript_window": _transcript_window(transcript, prose),
                    "version": version,
                    "reviewer_issue_type": issue.get("issue_type"),
                    "reviewer_detail": issue.get("detail"),
                    "stratum": f"reviewer_{issue.get('issue_type')}",
                })

        # 反向对照：词法代理标记 / 完全未被标记（只用初稿，与审查的第一轮同源）
        output = outputs.get("baseline")
        if isinstance(output, Mapping):
            analysis_result = g.analyze_output(output, transcript)
            for field, results in analysis_result["per_field"].items():
                for index, result in enumerate(results):
                    items = output.get(field) or []
                    if field == "summary":
                        item = output.get("summary")
                    elif index < len(items):
                        item = items[index]
                    else:
                        continue
                    prose = g.item_text_for_content(field, item)
                    if not prose or len(g.normalize(prose)) < 12:
                        continue
                    entry = {
                        "case_id": case_id,
                        "field": field,
                        "item_index": index,
                        "item_text": prose,
                        "transcript_window": _transcript_window(transcript, prose),
                        "version": "baseline",
                        "proxy_flagged_atom": bool(result["has_unsupported"]),
                    }
                    (proxy_only if result["has_unsupported"] else unflagged).append(entry)

    # 匿名化：被标记条目全数纳入；两组对照按 n 抽样
    sampled: list[dict[str, Any]] = list(flagged)
    for pool, count in ((proxy_only, proxy_only_sample), (unflagged, unflagged_sample)):
        ordered = sorted(pool, key=lambda e: (e["case_id"], e["field"], e["item_index"]))
        rng.shuffle(ordered)
        sampled.extend(ordered[:count])
    rng.shuffle(sampled)

    items: list[dict[str, Any]] = []
    key_map: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(sampled, start=1):
        anonymous_id = f"item_{position:03d}"
        items.append({
            "anonymous_id": anonymous_id,
            "field": entry["field"],
            "item_text": entry["item_text"],
            "transcript_window": entry["transcript_window"],
            "label": None,
            "note": "",
        })
        key_map[anonymous_id] = {
            "stratum": entry["stratum"] if "stratum" in entry else (
                "proxy_only" if entry.get("proxy_flagged_atom") else "unflagged"),
            "case_id": entry["case_id"],
            "item_index": entry["item_index"],
            "reviewer_issue_type": entry.get("reviewer_issue_type"),
            "reviewer_detail": entry.get("reviewer_detail"),
            "proxy_flagged_atom": entry.get("proxy_flagged_atom"),
            "version": entry["version"],
        }

    available = Counter(key_map[i["anonymous_id"]]["stratum"] for i in items)
    return {
        "packet_version": 2,
        "source_dataset": "vcsum-test26-v2",
        "seed": seed,
        "blinding": (
            "items are shuffled and anonymously numbered; the reviewer's verdict, the proxy's "
            "verdict and the version (draft or revised) are withheld until labels are recorded"
        ),
        "label_guide": LABEL_GUIDE,
        "labels_allowed": list(LABELS),
        "available_by_stratum": dict(sorted(available.items())),
        "flagged_total_in_corpus": len(flagged),
        "items": items,
        "_key": key_map,
    }


def label_collapse(label: str) -> bool | None:
    """Map a four-way label to the binary grounded / not-grounded used by the proxies."""
    if label in GROUNDED_LABELS:
        return True
    if label in {"partial", "unsupported"}:
        return False
    return None


def cohens_kappa(first: Sequence[str], second: Sequence[str]) -> dict[str, Any]:
    """Cohen's kappa with raw agreement and an exact interval on the raw rate."""
    pairs = [(a, b) for a, b in zip(first, second) if a and b]
    if not pairs:
        return {"n": 0, "raw_agreement": None, "kappa": None}
    n = len(pairs)
    agree = sum(1 for a, b in pairs if a == b)
    first_counts = Counter(a for a, _ in pairs)
    second_counts = Counter(b for _, b in pairs)
    expected = sum(first_counts[k] * second_counts[k] for k in set(first_counts) | set(second_counts)) / (n * n)
    observed = agree / n
    kappa = (observed - expected) / (1 - expected) if expected < 1 else None
    return {
        "n": n,
        "raw_agreement": observed,
        "raw_agreement_95": stats.clopper_pearson_interval(agree, n),
        "kappa": kappa,
        "expected_agreement": expected,
        "note": "kappa is unstable at this n; the raw rate and its exact interval carry the reading",
    }


def binary_agreement_extras(
    first: Sequence[str], second: Sequence[str], *, first_name: str = "adjudicator",
    second_name: str = "reviewer",
) -> dict[str, Any]:
    """"flagged"/"clean" 混淆矩阵，以及 κ 之外必须一起报的四个量。

    为什么不能只报 κ（`JUDGMENT_STANDARD.md` 第六节的要求）：实测踩到过 14/16 的
    单向分歧在偏斜患病率下让 κ 坍缩到 ≈0——κ 把"系统性偏差"读成了"随机噪声"，
    而问题恰恰是**单向**的。因此这里同时给出：

    * **PABAK** = 2·p₀ − 1（不看边缘分布的校正一致率）；
    * **患病率指数**（双方都判 flagged 与都判 clean 的不平衡程度）；
    * **偏差指数**（一方比另一方更爱判 flagged 的程度）；
    * **两个方向的错误率分开报**——单向偏差只能从这里看出来。
    """
    pairs = [(a, b) for a, b in zip(first, second) if a and b]
    n = len(pairs)
    if not n:
        return {"n": 0}
    both_flagged = sum(1 for a, b in pairs if a == "flagged" and b == "flagged")
    only_first = sum(1 for a, b in pairs if a == "flagged" and b != "flagged")
    only_second = sum(1 for a, b in pairs if a != "flagged" and b == "flagged")
    both_clean = sum(1 for a, b in pairs if a != "flagged" and b != "flagged")
    observed = (both_flagged + both_clean) / n
    first_flagged = both_flagged + only_first
    second_flagged = both_flagged + only_second
    return {
        "n": n,
        "confusion": {
            "both_flagged": both_flagged,
            f"only_{first_name}_flagged": only_first,
            f"only_{second_name}_flagged": only_second,
            "both_clean": both_clean,
        },
        "pabak": 2 * observed - 1,
        "prevalence_index": abs(both_flagged - both_clean) / n,
        "bias_index": abs(only_first - only_second) / n,
        "error_rate": {
            f"{second_name}_flagged_but_{first_name}_did_not": (
                stats.proportion_report(only_second, second_flagged) if second_flagged else None
            ),
            f"{first_name}_flagged_but_{second_name}_did_not": (
                stats.proportion_report(only_first, first_flagged) if first_flagged else None
            ),
        },
        "reading": (
            "两个方向的错误率必须分开读：若只有一个方向大，说明是**系统性偏差**而非随机分歧，"
            "此时 κ 被患病率压扁，不能作为「无一致性」的证据。"
        ),
    }


def reconcile(packet: Mapping[str, Any], *, label_field: str = "label") -> dict[str, Any]:
    """Agreement between the adjudicator's labels and the reviewer / proxy.

    All three comparisons are made on a single binary scale (grounded vs
    not-grounded) so the kappa values are mutually comparable. The four-way
    label distribution is reported alongside it, because collapsing hides the
    ``derivable`` class that matters most for this product.
    """
    key = packet["_key"]
    items = packet["items"]
    labelled = [i for i in items if i.get(label_field)]
    if not labelled:
        return {"labelled_count": 0, "note": "no labels recorded yet"}

    adjudicated_four: list[str] = []
    adjudicated_binary: list[str] = []
    reviewer_binary: list[str] = []
    proxy_binary: list[str] = []
    per_stratum: dict[str, Counter[str]] = defaultdict(Counter)
    discordant: list[dict[str, Any]] = []

    for item in labelled:
        info = key[item["anonymous_id"]]
        label = item[label_field]
        adjudicated_four.append(label)
        per_stratum[info["stratum"]][label] += 1
        collapsed = label_collapse(label)
        if collapsed is None:
            continue
        adjudicator_flagged = not collapsed
        reviewer_flagged = bool(info.get("reviewer_issue_type")) or info["stratum"] == "owner_fabricated"
        proxy_flagged = bool(info.get("proxy_flagged_atom")) or info["stratum"] == "owner_fabricated"
        adjudicated_binary.append("flagged" if adjudicator_flagged else "clean")
        reviewer_binary.append("flagged" if reviewer_flagged else "clean")
        proxy_binary.append("flagged" if proxy_flagged else "clean")
        if adjudicator_flagged == reviewer_flagged:
            continue
        entry = {
            "anonymous_id": item["anonymous_id"],
            "case_id": info["case_id"],
            "field": item["field"],
            "stratum": info["stratum"],
            "adjudicated_label": label,
            "item_text": item["item_text"][:170],
            "direction": ("adjudicator flagged, reviewer did not" if adjudicator_flagged
                          else "reviewer flagged, adjudicator did not"),
        }
        if not adjudicator_flagged:
            entry["reviewer_issue_type"] = info.get("reviewer_issue_type")
            entry["reviewer_detail"] = (info.get("reviewer_detail") or "")[:170]
        discordant.append(entry)

    return {
        "labelled_count": len(labelled),
        "label_distribution": dict(Counter(adjudicated_four)),
        "grounded_binary_distribution": dict(Counter(adjudicated_binary)),
        "per_stratum_adjudicated_label": {k: dict(v) for k, v in per_stratum.items()},
        "agreement_adjudicator_vs_reviewer": cohens_kappa(adjudicated_binary, reviewer_binary),
        "agreement_adjudicator_vs_proxy": cohens_kappa(adjudicated_binary, proxy_binary),
        "agreement_reviewer_vs_proxy": cohens_kappa(reviewer_binary, proxy_binary),
        # κ 之外必报的部分：偏斜患病率下 κ 会坍缩，单向偏差必须单列
        "binaries_adjudicator_vs_reviewer": binary_agreement_extras(
            adjudicated_binary, reviewer_binary),
        "binaries_adjudicator_vs_proxy": binary_agreement_extras(
            adjudicated_binary, proxy_binary, second_name="proxy"),
        "binaries_reviewer_vs_proxy": binary_agreement_extras(
            reviewer_binary, proxy_binary, first_name="reviewer", second_name="proxy"),
        "discordant_items": discordant,
        "sampling_note": (
            "The sample is stratified, so raw agreement rates are not the population rates and "
            "only the kappa comparisons between the three columns are meaningful. Read kappa, not "
            "the raw rate, when comparing reviewer against proxy."
        ),
        "framing": (
            "This is AI-versus-AI agreement, not human validity. It bounds how reproducible the "
            "judgement is, and its discordance list is the input to a human review; it does not "
            "license calling any number a fact accuracy or human acceptance rate."
        ),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build", action="store_true", help="write a fresh blinded packet")
    parser.add_argument("--reconcile", type=Path, help="reconcile a labelled packet")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "results" / "adjudication_packet.json")
    parser.add_argument("--labels", type=Path, help="JSON file of {anonymous_id: label}")
    parser.add_argument("--set", dest="dataset", default="test26",
                        choices=["test26", "test26-v2"],
                        help="test26=旧契约产物；test26-v2=契约 v2 产物（默认取哪一代会改变判定的含义）")
    parser.add_argument("--per-stratum", type=int, default=6)
    parser.add_argument("--owner-sample", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260925)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.build:
        # 直接当脚本跑时（`python evaluation/metrics/adjudication.py`）没有父包，
        # 相对导入会失败——本模块顶部就是为这种情况准备的 sys.path 兜底，
        # 这里必须用同一套，否则 CLI 的 --build 从来跑不通。
        if __package__ in (None, ""):
            from lib import corpora  # type: ignore
        else:
            from lib import corpora  # type: ignore
        if args.dataset == "test26-v2":
            cases = corpora.load_new_contract_cases()
        else:
            cases = corpora.load_vcsum_cases("test26")
        packet = build_packet(cases, per_stratum=args.per_stratum,
                              owner_sample=args.owner_sample, seed=args.seed)
        packet["source_dataset"] = args.dataset
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.output} with {len(packet['items'])} blinded items "
              f"(dataset={args.dataset}, per_stratum={args.per_stratum})")
        print(json.dumps(packet["available_by_stratum"], ensure_ascii=False))
        return 0

    if args.reconcile:
        packet = json.loads(args.reconcile.read_text(encoding="utf-8"))
        if args.labels:
            overrides = json.loads(args.labels.read_text(encoding="utf-8"))
            for item in packet["items"]:
                if item["anonymous_id"] in overrides:
                    item["label"] = overrides[item["anonymous_id"]]
        result = reconcile(packet)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
