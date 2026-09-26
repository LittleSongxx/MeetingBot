"""Paired, offline metric computations over stored evaluation results.

Everything here reads artifacts that already exist on disk (``results/combined.json``
for the synthetic set, ``results/*/report.json`` for the VCSum public set). No
function in this module calls a model, and none of them require new annotation.

The metrics implemented are the ones ``METRICS_RESEARCH.md`` identified as
load-bearing and structurally missing from the previous evaluation:

* ``transition_report``  -- paired draft/revised item transitions, including the
  harm direction (correct content removed or broken) that a net ROUGE delta hides.
* ``critique_report``     -- agreement between the reviewer's ``UNSUPPORTED``
  issues and an independent offline grounding check, in both directions.
* ``slot_report``         -- owner/deadline fabrication counts, plus the
  measurement that decides whether ``MISSING_DEADLINE`` should be conditional.
* ``schema_report``       -- schema validity separated from content correctness.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

from regression import grounding as g
from lib import stats

FIELDS = ("summary", "topics", "viewpoints", "decisions", "pending_items", "risks")

# Mirrors the product's MinutesResult contract. The authoritative definition is
# the product's own schema; this is a structural restatement for field-level
# attribution of failures.
CONTRACT: dict[str, dict[str, Any]] = {
    "summary": {"kind": "str"},
    "topics": {"kind": "list", "item_keys": {"title": str, "summary": str}},
    "viewpoints": {"kind": "list", "item_keys": {"speaker": str, "viewpoint": str}},
    "decisions": {
        "kind": "list",
        "item_keys": {
            "content": str, "basis": str, "owner_suggestion": str, "deadline_suggestion": str,
        },
    },
    "pending_items": {
        "kind": "list",
        "item_keys": {"content": str, "owner_suggestion": str, "deadline_suggestion": str},
    },
    "risks": {
        "kind": "list",
        "item_keys": {"content": str, "level": str, "suggestion": str},
        "enum": {"level": ("LOW", "MEDIUM", "HIGH")},
    },
}

MATCH_THRESHOLD = 0.5
MIN_MATCHED_BLOCK = 4
# 这两个常量只作为**产品目录不可用时的兜底声明**保留：判据本身已统一到
# `common/textsim.py`，`_is_match` 委托过去。报告里标注的阈值读
# `grounding.shared_thresholds()`，因此不会与真正生效的值脱节。


# --------------------------------------------------------------------------- #
# Item pairing
# --------------------------------------------------------------------------- #

def _similarity(a: str, b: str) -> tuple[float, int]:
    """Return (ratio, longest common block length).

    Both are needed: on short items a high ratio can come from a two-character
    coincidence (``旧事项`` vs ``全新的事项`` scores 0.50), so pairing also
    requires a substantively long common block.
    """
    if not a or not b:
        return 0.0, 0
    if a == b:
        return 1.0, len(a)
    matcher = SequenceMatcher(None, a, b)
    longest = max((block.size for block in matcher.get_matching_blocks()), default=0)
    return matcher.ratio(), longest


def _is_match(a: str, b: str) -> bool:
    """Acceptance of a pair. **Delegates to the shared criterion when available.**

    The thresholds are calibrated in one place (``common/textsim.py``) and the
    product uses the same module to detect silent deletions, so a local copy
    here would drift silently: re-tuning the shared threshold would leave this
    pairing behind and the transition matrix would disagree with the product
    about what counts as "the same item". ``_similarity`` stays local because
    ranking candidates (not accepting them) is an evaluation-side concern.
    """
    return g.shared_is_same_item(a, b)



def pair_items(field: str, base_items: Sequence[Any], rev_items: Sequence[Any]) -> dict[str, Any]:
    """Pair baseline items with revised items of the same field.

    Exact normalized matches are taken first, then greedy best-similarity
    matches that clear both ``MATCH_THRESHOLD`` and ``MIN_MATCHED_BLOCK``.
    Unmatched items are reported explicitly so a reader can see how much of the
    comparison rests on fuzzy pairing.
    """
    base_texts = [g.normalize(g.item_text(field, item)) for item in base_items]
    rev_texts = [g.normalize(g.item_text(field, item)) for item in rev_items]
    used_rev: set[int] = set()
    pairs: list[tuple[int, int]] = []

    for i, base_text in enumerate(base_texts):
        if not base_text:
            continue
        for j, rev_text in enumerate(rev_texts):
            if j in used_rev or not rev_text:
                continue
            if base_text == rev_text:
                pairs.append((i, j))
                used_rev.add(j)
                break

    candidates: list[tuple[float, int, int]] = []
    paired_base = {i for i, _ in pairs}
    for i, base_text in enumerate(base_texts):
        if i in paired_base or not base_text:
            continue
        for j, rev_text in enumerate(rev_texts):
            if j in used_rev or not rev_text:
                continue
            if _is_match(base_text, rev_text):
                candidates.append((_similarity(base_text, rev_text)[0], i, j))
    for score, i, j in sorted(candidates, reverse=True):
        if i in paired_base or j in used_rev:
            continue
        pairs.append((i, j))
        paired_base.add(i)
        used_rev.add(j)

    base_only = [i for i in range(len(base_items)) if i not in paired_base]
    rev_only = [j for j in range(len(rev_items)) if j not in used_rev]
    return {"pairs": pairs, "base_only": base_only, "rev_only": rev_only}


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #

def _analyze_side(output: Mapping[str, Any] | None, transcript: str) -> dict[str, Any]:
    return g.analyze_output(output, transcript)


def transition_report(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Paired item-level transitions between the draft and the reviewed version.

    Claim-level transitions use matched pairs only, which is what a paired
    statistical test requires. Set-level counts additionally include items that
    were removed or added, because removal of unsupported content is a real
    product outcome that a matched-pairs view cannot show.
    """
    claim_counts: Counter[str] = Counter()
    set_counts: Counter[str] = Counter()
    per_field: dict[str, Counter[str]] = defaultdict(Counter)
    per_case: list[dict[str, Any]] = []
    example_bag: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for case in cases:
        case_id = case.get("case_id")
        transcript = case.get("transcript") or ""
        base_output = case.get("baseline_output")
        rev_output = case.get("revised_output")
        base = _analyze_side(base_output, transcript)
        rev = _analyze_side(rev_output, transcript)

        case_claim: Counter[str] = Counter()
        case_set: Counter[str] = Counter()
        fuzzy_pairs = 0

        for field in FIELDS:
            base_results = base["per_field"].get(field, [])
            rev_results = rev["per_field"].get(field, [])
            base_items = g.iter_items(base_output, field)
            rev_items = g.iter_items(rev_output, field)
            if not base_results and not rev_results:
                continue
            pairing = pair_items(field, base_items, rev_items)
            exact_lookup = {
                (i, j): (g.normalize(g.item_text(field, base_items[i]))
                         == g.normalize(g.item_text(field, rev_items[j])))
                for i, j in pairing["pairs"]
            }
            # Pools used to tell consolidation from deletion and split from
            # addition, so the churn counts are not inflated by restructuring.
            rev_gram_pool: set[str] = set()
            for item in rev_items:
                rev_gram_pool |= g.content_gram_set(g.item_text_for_content(field, item))
            base_gram_pool: set[str] = set()
            for item in base_items:
                base_gram_pool |= g.content_gram_set(g.item_text_for_content(field, item))

            for i, j in pairing["pairs"]:
                if not exact_lookup[(i, j)]:
                    fuzzy_pairs += 1
                base_flagged = bool(base_results[i]["has_unsupported"]) if i < len(base_results) else False
                rev_flagged = bool(rev_results[j]["has_unsupported"]) if j < len(rev_results) else False
                if base_flagged and not rev_flagged:
                    label = "fixed_to_supported"
                elif base_flagged and rev_flagged:
                    label = "kept_unsupported"
                elif not base_flagged and rev_flagged:
                    label = "introduced_unsupported"
                else:
                    label = "kept_supported"
                claim_counts[label] += 1
                case_claim[label] += 1
                per_field[field][label] += 1
                if label in {"introduced_unsupported", "fixed_to_supported"} and len(example_bag[label]) < 6:
                    example_bag[label].append({
                        "case_id": case_id, "field": field,
                        "text": g.item_text(field, rev_items[j] if label == "introduced_unsupported" else base_items[i])[:160],
                    })

            for i in pairing["base_only"]:
                flagged = bool(base_results[i]["has_unsupported"]) if i < len(base_results) else False
                base_grams = g.content_gram_set(g.item_text_for_content(field, base_items[i]))
                if flagged:
                    label = "removed_unsupported"
                elif g.is_coverable_by(base_grams, rev_gram_pool):
                    # Its wording survives elsewhere in the revised field, so this
                    # is a consolidation, not a deletion of correct content.
                    label = "merged_supported"
                else:
                    label = "removed_supported"
                set_counts[label] += 1
                case_set[label] += 1
                per_field[field][label] += 1
                if label == "removed_supported" and len(example_bag[label]) < 6:
                    example_bag[label].append({
                        "case_id": case_id, "field": field,
                        "text": g.item_text(field, base_items[i])[:160],
                    })

            for j in pairing["rev_only"]:
                flagged = bool(rev_results[j]["has_unsupported"]) if j < len(rev_results) else False
                rev_grams = g.content_gram_set(g.item_text_for_content(field, rev_items[j]))
                if flagged:
                    label = "added_unsupported"
                elif g.is_coverable_by(rev_grams, base_gram_pool):
                    # The wording already existed across the draft, so this is a
                    # split of existing content rather than new content.
                    label = "split_supported"
                else:
                    label = "added_supported"
                set_counts[label] += 1
                case_set[label] += 1
                per_field[field][label] += 1
                if label == "added_unsupported" and len(example_bag[label]) < 6:
                    example_bag[label].append({
                        "case_id": case_id, "field": field,
                        "text": g.item_text(field, rev_items[j])[:160],
                    })

        unsupported_before = base["flagged_item_count"]
        unsupported_after = rev["flagged_item_count"]
        per_case.append({
            "case_id": case_id,
            "claim_transitions": dict(case_claim),
            "set_transitions": dict(case_set),
            "unsupported_items_before": unsupported_before,
            "unsupported_items_after": unsupported_after,
            "paired_difference": unsupported_after - unsupported_before,
            "checkable_atoms_before": base["checkable_atom_count"],
            "checkable_atoms_after": rev["checkable_atom_count"],
            "fuzzy_pairs": fuzzy_pairs,
        })

    gains = claim_counts["fixed_to_supported"] + set_counts["removed_unsupported"]
    harms = claim_counts["introduced_unsupported"] + set_counts["removed_supported"] + set_counts["added_unsupported"]
    mcnemar = stats.mcnemar_exact(
        claim_counts["introduced_unsupported"], claim_counts["fixed_to_supported"]
    )
    differences = [c["paired_difference"] for c in per_case]
    record_sign = stats.mcnemar_exact(
        sum(1 for d in differences if d > 0), sum(1 for d in differences if d < 0)
    )

    return {
        "case_count": len(cases),
        "claim_level_transitions": dict(claim_counts),
        "set_level_transitions": dict(set_counts),
        "net_grounded_item_change": gains - harms,
        "claim_level_mcnemar": mcnemar,
        "record_level_sign_test": record_sign,
        "per_case": per_case,
        "per_field": {field: dict(counter) for field, counter in per_field.items()},
        "paired_bootstrap": stats.paired_bootstrap_mean_ci(differences),
        "examples_for_audit": {k: v for k, v in example_bag.items()},
        "pairing_note": (
            f"matched pairs use exact text equality first, then similarity >= "
            f"{g.shared_thresholds()['similarity']:g} "
            f"with a common block of at least {g.shared_thresholds()['min_matched_block']:g} "
            f"characters (thresholds read from {g.shared_thresholds()['source']}); "
            "unmatched items are reported as removed/added and are excluded from the McNemar test"
        ),
        "removal_caution": (
            "removed_supported counts items whose wording does not survive anywhere in the revised "
            "field, so consolidations are excluded into merged_supported. It remains an upper bound "
            "on over-correction because a legitimate rewrite with little shared wording also lands "
            "here; read the examples_for_audit entries before quoting it."
        ),
        "interpretation": (
            "Grounding here is a lexical proxy over dates, quantities and named entities. "
            "A null or negative net change is a legitimate outcome: the proxy has limited power, "
            "so it can under-detect real improvement and cannot be read as proof of harm."
        ),
    }


# --------------------------------------------------------------------------- #
# Critique quality
# --------------------------------------------------------------------------- #

def _round_inputs(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Reconstruct what each REVIEW round actually saw.

    Round 1 reviews the initial draft; round N reviews the previous round's
    REFINE payload, which the product stores in full.
    """
    steps = record.get("agent_steps") or []
    review_steps = [s for s in steps if s.get("step_type") == "REVIEW"]
    refine_steps = {s.get("round_no"): s for s in steps if s.get("step_type") == "REFINE"}
    rounds: list[dict[str, Any]] = []
    for position, review in enumerate(review_steps):
        round_no = review.get("round_no")
        if position == 0:
            reviewed_output = (record.get("baseline") or {}).get("output")
            source = "baseline.output"
        else:
            previous = refine_steps.get(review_steps[position - 1].get("round_no"))
            reviewed_output = (previous or {}).get("payload") or None
            source = f"round {review_steps[position - 1].get('round_no')} REFINE payload"
        rounds.append({
            "round_no": round_no,
            "review_payload": review.get("payload") or {},
            "reviewed_output": reviewed_output,
            "reviewed_source": source,
        })
    return rounds


def critique_report(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Agreement between the reviewer's UNSUPPORTED issues and the offline check.

    Both directions are measured against the *same* proxy, so this is agreement
    with the proxy, not accuracy. Its purpose is to distinguish three failure
    modes that look identical in an end-to-end score:

    * the reviewer finds real problems but the refiner cannot fix them;
    * the reviewer emits issue-shaped boilerplate (the documented degeneracy in
      long-horizon reflection harnesses);
    * the two disagree because the reviewer is reading semantics the proxy
      cannot see -- which is a limit of the proxy, not of the reviewer.
    """
    true_positive_issues = 0
    unsupported_issues_total = 0
    flagged_items_total = 0
    flagged_items_covered = 0
    type_counts: Counter[str] = Counter()
    passed_with_flags = 0
    passed_rounds = 0
    per_round_rows: list[dict[str, Any]] = []
    issue_load: list[float] = []
    flag_load: list[float] = []
    index_histogram: Counter[str] = Counter()
    content_by_field: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"flagged": [], "clean": []})
    # Per-meeting alignment between the recall proxy and the reviewer's own
    # MISSING_ITEM count. Recomputed on every run so the calibration is a live
    # measurement rather than a one-off claim made once and then trusted.
    alignment_uncovered: list[float] = []
    alignment_missing_item: list[float] = []
    alignment_supported_issues: list[float] = []
    alignment_invented: list[float] = []

    for case in cases:
        transcript = case.get("transcript") or ""
        for round_info in _round_inputs(case.get("record") or {}):
            payload = round_info["review_payload"]
            issues = payload.get("issues") or []
            reviewed_output = round_info["reviewed_output"]
            if not isinstance(reviewed_output, Mapping):
                continue
            analysis = _analyze_side(reviewed_output, transcript)

            flagged_positions: dict[str, set[int]] = {}
            for field, results in analysis["per_field"].items():
                flagged_positions[field] = {
                    index for index, result in enumerate(results) if result["has_unsupported"]
                }

            # Content-overlap support, per field, split by whether the reviewer
            # flagged the item. Restricted to structured list fields: this is a
            # separate signal from the atom check and behaves differently by field.
            reported_positions: dict[str, set[int]] = defaultdict(set)
            for issue in issues:
                if issue.get("issue_type") == "UNSUPPORTED" and isinstance(issue.get("index"), int):
                    reported_positions[issue.get("field")].add(issue["index"])
            for field, results in analysis["per_field"].items():
                if field not in g.CONTENT_SUPPORT_FIELDS and field not in ("risks", "viewpoints", "topics"):
                    continue
                for index, result in enumerate(results):
                    content = result.get("content_support")
                    if not content or content["gram_count"] < 8:
                        continue
                    bucket = "flagged" if index in reported_positions.get(field, set()) else "clean"
                    content_by_field[field][bucket].append(content["ratio"])

            unsupported_issues = [i for i in issues if i.get("issue_type") == "UNSUPPORTED"]
            hit = 0
            for issue in unsupported_issues:
                field = issue.get("field")
                index = issue.get("index")
                type_counts[str(issue.get("issue_type"))] += 1
                if index == -1:
                    # Whole-block issue: counts as covering the field if anything
                    # in that field was flagged by the proxy.
                    if flagged_positions.get(field):
                        hit += 1
                elif isinstance(index, int) and index in flagged_positions.get(field, set()):
                    hit += 1

            covered = 0
            for field, positions in flagged_positions.items():
                reported = {
                    i.get("index") for i in unsupported_issues
                    if i.get("field") == field and isinstance(i.get("index"), int)
                }
                if any(i.get("field") == field and i.get("index") == -1 for i in unsupported_issues):
                    covered += len(positions)
                else:
                    covered += len(positions & reported)

            flagged_count = sum(len(v) for v in flagged_positions.values())
            true_positive_issues += hit
            unsupported_issues_total += len(unsupported_issues)
            flagged_items_total += flagged_count
            flagged_items_covered += covered

            is_passed = bool(payload.get("passed"))
            if is_passed:
                passed_rounds += 1
                if flagged_count:
                    passed_with_flags += 1

            for issue in issues:
                index_histogram["whole_block" if issue.get("index") == -1 else "item"] += 1

            per_round_rows.append({
                "case_id": case.get("case_id"),
                "round_no": round_info["round_no"],
                "reviewed_source": round_info["reviewed_source"],
                "review_passed": is_passed,
                "issue_count": len(issues),
                "unsupported_issue_count": len(unsupported_issues),
                "proxy_flagged_item_count": flagged_count,
                "unsupported_issue_hits": hit,
                "proxy_flagged_items_covered": covered,
            })
            issue_load.append(float(len(issues)))
            flag_load.append(float(flagged_count))

            coverage = analysis.get("action_candidate_coverage") or {}
            alignment_uncovered.append(float(coverage.get("uncovered_count") or 0))
            alignment_invented.append(float(analysis.get("invented_span_count") or 0))
            alignment_missing_item.append(
                float(sum(1 for i in issues if i.get("issue_type") == "MISSING_ITEM"))
            )
            alignment_supported_issues.append(float(len(unsupported_issues)))

    return {
        "rounds_analyzed": len(per_round_rows),
        "review_pass_rate": (passed_rounds / len(per_round_rows)) if per_round_rows else None,
        "passed_rounds_with_proxy_flags": passed_with_flags,
        "passed_round_denominator": passed_rounds,
        "reviewer_precision_vs_proxy": stats.proportion_report(true_positive_issues, unsupported_issues_total),
        "reviewer_recall_vs_proxy": stats.proportion_report(flagged_items_covered, flagged_items_total),
        "issue_type_distribution": dict(type_counts),
        "issue_index_kind": dict(index_histogram),
        "spearman_issue_count_vs_flag_count": stats.spearman_rho(issue_load, flag_load),
        "correlation_note": (
            "A near-zero rank correlation between how many issues the reviewer raises and how much "
            "the proxy flags in the very text it is reading is evidence of content-independent "
            "boilerplate rather than review."
        ),
        "per_round": per_round_rows,
        "proxy_alignment": {
            "action_candidates_vs_missing_item": {
                "spearman_rho": stats.spearman_rho(alignment_uncovered, alignment_missing_item),
                "n_rounds": len(alignment_uncovered),
                "expected": "positive if the recall proxy tracked the reviewer's MISSING_ITEM count",
                "status": g.PROXY_STATUS["action_candidate_coverage"]["status"],
                "evidence": g.PROXY_STATUS["action_candidate_coverage"]["evidence"],
            },
            "invented_spans_vs_unsupported": {
                "spearman_rho": stats.spearman_rho(alignment_invented, alignment_supported_issues),
                "n_rounds": len(alignment_invented),
                "expected": "positive if absent n-grams tracked content-level invention",
                "status": g.PROXY_STATUS["invented_spans"]["status"],
                "evidence": g.PROXY_STATUS["invented_spans"]["evidence"],
            },
            "reading": (
                "This block re-measures each proxy's alignment on every run, and both entries above "
                "are rejected: their association with the reviewer's issue counts flips sign "
                "depending on whether counts are reported raw, as a rate, or per unit length. "
                "Do not quote either as a quality number, whichever way this run's rho falls. "
                "The consequence is explicit: MISSING_ITEM and content-level UNSUPPORTED have no "
                "working offline proxy, and closing that gap needs human adjudication."
            ),
        },
        "content_support_by_field": _summarize_content_support(content_by_field),
        "content_support_note": (
            "Content-overlap support is a second, independent signal from the atom check: it asks "
            "how much of an item's own wording appears in the transcript. It must NOT be pooled "
            "across fields. Measured on VCSum test26, risks score lowest (0.125) simply because "
            "risks are model-authored analysis, and within viewpoints the items the reviewer flags "
            "score HIGHER than average (0.333 vs 0.312). Only decisions and pending_items show the "
            "expected direction, so only those are used for discrimination."
        ),
        "interpretation": (
            "Precision and recall are measured against the same offline proxy, so they describe "
            "agreement with it, not correctness. Low agreement is ambiguous between a weak proxy "
            "and a weak reviewer; the correlation, the pass-with-flags count, and the content "
            "support discrimination together break the tie."
        ),
    }


def _summarize_content_support(
    content_by_field: Mapping[str, Mapping[str, Sequence[float]]]
) -> dict[str, Any]:
    """Per-field content-overlap support, split by whether the reviewer flagged it."""
    per_field: dict[str, Any] = {}
    pooled_flagged: list[float] = []
    pooled_clean: list[float] = []
    for field, buckets in sorted(content_by_field.items()):
        flagged = list(buckets.get("flagged", []))
        clean = list(buckets.get("clean", []))
        entry: dict[str, Any] = {
            "flagged_n": len(flagged),
            "clean_n": len(clean),
            "flagged_mean": (sum(flagged) / len(flagged)) if flagged else None,
            "clean_mean": (sum(clean) / len(clean)) if clean else None,
            "flags_discriminated": field in g.CONTENT_SUPPORT_FIELDS,
            "auc_flagged_vs_clean": stats.auc(clean, flagged),
        }
        per_field[field] = entry
        if field in g.CONTENT_SUPPORT_FIELDS:
            pooled_flagged.extend(flagged)
            pooled_clean.extend(clean)
    return {
        "per_field": per_field,
        "pooled_discrimination_fields": list(g.CONTENT_SUPPORT_FIELDS),
        "pooled_auc_flagged_vs_clean": stats.auc(pooled_clean, pooled_flagged),
        "pooled_flagged_n": len(pooled_flagged),
        "pooled_clean_n": len(pooled_clean),
        "reading": (
            "AUC here is P(a reviewer-flagged item scores higher on content overlap than an "
            "unflagged one). A value clearly below 0.5 means flagged items overlap the transcript "
            "LESS, which is the direction that supports the reviewer; 0.5 means the signal does "
            "not discriminate at all."
        ),
    }


# --------------------------------------------------------------------------- #
# Owner / deadline slots
# --------------------------------------------------------------------------- #

SLOT_FIELDS = ("decisions", "pending_items")


def slot_report(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Owner/deadline behaviour in both directions, plus the conditional rule test.

    The ``MISSING_DEADLINE`` review rule currently fires whenever a deadline is
    absent. That is only a defect if the meeting actually contained one. This
    report measures how often an empty deadline was in fact the correct answer,
    which is the false-positive rate of the unconditional rule.
    """
    owner_states: Counter[str] = Counter()
    deadline_states: Counter[str] = Counter()
    per_side: dict[str, dict[str, Counter[str]]] = {"baseline": {}, "revised": {}}
    conditional = Counter()
    per_case: list[dict[str, Any]] = []
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for case in cases:
        transcript = case.get("transcript") or ""
        index = g.build_transcript_index(transcript)
        transcript_dates = g.extract_date_atoms(transcript)
        meeting_has_date = bool(transcript_dates)
        gold = case.get("gold") or {}
        gold_deadlines = [
            fact for fact in (gold.get("facts") or []) if fact.get("category") == "deadline"
        ]
        case_conditional = Counter()

        sides = {"baseline": case.get("baseline_output"), "revised": case.get("revised_output")}
        for side_name, output in sides.items():
            side_counter = per_side.setdefault(side_name, {})
            for field in SLOT_FIELDS:
                items = g.iter_items(output, field)
                for position, item in enumerate(items):
                    if not isinstance(item, Mapping):
                        continue
                    owner = item.get("owner_suggestion")
                    deadline = item.get("deadline_suggestion")

                    if not isinstance(owner, str) or not owner.strip():
                        owner_state = "empty"
                    elif g._is_declared_unknown(owner):
                        owner_state = "declared_unknown"
                    else:
                        hard = [s for s in g.split_entities(owner) if s["class"] == "hard"]
                        unsupported = [s for s in hard if not g.atom_is_supported(s, index)]
                        owner_state = "fabricated" if unsupported else "grounded"
                        if unsupported and len(examples["owner_fabricated"]) < 8:
                            examples["owner_fabricated"].append({
                                "case_id": case.get("case_id"), "side": side_name, "field": field,
                                "item_index": position, "value": owner[:60],
                                "unsupported": [s["surface"] for s in unsupported][:3],
                            })
                    owner_states[owner_state] += 1
                    side_counter.setdefault("owner", Counter())[owner_state] += 1

                    state = g.deadline_state(deadline)
                    if state == "asserting":
                        atoms = g.extract_date_atoms(deadline) + g.extract_quantity_atoms(deadline)
                        unsupported = [a for a in atoms if not g.atom_is_supported(a, index)]
                        if unsupported and atoms:
                            state = "fabricated"
                            if len(examples["deadline_fabricated"]) < 8:
                                examples["deadline_fabricated"].append({
                                    "case_id": case.get("case_id"), "side": side_name, "field": field,
                                    "item_index": position, "value": deadline[:60],
                                    "unsupported": [a["surface"] for a in unsupported][:3],
                                })
                        elif atoms:
                            state = "grounded"
                    elif state == "field_misuse" and len(examples["deadline_field_misuse"]) < 8:
                        # A commitment was written into the time slot: a defect,
                        # not a classification gap.
                        examples["deadline_field_misuse"].append({
                            "case_id": case.get("case_id"), "side": side_name, "field": field,
                            "item_index": position, "value": deadline[:70],
                        })
                    deadline_states[state] += 1
                    side_counter.setdefault("deadline", Counter())[state] += 1

                    if side_name == "revised" and state in {"empty", "declared_unknown"}:
                        # Scope the question to the transcript region this item was
                        # derived from. A meeting-wide test is too coarse: almost
                        # every meeting mentions some date somewhere, so an
                        # item-scoped window is what makes the check informative.
                        located = g.find_source_window(g.item_text(field, item), transcript)
                        if located is None:
                            bucket = "window_not_located"
                        elif g.window_contains_date(located["window"]):
                            bucket = "deadline_in_source_window"
                        else:
                            bucket = "no_deadline_in_source_window"
                        case_conditional[bucket] += 1
                        conditional[bucket] += 1

        per_case.append({
            "case_id": case.get("case_id"),
            "meeting_has_explicit_date": meeting_has_date,
            "gold_deadline_fact_count": len(gold_deadlines),
            "revised": dict(case_conditional),
        })

    correctly_empty = conditional.get("no_deadline_in_source_window", 0)
    flagged_empty = conditional.get("deadline_in_source_window", 0)
    return {
        "owner_states": dict(owner_states),
        "deadline_states": dict(deadline_states),
        "per_side": per_side,
        "conditional_deadline": {
            "empty_deadline_bucket_counts": dict(conditional),
            "correctly_empty_count": correctly_empty,
            "unconditional_MISSING_DEADLINE_false_positive_rate": stats.proportion_report(
                correctly_empty, correctly_empty + flagged_empty
            ),
            "note": (
                "Counts revised-side items whose deadline was empty or explicitly unknown, then "
                "locates the transcript region the item came from and asks whether that region "
                "asserts any date. 'no_deadline_in_source_window' means an empty deadline was the "
                "correct answer, so an unconditional MISSING_DEADLINE rule raises a false positive "
                "there. 'deadline_in_source_window' is an upper bound on true misses: the region "
                "mentions a date, not necessarily one governing this exact item. "
                "'window_not_located' means no six-character substring of the item appears in the "
                "transcript, so the question cannot be answered lexically; these are excluded from "
                "the rate rather than guessed."
            ),
        },
        "per_case": per_case,
        "examples_for_audit": {k: v for k, v in examples.items()},
        "interpretation": (
            "Fabrication counts are lexical: an owner or deadline is counted only when a concrete "
            "assertion has no textual trace in the transcript. Explicit unknowns and vague periods "
            "are excluded. Cross-check the examples before quoting any rate."
        ),
    }


# --------------------------------------------------------------------------- #
# Schema separation
# --------------------------------------------------------------------------- #

def _validate_contract(output: Any) -> tuple[bool, list[str]]:
    """Structural restatement of the product's six-field contract."""
    errors: list[str] = []
    if not isinstance(output, Mapping):
        return False, ["root:not_object"]
    for field, spec in CONTRACT.items():
        if field not in output:
            errors.append(f"{field}:missing")
            continue
        value = output[field]
        if spec["kind"] == "str":
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{field}:not_nonempty_string")
            continue
        if not isinstance(value, list):
            errors.append(f"{field}:not_array")
            continue
        for position, item in enumerate(value):
            if not isinstance(item, Mapping):
                errors.append(f"{field}/{position}:not_object")
                continue
            for key, key_type in spec.get("item_keys", {}).items():
                if key not in item:
                    errors.append(f"{field}/{position}/{key}:missing")
                elif not isinstance(item[key], key_type):
                    errors.append(f"{field}/{position}/{key}:wrong_type")
            for key, allowed in spec.get("enum", {}).items():
                if key in item and item[key] not in allowed:
                    errors.append(f"{field}/{position}/{key}:invalid_enum")
    # Unknown top-level keys are a contract violation too.
    for key in output:
        if key not in CONTRACT:
            errors.append(f"{key}:unexpected_field")
    return not errors, errors


def schema_report(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Schema validity separated from content correctness.

    A schema-valid document can still be wrong, and reporting only validity
    rewards a model that emits well-typed nonsense. The separation here follows
    the naming used by the structured-output literature.
    """
    per_field_failures: Counter[str] = Counter()
    first_failing_path: Counter[str] = Counter()
    sides: dict[str, Counter[str]] = {"baseline": Counter(), "revised": Counter()}
    per_case: list[dict[str, Any]] = []

    for case in cases:
        transcript = case.get("transcript") or ""
        row: dict[str, Any] = {"case_id": case.get("case_id")}
        for side_name in ("baseline", "revised"):
            output = case.get(f"{side_name}_output")
            valid, errors = _validate_contract(output)
            counter = sides[side_name]
            counter["evaluated"] += 1
            counter["schema_valid"] += int(valid)
            if not valid:
                counter["schema_invalid"] += 1
                first_failing_path[errors[0].split(":")[0]] += 1
                for error in errors:
                    per_field_failures[error.split(":")[0].split("/")[0]] += 1
            analysis = _analyze_side(output, transcript) if isinstance(output, Mapping) else None
            flagged = analysis["flagged_item_count"] if analysis else 0
            counter["items_flagged"] += flagged
            if valid and flagged:
                counter["wrong_valid_schema"] += 1
            row[f"{side_name}_schema_valid"] = valid
            row[f"{side_name}_flagged_items"] = flagged
        per_case.append(row)

    def summarize(side_name: str) -> dict[str, Any]:
        counter = sides[side_name]
        evaluated = counter["evaluated"] or 0
        valid = counter["schema_valid"]
        result = {
            "evaluated_cases": evaluated,
            "schema_validity_rate": stats.proportion_report(valid, evaluated),
            "wrong_valid_schema_cases": counter["wrong_valid_schema"],
            "wrong_valid_schema_rate_among_valid": stats.proportion_report(
                counter["wrong_valid_schema"], valid
            ),
            "items_flagged": counter["items_flagged"],
        }
        if evaluated:
            result["effective_yield_components"] = {
                "schema_validity_rate": valid / evaluated,
                "items_flagged": counter["items_flagged"],
                "note": (
                    "effective yield = schema validity rate x (1 - flagged item rate); the item "
                    "denominator comes from the transition report's item counts"
                ),
            }
        return result

    return {
        "contract_source": "structural restatement of the product's MinutesResult schema",
        "baseline": summarize("baseline"),
        "revised": summarize("revised"),
        "per_field_failure_count": dict(per_field_failures),
        "first_failing_path_histogram": dict(first_failing_path),
        "per_case": per_case,
        "interpretation": (
            "wrong_valid_schema counts documents that satisfy every structural rule while still "
            "containing a lexically unsupported assertion. It is the closest offline analogue of "
            "'valid JSON, wrong content' and is a proxy, not a factuality judgement."
        ),
    }
