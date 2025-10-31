"""CLI driver for the offline metric suite.

Usage (no model calls, safe to run at any time)::

    python evaluation/metrics/run_metrics.py --set all

Reads the stored reports under ``evaluation/results/`` and
``evaluation/benchmarks/results/``, rebuilds the VCSum transcripts from the
pinned repository files, and writes ``metrics.json`` plus ``METRICS_REPORT.md``.

This does not run any evaluation, does not touch the database, and does not
consume the LLM budget. It only recomputes metrics over results that earlier,
explicitly authorised runs already produced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in (None, ""):  # allow direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from judges import actions as amc_aid  # type: ignore
    from lib import corpora, stats  # type: ignore
    from regression import analysis, contract_checks, grounding, reliability  # type: ignore
else:
    from judges import actions as amc_aid
    from lib import corpora, stats
    from regression import analysis, contract_checks, grounding, reliability

# 兼容再导出：历史代码与测试通过本模块访问装载器
PUBLIC_RESULTS = corpora.PUBLIC_RESULTS
VCSUM_RUNS = corpora.VCSUM_RUNS
NEW_CONTRACT_TEST26_RUNS = corpora.NEW_CONTRACT_TEST26_RUNS
VCSUM_ALL_DEV_MANIFEST = corpora.VCSUM_ALL_DEV_MANIFEST
load_vcsum_cases = corpora.load_vcsum_cases
load_new_contract_cases = corpora.load_new_contract_cases

EVALUATION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = EVALUATION_DIR / "results"

def summarize_grounding(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Draft-versus-revised grounding rates with exact intervals on the meetings."""
    rows: list[dict[str, Any]] = []
    before_atoms: list[float] = []
    after_atoms: list[float] = []
    before_items: list[float] = []
    after_items: list[float] = []
    per_field: dict[str, dict[str, Any]] = {}
    # A per-kind breakdown is essential: the entity class depends on a
    # lexicographic generic-word list and moves when that list changes, while
    # the date and quantity classes do not. Reporting only the pooled rate
    # hides which half of the number is stable.
    by_kind: dict[str, dict[str, int]] = {}

    for case in cases:
        transcript = case.get("transcript") or ""
        base = grounding.analyze_output(case.get("baseline_output"), transcript)
        rev = grounding.analyze_output(case.get("revised_output"), transcript)
        row = {
            "case_id": case.get("case_id"),
            "baseline": {
                "checkable_atoms": base["checkable_atom_count"],
                "unsupported_atoms": base["unsupported_atom_count"],
                "items": base["item_count"],
                "flagged_items": base["flagged_item_count"],
                "declared_unknown": base["declared_unknown_count"],
                "vague_time": base["vague_time_count"],
                "uncovered_candidates": (base.get("action_candidate_coverage") or {}).get("uncovered_count"),
                "invented_spans": base.get("invented_span_count"),
                "actionable_rate": base.get("actionable_rate"),
                "risk_levels": base.get("risk_level_distribution"),
                "summary_decision_coverage": (base.get("summary_decision_coverage") or {}).get("coverage_rate"),
            },
            "revised": {
                "checkable_atoms": rev["checkable_atom_count"],
                "unsupported_atoms": rev["unsupported_atom_count"],
                "items": rev["item_count"],
                "flagged_items": rev["flagged_item_count"],
                "declared_unknown": rev["declared_unknown_count"],
                "vague_time": rev["vague_time_count"],
                "uncovered_candidates": (rev.get("action_candidate_coverage") or {}).get("uncovered_count"),
                "invented_spans": rev.get("invented_span_count"),
                "actionable_rate": rev.get("actionable_rate"),
                "risk_levels": rev.get("risk_level_distribution"),
                "summary_decision_coverage": (rev.get("summary_decision_coverage") or {}).get("coverage_rate"),
            },
        }
        rows.append(row)
        before_atoms.append(row["baseline"]["unsupported_atoms"])
        after_atoms.append(row["revised"]["unsupported_atoms"])
        before_items.append(row["baseline"]["flagged_items"])
        after_items.append(row["revised"]["flagged_items"])

        for side_name, side_analysis in (("baseline", base), ("revised", rev)):
            for results in side_analysis["per_field"].values():
                for result in results:
                    for atom in result["atoms"]:
                        if atom.get("class") == "descriptive":
                            continue
                        bucket = by_kind.setdefault(atom["kind"], {
                            "baseline_checkable": 0, "baseline_unsupported": 0,
                            "revised_checkable": 0, "revised_unsupported": 0,
                        })
                        bucket[f"{side_name}_checkable"] += 1
                        if not atom.get("supported", True):
                            bucket[f"{side_name}_unsupported"] += 1

        for field in analysis.FIELDS:
            bucket = per_field.setdefault(field, {
                "baseline_checkable": 0, "baseline_unsupported": 0,
                "revised_checkable": 0, "revised_unsupported": 0,
            })
            for side, field_analysis in (("baseline", base), ("revised", rev)):
                for result in field_analysis["per_field"].get(field, []):
                    bucket[f"{side}_checkable"] += result["checkable_count"]
                    bucket[f"{side}_unsupported"] += len(result["unsupported_atoms"])

    for kind, bucket in by_kind.items():
        bucket["baseline_rate"] = (
            bucket["baseline_unsupported"] / bucket["baseline_checkable"]
            if bucket["baseline_checkable"] else None
        )
        bucket["revised_rate"] = (
            bucket["revised_unsupported"] / bucket["revised_checkable"]
            if bucket["revised_checkable"] else None
        )

    total_atoms_before = sum(int(r["baseline"]["checkable_atoms"]) for r in rows)
    total_atoms_after = sum(int(r["revised"]["checkable_atoms"]) for r in rows)
    unsupported_before = sum(int(r["baseline"]["unsupported_atoms"]) for r in rows)
    unsupported_after = sum(int(r["revised"]["unsupported_atoms"]) for r in rows)

    # Paired statistics are the primary reading: the pooled atom rate needs about
    # 170 meetings to resolve the effect this data shows, while the paired
    # per-meeting comparison resolves it at 26. Reporting order follows that.
    def _paired(key):
        return [
            int(r["revised"].get(key) or 0) - int(r["baseline"].get(key) or 0)
            for r in rows
        ]

    paired_block = {
        "unsupported_atom_count_difference": stats.wilcoxon_signed_rank(
            _paired("unsupported_atoms")
        ),
        "flagged_item_count_difference": stats.wilcoxon_signed_rank(_paired("flagged_items")),
        "uncovered_action_candidate_difference": stats.wilcoxon_signed_rank(
            _paired("uncovered_candidates")
        ),
        "reading": (
            "Primary statistic. Paired within meeting, so between-meeting heterogeneity cancels; "
            "the pooled atom rate below is secondary because it needs far more meetings to resolve "
            "an effect of the size observed here."
        ),
    }
    power_note = (
        "Detecting a 2.2 percentage-point change in the pooled unsupported-atom rate at 80% power "
        "needs roughly 1,900 checkable atoms per arm; this data yields about 11 atoms per meeting, "
        "so the pooled rate would need on the order of 170 meetings. Prefer the paired statistic "
        "and raise atoms per meeting."
    )

    risk_totals: dict[str, dict[str, int]] = {}
    for side in ("baseline", "revised"):
        counts: dict[str, int] = {}
        for row in rows:
            for level, value in (row[side].get("risk_levels") or {}).items():
                counts[level] = counts.get(level, 0) + int(value)
        risk_totals[side] = counts

    coverage_means: dict[str, float | None] = {}
    for side in ("baseline", "revised"):
        values = [row[side]["summary_decision_coverage"] for row in rows
                  if row[side].get("summary_decision_coverage") is not None]
        coverage_means[side] = sum(values) / len(values) if values else None


    meetings_improved = sum(1 for a, b in zip(after_atoms, before_atoms) if a < b)
    meetings_worsened = sum(1 for a, b in zip(after_atoms, before_atoms) if a > b)

    # Length sensitivity: does the lexical flag rate rise with transcript length?
    # This is the free test of the truncation hypothesis. It cannot establish
    # causation -- longer meetings also differ in content -- so it is reported as
    # a correlation with an explicit caveat, not as evidence.
    lengths: list[float] = []
    baseline_rates: list[float] = []
    revised_rates: list[float] = []
    for case, row in zip(cases, rows):
        length = float(len(case.get("transcript") or ""))
        base_checkable = int(row["baseline"]["checkable_atoms"])
        rev_checkable = int(row["revised"]["checkable_atoms"])
        lengths.append(length)
        baseline_rates.append(
            row["baseline"]["unsupported_atoms"] / base_checkable if base_checkable else 0.0
        )
        revised_rates.append(
            row["revised"]["unsupported_atoms"] / rev_checkable if rev_checkable else 0.0
        )

    return {
        "case_count": len(rows),
        "primary_paired": paired_block,
        "power_note": power_note,
        "risk_level_distribution": risk_totals,
        "summary_decision_coverage_mean": coverage_means,
        "atom_level": {
            "baseline_unsupported_atom_rate": stats.proportion_report(unsupported_before, total_atoms_before),
            "revised_unsupported_atom_rate": stats.proportion_report(unsupported_after, total_atoms_after),
            "atom_denominator_note": (
                "the denominator is the number of checkable atoms (dates, quantities, named "
                "entities) the generator actually asserted, so the two rates are not over a fixed set"
            ),
        },
        "length_sensitivity": {
            "spearman_length_vs_baseline_flag_rate": stats.spearman_rho(lengths, baseline_rates),
            "spearman_length_vs_revised_flag_rate": stats.spearman_rho(lengths, revised_rates),
            "transcript_length_chars": stats.describe(lengths),
            "caveat": (
                "Longer meetings also differ in topic and speaker count, so a positive correlation "
                "is consistent with a truncation effect but does not identify it. Identifying it "
                "requires holding the meeting fixed and varying only the review budget."
            ),
        },
        "item_level": {
            "baseline_flagged_items": sum(int(r["baseline"]["flagged_items"]) for r in rows),
            "revised_flagged_items": sum(int(r["revised"]["flagged_items"]) for r in rows),
        },
        "meeting_level": {
            "improved": meetings_improved,
            "worsened": meetings_worsened,
            "unchanged": len(rows) - meetings_improved - meetings_worsened,
            "sign_test": stats.mcnemar_exact(meetings_worsened, meetings_improved),
            "paired_bootstrap_flagged_items": stats.paired_bootstrap_mean_ci(
                [a - b for a, b in zip(after_items, before_items)]
            ),
        },
        "per_field": per_field,
        "by_atom_kind": by_kind,
        "per_case": rows,
    }


def build_report(
    sets: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    contract_versions: Mapping[str, str] | None = None,
    results_root: Path | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "suite": "aimeeting-offline-metrics-v1",
        "model_calls_made_by_this_suite": 0,
        "proxy_status": grounding.PROXY_STATUS,
        "sets": {},
    }
    versions = dict(contract_versions or {})
    # The public Chinese action-item anchor. Scoring it costs nothing, so the
    # scorer and its trivial floors are validated on every run; an authorised
    # paid run can then immediately produce a comparable number.
    amc: dict[str, Any] = {}
    for split in ("dev", "test1"):
        try:
            amc[split] = {
                "self_check": amc_aid.self_check(split),
                "corpus": amc_aid.corpus_stats(split),
            }
        except (FileNotFoundError, ValueError) as error:
            amc[split] = {"unavailable": str(error)}
    report["amc_aid"] = amc
    # 引用级（ALCE 式）：读已存档的判定产物，**零模型调用**。
    # 它是一次性抽样实验的产物，因此只做"报告 + 回归护栏"：值缺失就显式标注不可用，
    # 而不是静默当作 0（本项目反复踩过的"无声的零"）。
    citation_path = EVALUATION_DIR / "metrics" / "results" / "citation_packet.json"
    if citation_path.exists():
        try:
            stored = json.loads(citation_path.read_text(encoding="utf-8"))
            report["citation_level"] = {
                "source": str(citation_path.relative_to(EVALUATION_DIR)),
                "summary": stored.get("summary"),
                "alce_strict": stored.get("alce_strict"),
                "adjudicator_disclosure": stored.get("adjudicator_disclosure"),
            }
        except (OSError, ValueError) as error:
            report["citation_level"] = {"unavailable": f"{type(error).__name__}: {error}"}
    else:
        report["citation_level"] = {
            "unavailable": "未找到引用级判定产物；先跑 citation_metrics.py --build/--judge"
        }
    # 可靠性与开销：纯计数，零阈值。与逐数据集的质量指标并列，因为它回答的是另一个
    # 问题——「这套链路跑得稳不稳、贵不贵」，而它与会议内容无关。
    if results_root is not None:
        report["reliability"] = reliability.build_report(results_root)
    for name, cases in sets.items():
        if not cases:
            continue
        report["sets"][name] = {
            "case_count": len(cases),
            "contract_invariants": contract_checks.build_report(
                cases, data_contract=versions.get(name, contract_checks.LEGACY)
            ),
            "grounding": summarize_grounding(cases),
            "transitions": analysis.transition_report(cases),
            "critique": analysis.critique_report(cases),
            "slots": analysis.slot_report(cases),
            "schema": analysis.schema_report(cases),
        }
    return report


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #

def _pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{100 * float(value):.{digits}f}%"


def _num(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def _interval(block: Mapping[str, Any]) -> str:
    cp = block.get("clopper_pearson_95")
    if not cp:
        return "n/a"
    return f"[{_pct(cp[0])}, {_pct(cp[1])}]"


def _render_item_and_evidence(block: Mapping[str, Any]) -> list[str]:
    """条目/证据/槽位三个计数的渲染。缺数时不渲染，而不是渲染 0——0 与「没测」不同。"""
    items = block.get("items") or {}
    by_version = items.get("by_version") or {}
    combined = items.get("combined") or {}
    loc = (block.get("locatability") or {}).get("combined") or {}
    scopes = (block.get("locatability") or {}).get("by_scope") or {}
    if not by_version and not loc:
        return []
    lines = ["", "**条目、证据与槽位（新契约下「模型是否真按契约产出」的直接读数）**", ""]
    if by_version:
        lines += [
            "| 版本 | 条目 | 带证据 | 证据覆盖率 | 类型化槽位 | 槽位 basis 分布 |",
            "|---|---:|---:|---|---:|---|",
        ]
        for label in ("baseline", "revised"):
            bucket = by_version.get(label) or {}
            basis = "、".join(f"{k} {v}" for k, v in (bucket.get("basis_breakdown") or {}).items()) or "无"
            lines.append(
                f"| {label} | {bucket.get('items', 0)} | {bucket.get('items_with_evidence', 0)} | "
                f"{_pct((bucket.get('evidence_coverage') or {}).get('rate'))} | "
                f"{bucket.get('typed_slots', 0)} | {basis} |"
            )
        if combined:
            basis = "、".join(f"{k} {v}" for k, v in (combined.get("basis_breakdown") or {}).items()) or "无"
            lines.append(
                f"| **合计** | **{combined.get('items', 0)}** | "
                f"**{combined.get('items_with_evidence', 0)}** | "
                f"**{_pct((combined.get('evidence_coverage') or {}).get('rate'))}** | "
                f"**{combined.get('typed_slots', 0)}** | {basis} |"
            )
    if loc:
        lines += [
            "",
            f"证据可定位（两个范围合计）：{loc.get('locatable', 0)}/{loc.get('evidence', 0)} "
            f"（{_pct((loc.get('locatable_rate') or {}).get('rate'), 2)}，"
            f"95% 精确区间 {_interval(loc.get('locatable_rate') or {})}）。",
        ]
        for scope, title in (
            ("item_evidence", "条目级证据（对外引用口径）"),
            ("slot_evidence", "类型化槽位自己的证据（单列，不并入上者）"),
        ):
            entry = (scopes.get(scope) or {}).get("combined") or {}
            if not entry:
                continue
            rate = entry.get("locatable_rate") or {}
            lines.append(
                f"* {title}：{entry.get('locatable', 0)}/{entry.get('evidence', 0)}"
                f"（{_pct(rate.get('rate'), 2)}，区间 {_interval(rate)}）。"
            )
        lines += [
            "",
            f"判据：{block.get('locatability', {}).get('criterion', '')}",
            f"口径：{block.get('locatability', {}).get('scope_note', '')}",
        ]
        breakdown = block.get("locatability", {}).get("unlocated_breakdown") or {}
        if breakdown:
            rendered = "、".join(f"{k} {v}" for k, v in sorted(breakdown.items()))
            lines += [
                "",
                f"未定位的诊断拆分：{rendered}。"
                "`ellipsis_elided` 是引文内含省略号（模型把两段拼接），`verbatim_mismatch` "
                "是不含省略号却找不到——**后者才是需要人工看的候选**。该拆分不参与任何阈值。",
            ]
        examples = (block.get("locatability") or {}).get("unlocated_examples_for_audit") or []
        if examples:
            lines += [
                "",
                f"不可定位的样例（{len(examples)} 条，按每场最多 2 条分层抽取）：",
                "",
                "| 会议 | 版本 | 范围 | 字段 | 形态 | 引用 |",
                "|---|---|---|---|---|---|",
            ]
            for example in examples:
                quote = str(example.get("quote") or "").replace("|", "\\|")
                lines.append(
                    f"| {example.get('case_id', '')} | {example.get('version', '')} | "
                    f"{example.get('scope', '')} | {example.get('field', '')} | "
                    f"{example.get('kind', '')} | {quote} |"
                )
    return lines


def _render_flagged_values(inv: Mapping[str, Any], kind: str, *, limit: int = 24) -> list[str]:
    """把某类违规的**原值**列出来。计数会让两类不同的东西合成一个数，原值是唯一凭据。"""
    rows: list[tuple[str, str, str]] = []
    for case in inv.get("per_case") or []:
        for label in ("baseline", "revised"):
            examples = (case.get(f"{label}_examples") or {}).get(kind) or []
            for example in examples:
                rows.append((str(case.get("case_id")), label, str(example.get("value") or "")))
    if not rows:
        return []
    lines = [
        "",
        f"**{kind} 的原值（{len(rows)} 条，最多列 {limit} 条）**——计数会掩盖"
        "「同一个数里混着两类东西」，原值是唯一能一眼分辨的凭据：",
        "",
        "| 会议 | 版本 | 值 |",
        "|---|---|---|",
    ]
    for case_id, label, value in rows[:limit]:
        lines.append(f"| {case_id} | {label} | {value.replace('|', chr(92) + '|')} |")
    return lines


def _render_citation_level(block: Mapping[str, Any]) -> list[str]:
    """引用级（ALCE 式）：引用是否**支撑**断言，而不只是能否定位。"""
    if not block:
        return []
    if block.get("unavailable"):
        return ["## 引用级评测（ALCE 式）", "",
                f"不可用：{block['unavailable']}。**这不是「没有无依据内容」，是「没有测量」。**", ""]
    lines = ["## 引用级评测（ALCE 式，零调用复算自存档判定）", ""]
    summary = block.get("summary") or {}
    cp = summary.get("citation_precision") or {}
    sr = summary.get("item_support_rate") or {}
    strict = block.get("alce_strict") or {}
    lines += [
        f"判定条目 {summary.get('items_judged')} 条，引用 {cp.get('total')} 条。",
        "",
        "| 口径 | 值 | 95% 精确区间 |",
        "|---|---|---|",
        f"| 引用级支撑率（citation precision） | {_pct(cp.get('rate'), 1)} | {_interval(cp)} |",
        f"| 条目级支撑率（任一条引用支撑） | {_pct(sr.get('rate'), 1)} | {_interval(sr)} |",
        f"| **ALCE 严格版**（无多余引用） | {_pct(strict.get('strict_rate'), 1)} | "
        f"[{_pct((strict.get('strict_95') or [0])[0], 1)}, {_pct((strict.get('strict_95') or [0, 1])[1], 1)}] |",
        f"| 多余引用率（ALCE 的 shotgun citation） | {_pct(summary.get('unnecessary_citation_rate'), 1)} | — |",
        "",
        (summary.get("reading") or ""),
        "",
        f"判定方：{(block.get('adjudicator_disclosure') or {}).get('model_used', '未知')}"
        f"（{(block.get('adjudicator_disclosure') or {}).get('model_used') and '独立族' or ''}）。",
        "",
    ]
    return lines


def _render_reliability(block: Mapping[str, Any]) -> list[str]:
    """可靠性与开销。纯计数口径，与逐数据集的质量指标分开呈现。"""
    if not block:
        return []
    end_to_end = block.get("end_to_end") or {}
    overall = (block.get("call_success") or {}).get("overall") or {}
    lines = [
        "## 可靠性与开销（纯计数：无阈值、无词表）",
        "",
        f"扫描运行目录 {block.get('run_directories_scanned', 0)} 个，"
        f"派发调用 {block.get('dispatched_call_count', 0)} 次。",
        "",
        "| 口径 | 成功 | 分母 | 成功率 | 95% 精确区间 |",
        "|---|---:|---:|---|---|",
        f"| 端到端（逐场尝试） | {end_to_end.get('completed_case_runs', 0)} | "
        f"{end_to_end.get('attempted_case_runs', 0)} | "
        f"{_pct((end_to_end.get('success_rate') or {}).get('rate'))} | "
        f"{_interval(end_to_end.get('success_rate') or {})} |",
        f"| 调用级 | {overall.get('response_received', 0)} | {overall.get('dispatched', 0)} | "
        f"{_pct((overall.get('success_rate') or {}).get('rate'))} | "
        f"{_interval(overall.get('success_rate') or {})} |",
        "",
        "**分母是每一次尝试，不是最终成功的场次**——把多轮修复后的“最终全通过”"
        "报成成功率会掩盖过程中的真实失败。",
        "",
    ]
    by_type = (block.get("call_success") or {}).get("by_call_type") or {}
    if by_type:
        lines += [
            "| 调用类型 | 派发 | 成功 | 成功率 | 中位延迟 | 最大延迟 |",
            "|---|---:|---:|---|---:|---:|",
        ]
        latency = (block.get("latency_ms") or {}).get("by_call_type") or {}
        for name, entry in by_type.items():
            stats_block = latency.get(name) or {}
            median = stats_block.get("median")
            maximum = stats_block.get("max")
            lines.append(
                f"| {name} | {entry.get('dispatched', 0)} | {entry.get('response_received', 0)} | "
                f"{_pct((entry.get('success_rate') or {}).get('rate'))} | "
                f"{'n/a' if median is None else f'{median:,.0f} ms'} | "
                f"{'n/a' if maximum is None else f'{maximum:,.0f} ms'} |"
            )
        lines.append("")
    failures = (block.get("failure_modes") or {}).get("by_bucket") or {}
    if failures:
        lines += [
            f"失败类型分布：`{json.dumps(failures, ensure_ascii=False)}`"
            "（按已记录的错误文本归档，识别不到的进 other 并保留原文）。",
            "",
        ]
    unparsed = block.get("ledgers_without_recognizable_calls") or []
    if unparsed:
        lines += [
            f"**账本结构无法识别的运行目录**：{unparsed}。"
            "这会让上面的计数偏小，因此显式列出而不是安静地少算。",
            "",
        ]
    tokens = (block.get("tokens") or {}).get("total") or {}
    if tokens:
        lines += [
            f"token 合计（供应商返回的用量，不是账单金额）：`{json.dumps(tokens, ensure_ascii=False)}`。"
            "该供应商的推理 token 计入输出，因此只报输出会低估开销。",
            "",
        ]
    lines += [f"口径：{block.get('design_note', '')}", ""]
    return lines


def format_markdown(report: Mapping[str, Any]) -> str:
    lines: list[str] = [
        "# AIMeeting 离线指标复算报告",
        "",
        "本报告由 `evaluation/metrics/run_metrics.py` 生成，**未调用任何模型**，"
        "只对既有评测产物做离线复算。所有比例同时给出点估计与 95% 精确区间"
        "（Clopper–Pearson）；比例接近 0 或 1 且样本量小时，正态近似与 bootstrap 都不可靠。",
        "",
        "口径声明：这里的 grounding 是**词法代理**，只检查日期、数量、具名实体三类高精度断言"
        "在转写中是否有字面痕迹，并刻意偏向“有支撑”。它看不到改写、蕴含与正确推导，"
        "**不能称为幻觉率或事实准确率**；所有被标记的原文都列在 JSON 的 `examples_for_audit` 中供人工复核。",
        "",
    ]
    unavailable = report.get("unavailable_sets") or {}
    if unavailable:
        lines += [
            "## ⚠️ 本次有数据集不可用（不是「没有缺陷」，是「没有测量」）",
            "",
            "| 数据集 | 原因 |",
            "|---|---|",
        ]
        for name, reason in unavailable.items():
            lines.append(f"| {name} | {reason} |")
        lines += [
            "",
            "**不要把缺失读成零**：这些数据集的质量数字在本报告中**不存在**。",
            "",
        ]
    for name, block in report["sets"].items():
        lines.append(f"## 数据集：{name}（{block['case_count']} 场）")
        lines.append("")

        grounding_block = block["grounding"]
        primary = grounding_block.get("primary_paired") or {}
        if primary:
            lines += [
                "### 0. 首要口径：逐会议配对（Wilcoxon 符号秩）",
                "",
                "| 配对量 | 非零对 | W+ | W− | p（双侧） |",
                "|---|---:|---:|---:|---:|",
            ]
            for label, key in (
                ("无依据原子数", "unsupported_atom_count_difference"),
                ("被标记条目数", "flagged_item_count_difference"),
                ("未覆盖行动候选数", "uncovered_action_candidate_difference"),
            ):
                entry = primary.get(key) or {}
                p_value = entry.get("p_value_two_sided")
                lines.append(
                    f"| {label} | {entry.get('n_nonzero', 0)} | "
                    f"{_num(entry.get('w_plus'))} | {_num(entry.get('w_minus'))} | "
                    f"{'n/a' if p_value is None else f'{p_value:.4f}'} |"
                )
            lines += ["", primary.get("reading", ""), "",
                      f"**功效提示**：{grounding_block.get('power_note', '')}", ""]

        atom = grounding_block["atom_level"]
        before = atom["baseline_unsupported_atom_rate"]
        after = atom["revised_unsupported_atom_rate"]
        meeting = grounding_block["meeting_level"]
        lines += [
            "### 1. 无依据率（逐字段词法代理）",
            "",
            "| 版本 | 无依据原子 | 可核查原子 | 无依据率 | 95% 精确区间 |",
            "|---|---:|---:|---:|---:|",
            f"| 初稿 | {before['successes']} | {before['total']} | {_pct(before['rate'], 2)} | {_interval(before)} |",
            f"| 自检后 | {after['successes']} | {after['total']} | {_pct(after['rate'], 2)} | {_interval(after)} |",
            "",
            f"逐会议改善 {meeting['improved']} 场、退化 {meeting['worsened']} 场、"
            f"持平 {meeting['unchanged']} 场；符号检验 p = "
            f"{(meeting['sign_test'].get('p_value_two_sided') or float('nan')):.4f}"
            "（样本量不足以支持强结论）。",
            "",
        ]
        bootstrap = meeting.get("paired_bootstrap_flagged_items")
        if bootstrap:
            lines.append(
                f"被标记条目数的配对差均值 {bootstrap['mean_difference']:.3f}，"
                f"bootstrap 95% 区间 [{bootstrap['percentile_95'][0]:.3f}, "
                f"{bootstrap['percentile_95'][1]:.3f}]（次要口径）。"
            )
            lines.append("")

        pf = grounding_block["per_field"]
        if pf:
            lines += [
                "逐字段无依据原子：",
                "",
                "| 字段 | 初稿可核查 | 初稿无依据 | 自检后可核查 | 自检后无依据 |",
                "|---|---:|---:|---:|---:|",
            ]
            for field, bucket in pf.items():
                lines.append(
                    f"| {field} | {bucket['baseline_checkable']} | {bucket['baseline_unsupported']} | "
                    f"{bucket['revised_checkable']} | {bucket['revised_unsupported']} |"
                )
            lines.append("")

        risk = grounding_block.get("risk_level_distribution") or {}
        if risk:
            lines += [
                "风险等级分布（常量即校准缺陷）与摘要对决策的覆盖（内部一致性，无需转写）：",
                "",
                f"风险等级：`{json.dumps(risk, ensure_ascii=False)}`；"
                f"摘要决策覆盖率均值：初稿 {_num((grounding_block.get('summary_decision_coverage_mean') or {}).get('baseline'))}、"
                f"自检后 {_num((grounding_block.get('summary_decision_coverage_mean') or {}).get('revised'))}。",
                "",
            ]

        by_kind = grounding_block.get("by_atom_kind") or {}
        if by_kind:
            lines += [
                "按原子类型分解（**重要**：entity 类依赖通用词表，会随词表调整而变动；"
                "date 与 quantity 类不依赖词表，是稳定口径）：",
                "",
                "| 原子类型 | 初稿可核查 | 初稿无依据 | 初稿率 | 自检后可核查 | 自检后无依据 | 自检后率 |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
            for kind in sorted(by_kind):
                bucket = by_kind[kind]
                lines.append(
                    f"| {kind} | {bucket['baseline_checkable']} | {bucket['baseline_unsupported']} | "
                    f"{_pct(bucket['baseline_rate'], 2)} | {bucket['revised_checkable']} | "
                    f"{bucket['revised_unsupported']} | {_pct(bucket['revised_rate'], 2)} |"
                )
            lines.append("")

        inv = block.get("contract_invariants") or {}
        legacy = inv.get("data_contract", contract_checks.LEGACY) == contract_checks.LEGACY
        lines += [
            f"### 1b. 契约不变量（结构性判定，非启发式；产物契约：{inv.get('data_contract', 'unknown')}）",
            "",
        ]
        if not inv.get("available"):
            lines += [f"不可用：{inv.get('reason', '')}", ""]
        else:
            quoted = inv.get("quoted_counts") or {}
            headline = (
                "**可在旧产物上引用的量**（判据是定义性的，不存在假阳性）："
                if legacy else
                "**可引用的量**（产物来自契约 v2：模型被要求给出 basis 与 evidence，"
                "因此证据类不变量在这里是真实缺陷率，不是假象）："
            )
            lines += [headline, "", "| 版本 | 不变量计数 |", "|---|---|"]
            for label in ("baseline", "revised", "draft_to_revised"):
                counts = quoted.get(label) or {}
                rendered = "、".join(f"{k} {v}" for k, v in sorted(counts.items())) or "无"
                lines.append(f"| {label} | {rendered} |")
            lines += _render_item_and_evidence(inv.get("item_and_evidence") or {})
            artifact = inv.get("legacy_artifact_counts") or {}
            artifact_total = sum(sum((v or {}).values()) for v in artifact.values())
            if artifact_total:
                lines += [
                    "",
                    f"**假象计数（共 {artifact_total} 条，禁止引用）**："
                    "证据类不变量在旧产物上必然全数失败，因为旧契约从未要求模型给出 basis/evidence。"
                    "这些量只有在新契约下真实运行后才有意义。",
                ]
            issue_check = inv.get("issue_evidence_check") or {}
            if issue_check:
                label = (
                    "审查意见证据门控（旧产物上同为假象）："
                    if legacy else
                    "审查意见证据门控（**这是真实读数**：新契约要求每条意见给出证据锚点）："
                )
                lines += ["", f"{label}`{issue_check}`。"]
            lines += _render_flagged_values(inv, "slot_type_mismatch")
            lines += ["", inv.get("reading", ""), ""]
        lines += ["### 2. 配对转移矩阵（初稿 → 自检稿）", ""]
        claim = block["transitions"]["claim_level_transitions"]
        set_level = block["transitions"]["set_level_transitions"]
        lines += [
            "| 转移 | 含义 | 计数 |",
            "|---|---|---:|",
            f"| fixed_to_supported | 配对条目由无依据变为有依据（收益） | {claim.get('fixed_to_supported', 0)} |",
            f"| introduced_unsupported | 配对条目引入无依据（**损害**） | {claim.get('introduced_unsupported', 0)} |",
            f"| kept_unsupported | 无依据内容未被修正 | {claim.get('kept_unsupported', 0)} |",
            f"| kept_supported | 有依据内容保持有依据 | {claim.get('kept_supported', 0)} |",
            f"| removed_unsupported | 删除了无依据条目（收益） | {set_level.get('removed_unsupported', 0)} |",
            f"| merged_supported | 与其他条目合并，措辞仍有留存（非删除） | {set_level.get('merged_supported', 0)} |",
            f"| removed_supported | **措辞无处留存的删除（过度修订上界）** | {set_level.get('removed_supported', 0)} |",
            f"| added_unsupported | 新增了无依据条目（**损害**） | {set_level.get('added_unsupported', 0)} |",
            f"| split_supported | 由既有条目拆分而来（非新增） | {set_level.get('split_supported', 0)} |",
            f"| added_supported | 新增了有依据的新内容 | {set_level.get('added_supported', 0)} |",
            "",
        ]
        mcnemar = block["transitions"]["claim_level_mcnemar"]
        if mcnemar.get("p_value_two_sided") is not None:
            lines.append(
                f"配对条目上的 McNemar 精确检验：不一致对 {mcnemar['n_discordant']} 组，"
                f"p = {mcnemar['p_value_two_sided']:.4f}。"
            )
        else:
            lines.append("配对条目上无不一致对，McNemar 检验在此无功效。")
        lines += [
            "",
            f"配对方式：{block['transitions']['pairing_note']}。"
            "模糊配对会让“收益/损害”都偏乐观，因此未配对条目单列而非丢弃。",
            "",
        ]

        critique = block["critique"]
        lines += [
            "### 3. 审查质量（与词法代理的一致性）",
            "",
            f"分析 {critique['rounds_analyzed']} 个审查轮次；审查通过率 "
            f"{_pct(critique['review_pass_rate'])}。",
            "",
            "| 方向 | 分子 | 分母 | 比例 | 95% 精确区间 |",
            "|---|---:|---:|---:|---:|",
            f"| 审查 precision（与代理一致） | {critique['reviewer_precision_vs_proxy']['successes']} | "
            f"{critique['reviewer_precision_vs_proxy']['total']} | "
            f"{_pct(critique['reviewer_precision_vs_proxy']['rate'])} | "
            f"{_interval(critique['reviewer_precision_vs_proxy'])} |",
            f"| 审查 recall（与代理一致） | {critique['reviewer_recall_vs_proxy']['successes']} | "
            f"{critique['reviewer_recall_vs_proxy']['total']} | "
            f"{_pct(critique['reviewer_recall_vs_proxy']['rate'])} | "
            f"{_interval(critique['reviewer_recall_vs_proxy'])} |",
            "",
            f"审查判定通过但代理仍标记出内容的轮次："
            f"{critique['passed_rounds_with_proxy_flags']} / {critique['passed_round_denominator']}。",
            "",
            f"issue 类型分布：`{json.dumps(critique['issue_type_distribution'], ensure_ascii=False)}`；"
            f"issue 指向方式：`{json.dumps(critique['issue_index_kind'], ensure_ascii=False)}`。",
            "",
            f"每轮 issue 数与代理标记数的 Spearman 秩相关："
            f"{critique['spearman_issue_count_vs_flag_count']}。{critique['correlation_note']}",
            "",
        ]

        content = critique.get("content_support_by_field")
        if content:
            lines += [
                "**内容断言支撑率**（第二路独立信号：条目自身措辞在转写中的重合比例）。"
                "AUC = 被审查标记的条目支撑率高于未标记条目的概率；< 0.5 表示被标记条目重合更低，"
                "即支持审查；≈ 0.5 表示无区分力：",
                "",
                "| 字段 | 被标记 n | 被标记均值 | 未标记 n | 未标记均值 | AUC | 用于判别 |",
                "|---|---:|---:|---:|---:|---:|---|",
            ]
            for field, entry in content["per_field"].items():
                lines.append(
                    f"| {field} | {entry['flagged_n']} | {_num(entry['flagged_mean'])} | "
                    f"{entry['clean_n']} | {_num(entry['clean_mean'])} | "
                    f"{_num(entry['auc_flagged_vs_clean'])} | "
                    f"{'★' if entry['flags_discriminated'] else '—'} |"
                )
            pooled = content["pooled_auc_flagged_vs_clean"]
            lines += [
                "",
                f"汇总判别 AUC（仅 decisions + pending_items，n={content['pooled_flagged_n']} vs "
                f"{content['pooled_clean_n']}）= {_num(pooled)}。{content['reading']}",
                "",
                critique["content_support_note"],
                "",
            ]

        slots = block["slots"]
        lines += [
            "### 4. 负责人与期限（双向）",
            "",
            f"负责人状态：`{json.dumps(slots['owner_states'], ensure_ascii=False)}`",
            "",
            f"期限状态：`{json.dumps(slots['deadline_states'], ensure_ascii=False)}`",
            "",
            "`field_misuse` = 期限字段里写的是动作而非时间（文档记录过的真实缺陷）；"
            "`open_ended` = 真实的开放式时间（年内、上线后），不是缺陷。",
            "",
        ]
        misused = slots["examples_for_audit"].get("deadline_field_misuse") or []
        if misused:
            lines.append("期限字段误用样例：")
            lines.append("")
            for entry in misused[:5]:
                lines.append(f"- `{entry['case_id']}` /{entry['field']}：{entry['value']}")
            lines.append("")
        conditional = slots["conditional_deadline"]
        fp = conditional["unconditional_MISSING_DEADLINE_false_positive_rate"]
        lines += [
            "**`MISSING_DEADLINE` 规则的条件性检验**（当前实现无条件触发）：",
            "",
            f"空/未明确期限条目共 {fp['total']} 条，其中会议内确实没有截止日期的 "
            f"{fp['successes']} 条，即无条件规则在这些条目上会误报，"
            f"误报率 {_pct(fp['rate'])}（95% 区间 {_interval(fp)}）。",
            "",
            f"分桶：`{json.dumps(conditional['empty_deadline_bucket_counts'], ensure_ascii=False)}`。",
            "",
            conditional["note"],
            "",
        ]

        schema = block["schema"]
        lines += [
            "### 5. Schema 有效性与内容正确性分离",
            "",
            "| 版本 | Schema 有效 | 有效但内容被标记 | 有效但内容错的比例 | 标记条目数 |",
            "|---|---:|---:|---:|---:|",
        ]
        for side in ("baseline", "revised"):
            s = schema[side]
            validity = s["schema_validity_rate"]
            wrong = s["wrong_valid_schema_rate_among_valid"]
            lines.append(
                f"| {side} | {validity['successes']}/{validity['total']} | "
                f"{s['wrong_valid_schema_cases']} | {_pct(wrong['rate'])} | {s['items_flagged']} |"
            )
        lines += [
            "",
            f"首个失败路径直方图：`{json.dumps(schema['first_failing_path_histogram'], ensure_ascii=False)}`；"
            f"逐字段失败计数：`{json.dumps(schema['per_field_failure_count'], ensure_ascii=False)}`。",
            "",
        ]

    lines += [
        "## 代理登记表（每个代理的校准状态与证据）",
        "",
        "**被否证的代理保留在代码中以便复现，但不得作为质量数字引用。**",
        "",
        "| 代理 | 状态 | 证据 |",
        "|---|---|---|",
    ]
    for proxy_name, entry in (report.get("proxy_status") or {}).items():
        lines.append(f"| {proxy_name} | {entry['status']} | {entry['evidence']} |")
    lines.append("")

    alignment_seen = {}
    for name, block in report["sets"].items():
        alignment = (block.get("critique") or {}).get("proxy_alignment")
        if alignment:
            alignment_seen[name] = alignment
    if alignment_seen:
        lines += [
            "### 每次运行重新测量的代理对齐（校准不是一次性结论）",
            "",
            "| 数据集 | 行动候选 vs MISSING_ITEM ρ | 凭空片段 vs UNSUPPORTED ρ |",
            "|---|---:|---:|",
        ]
        for name, alignment in alignment_seen.items():
            a = alignment["action_candidates_vs_missing_item"]["spearman_rho"]
            b = alignment["invented_spans_vs_unsupported"]["spearman_rho"]
            lines.append(f"| {name} | {_num(a)} | {_num(b)} |")
        lines += ["", next(iter(alignment_seen.values()))["reading"], ""]

    amc = report.get("amc_aid") or {}
    if amc:
        lines += [
            "## 公开锚点：AliMeeting4MUG 行动项检测（AID）",
            "",
            "官方指标是**正类 F1**。此表为零成本自检：把金标当预测必须恰好得到 1.0，"
            "同时记录全负类与全正类两个平凡基线，说明为什么**准确率永远不能引用**。",
            "",
            "| 划分 | 自检 | 会议 | 句子 | 正例 | 正例率 | 全负类准确率 / F1 | 全正类 F1 |",
            "|---|---|---:|---:|---:|---:|---|---:|",
        ]
        for split, payload in amc.items():
            if "corpus" not in payload:
                lines.append(f"| {split} | 不可用：{payload.get('unavailable', '')} | | | | | | |")
                continue
            corpus = payload["corpus"]
            floor = corpus["trivial_baseline_all_negative"]
            lines.append(
                f"| {split} | {payload['self_check']['verdict']} | {corpus['meeting_count']} | "
                f"{corpus['sentence_count']:,} | {corpus['positive_sentence_count']} | "
                f"{_pct(corpus['positive_rate'], 3)} | "
                f"{_pct(floor['accuracy'], 2)} / {_num(floor['positive_f1'])} | "
                f"{_num(corpus['trivial_baseline_all_positive']['positive_f1'])} |"
            )
        lines += [
            "",
            "已发表基线（来源不一致，引用前须核对 ICASSP 2023 MUG 总览）："
            f"`{json.dumps((amc.get('dev') or {}).get('corpus', {}).get('published_baselines', {}), ensure_ascii=False)}`",
            "",
            "**该锚点尚未用于产品输出**：官方标签是句级"
            "「与行动项有关的句子」，而产品输出没有来源句 ID，必须先规定映射并人工复核歧义匹配。",
            "",
        ]

    lines += _render_citation_level(report.get("citation_level") or {})
    lines += _render_reliability(report.get("reliability") or {})

    lines += [
        "## 本报告不能支持的说法",
        "",
        "- 不能称为幻觉率、事实准确率或人工可采纳率。词法代理只覆盖日期、数量、具名实体。",
        "- 不能据此证明或否定 Reflection 的价值。代理功效有限，零结果不等于无效；"
        "要给出因果结论需要 `METRICS_RESEARCH.md` 中列出的空反馈对照臂与 oracle 对照臂，"
        "那些臂需要新的付费模型调用。",
        "- 不能把审查一致性当作审查准确率：两个方向都对着同一个代理，低一致既可能是代理弱，"
        "也可能是审查弱。",
        "- 所有者与期限的“遗漏”方向缺少独立金标，只有合成集有部分参考要点。",
        "",
        "## 仍需付费运行才能补齐的部分",
        "",
        "| 缺口 | 需要的运行 |",
        "|---|---|",
        "| 空反馈 refine 对照臂 | 与现有轮次同规模的 LLM 调用 |",
        "| oracle issue 列表上界臂 | 人工金标 issue + 每场一次 refine |",
        "| 算力对齐的自一致性基线 | 与两轮自检同预算的采样 |",
        "| 中文会议字段级人工金标 | 人工标注，无模型调用 |",
        "| CER / cpCER / DER | AliMeeting / AISHELL-4 转写调用 |",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--set", dest="sets", action="append", default=None,
        choices=["vcsum-test26", "vcsum-dev22", "vcsum-test26-v2", "vcsum-test26-v3", "all"],
        help="which stored result set to recompute; repeatable",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--results-root", type=Path, default=PUBLIC_RESULTS,
        help="可靠性与开销指标扫描的运行目录根（默认 benchmarks/results）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    requested = args.sets or ["vcsum-test26-v2"]
    selected: set[str] = set()
    for item in requested:
        if item == "all":
            selected |= {"vcsum-test26", "vcsum-dev22", "vcsum-test26-v2"}
        else:
            selected.add(item)

    sets: dict[str, Sequence[Mapping[str, Any]]] = {}
    contract_versions: dict[str, str] = {}
    unavailable: dict[str, str] = {}
    if "vcsum-test26" in selected and VCSUM_RUNS["vcsum-test26"].exists():
        sets["vcsum-test26"] = load_vcsum_cases("test26")
        contract_versions["vcsum-test26"] = contract_checks.LEGACY
    if "vcsum-dev22" in selected and VCSUM_RUNS["vcsum-dev22"].exists():
        sets["vcsum-dev22"] = load_vcsum_cases("dev22")
        contract_versions["vcsum-dev22"] = contract_checks.LEGACY
    if "vcsum-test26-v3" in selected:
        # 单遍长上下文（2026-10-06 改造后）的产物。**目录未登记时明确报错**：
        # 首轮单遍运行完成前这组数字不存在，静默跳过会掩盖"该跑还没跑"。
        try:
            from lib import corpora as _corpora
            sets["vcsum-test26-v3"] = _corpora.load_single_pass_cases(
                results_root=args.results_root)
            contract_versions["vcsum-test26-v3"] = contract_checks.V2
        except (FileNotFoundError, ValueError) as error:
            if requested == ["all"]:
                unavailable["vcsum-test26-v3"] = str(error)
                print(f"警告：单遍数据集不可用（{error}）；其余数据集继续。",
                      file=sys.stderr)
            else:
                raise
    if "vcsum-test26-v2" in selected:
        # 契约 v2 的产物必须真的存在才报这组数。**缺目录时不静默跳过**：显式请求它
        # （--set vcsum-test26-v2）就直接报错；而 --set all 会继续跑其他数据集，
        # 但把"这一组不可用"写进报告并打印到 stderr——"这组数字消失了"必须看起来
        # 与"这组数字正常"不同，同时不让缺一份产物堵掉无关的测量。
        try:
            sets["vcsum-test26-v2"] = load_new_contract_cases(results_root=args.results_root)
            contract_versions["vcsum-test26-v2"] = contract_checks.V2
        except FileNotFoundError as error:
            if requested == ["all"]:
                unavailable["vcsum-test26-v2"] = str(error)
                print(f"警告：契约 v2 数据集不可用（{error}）；其余数据集继续。",
                      file=sys.stderr)
            else:
                raise

    if not sets:
        print("no stored result sets found for the requested selection", file=sys.stderr)
        return 1

    report = build_report(sets, contract_versions=contract_versions,
                          results_root=args.results_root)
    if unavailable:
        report["unavailable_sets"] = unavailable
    args.output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = args.output_dir / "metrics.json"
    markdown_path = args.output_dir / "METRICS_REPORT.md"
    metrics_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(format_markdown(report), encoding="utf-8")

    for name, cases in sets.items():
        print(f"{name}: {len(cases)} cases")
    print(f"wrote {metrics_path}")
    print(f"wrote {markdown_path}")
    print("model calls made: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
