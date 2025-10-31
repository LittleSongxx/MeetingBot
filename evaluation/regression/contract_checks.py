"""把契约层的不变量接进离线指标套件。

动机：契约层（`services/contracts.py`）能判定四类结构性缺陷，但这些判定必须能在
**既有评测产物**上批量跑，否则每次都要人肉写脚本。这个模块就是那个接线口：
读已存的初稿/自检稿与转写，输出逐不变量、逐版本的计数。

前置条件与 `grounding.shared_contract` 相同的模式：产品目录可用时直接调用契约层
（需要 pydantic，因此要用 `ai_meeting/fastapi-app/.venv` 运行）；不可用时**明确标注
不可用**，而不是退回一份本地近似实现——契约判定没有"近似版"，近似就是另一套规则。

一条重要的测量限制写在 `RETRO_MEASUREMENT` 里：证据类不变量**无法在旧数据上回溯
测量**，因为旧契约从未要求模型给出证据。把这些计数报成"缺陷率"是错的。
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from lib import stats

PRODUCT_ROOT = Path(__file__).resolve().parents[2] / "ai_meeting" / "fastapi-app"

RETRO_MEASUREMENT = {
    "possible_on_legacy_data": (
        "slot_type_mismatch",
        "silent_deletion",
    ),
    "not_possible_on_legacy_data": (
        "stated_without_verifiable_evidence",
        "item_without_verifiable_evidence",
        "missing_rule_without_premise",
        "evidence_on_non_assertion",
    ),
    "why": (
        "旧契约（v1）的槽位是裸字符串，模型从未被要求给出 basis 或 evidence。"
        "在旧产物上跑证据类不变量，每一条带文本的断言都必然失败——那是把新契约的"
        "缺失记成旧数据的缺陷，是测量假象，不是发现。这些量只有在新契约下真实运行"
        "一次之后才有意义。"
    ),
    "artifact_note": (
        "因此本模块在旧产物上只报 slot_type_mismatch 与 silent_deletion；证据类计数"
        "仍会计算并单独放在 legacy_artifact_counts 下，标注为假象，禁止引用。"
    ),
    "v2_note": (
        "当产物本身来自契约 v2 运行（模型被要求填 basis 与 evidence）时，语义**反转**："
        "同一批证据类计数不再是假象，而是真实的缺陷率，应放在 quoted_counts 下引用。"
        "同一个函数在两代产物上测同一个量，因此必须由调用方显式声明产物属于哪一代。"
    ),
    "legacy_slot_mismatch_caveat": (
        "**旧产物上的 slot_type_mismatch 不是定义性判定，而是上界。** 在旧产物里 basis "
        "不是模型声明的，而是升级路径（`contracts._upgrade_item`）推断的：非空裸字符串"
        "一律标 stated，只有命中非断言词表的才标 not_mentioned。因此凡是"
        "「语义上是『未定』但措辞不在词表内」的值，都会被记成「声明了却没有时间语义」。"
        "实测（2026-09-25 复核）：合成集 5 条里 4 条是 `未定`、1 条是 `即时生效`；"
        "test26 的 19 条里混着 `完成数据收集和目标差距分析`（真缺陷）与"
        "`作为双方合作的第一步，具体时间未定`、`持续跟进`（未定/开放时段）。"
        "**只有 v2 产物上这一项才是定义性的**——那时 basis 由模型自己声明，"
        "词表漏写法不会产生假阳性。跨代比较这一项等于比较两件事。"
    ),
    "slot_mismatch_interpretation": (
        "旧产物：『时间槽里的文本在当前解析器下得不到时间语义』——含真缺陷（动作写进期限）"
        "与措辞不在词表内的未定/开放时段两类，因此只能作上界；"
        "v2 产物：『模型声明 stated 却给不出时间语义』——定义性，无假阳性来源。"
    ),
}

# 契约 v2 的运行里，这四项是真实测量而不是假象。
EVIDENCE_INVARIANTS = RETRO_MEASUREMENT["not_possible_on_legacy_data"]

LEGACY = "legacy"
V2 = "v2"


def load_contracts() -> Any | None:
    """载入产品契约层；需要 pydantic，因此要用产品的 venv 跑。"""
    if not (PRODUCT_ROOT / "services" / "contracts.py").exists():
        return None
    if str(PRODUCT_ROOT) not in sys.path:
        sys.path.insert(0, str(PRODUCT_ROOT))
    try:
        from services import contracts  # type: ignore
    except Exception:
        return None
    return contracts


def _flagged_value(contracts: Any, upgraded: Mapping[str, Any], path: str) -> str:
    """按违规路径取回原值文本，供审计表展示。取不到时返回空串而不是猜。"""
    match = re.match(r"(\w+)\[(\d+)\]\.(\w+)", path or "")
    if not match:
        return ""
    field_name, index, slot = match.group(1), int(match.group(2)), match.group(3)
    items = upgraded.get(field_name)
    if not isinstance(items, list) or index >= len(items):
        return ""
    item = items[index]
    if not isinstance(item, Mapping):
        return ""
    value = item.get(slot)
    if isinstance(value, Mapping):
        return str(value.get("text") or "")
    return str(value or "")


def measure_case(contracts: Any, case: Mapping[str, Any]) -> dict[str, Any]:
    """对一场会议算出三类计数：初稿违规、自检稿违规、初稿→自检的处置违规。

    同时留下 `*_examples`：违规路径与原值。计数会让"两类不同的东西合成一个数"看不出来，
    原值是唯一能让人一眼分辨的凭据（实测：旧产物上的 slot_type_mismatch 里既真有
    "动作写进期限"，也真有"未定/开放时段被词表漏掉"）。
    """
    transcript = case.get("transcript") or ""
    baseline = case.get("baseline_output")
    revised = case.get("revised_output")
    result: dict[str, Any] = {"case_id": case.get("case_id")}

    for label, output in (("baseline", baseline), ("revised", revised)):
        if not isinstance(output, Mapping):
            result[label] = {}
            continue
        upgraded = contracts.upgrade_output(output)
        violations = list(contracts.validate_minutes(upgraded, transcript))
        result[label] = dict(Counter(v.kind for v in violations))
        examples: dict[str, list[dict[str, str]]] = {}
        for violation in violations:
            bucket = examples.setdefault(violation.kind, [])
            if len(bucket) >= 8:
                continue
            bucket.append({
                "path": violation.path,
                "value": _flagged_value(contracts, upgraded, violation.path)[:80],
            })
        result[f"{label}_examples"] = examples

    if isinstance(baseline, Mapping) and isinstance(revised, Mapping):
        dispositions = contracts.validate_dispositions(baseline, revised, [], 0)
        result["draft_to_revised"] = dict(Counter(v.kind for v in dispositions))
        examples = {}
        for violation in dispositions:
            bucket = examples.setdefault(violation.kind, [])
            if len(bucket) >= 8:
                continue
            bucket.append({"path": violation.path, "value": violation.detail[:120]})
        result["draft_to_revised_examples"] = examples
    else:
        result["draft_to_revised"] = {}
    return result


def measure_issue_evidence(contracts: Any, record: Mapping[str, Any], transcript: str) -> dict[str, int]:
    """对审查意见跑证据策略门控。

    在旧产物上这**必然全数失败**（旧意见不含 source_span / target_text），所以调用方
    必须把结果标为假象；保留它是因为新契约运行后同一个函数就是真实测量。
    """
    counts: Counter[str] = Counter()
    for step in record.get("agent_steps") or []:
        if not isinstance(step, Mapping) or step.get("step_type") != "REVIEW":
            continue
        payload = step.get("payload") or {}
        issues = payload.get("issues") or []
        for violation in contracts.check_issue_evidence(issues, transcript):
            counts[violation.kind] += 1
        existing = payload.get("evidence_check")
        if isinstance(existing, Mapping):
            for kind, value in (existing.get("by_kind") or {}).items():
                counts[f"stored:{kind}"] += int(value)
    return dict(counts)


def _iter_evidence(items: Any) -> Iterable[str]:
    """逐条产出条目里的引用文本。非断言槽位的证据也算——它本来就不该存在，
    由 `evidence_on_non_assertion` 单独报，不在这里静默丢掉。"""
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for entry in item.get("evidence") or []:
            if isinstance(entry, Mapping):
                yield str(entry.get("quote") or "")
            elif isinstance(entry, str):
                yield entry
        for value in item.values():
            if isinstance(value, Mapping):
                for entry in value.get("evidence") or []:
                    if isinstance(entry, Mapping):
                        yield str(entry.get("quote") or "")


def _iter_evidence(items: Any, scope: str) -> Iterable[tuple[str, str]]:
    """逐条产出（范围，引用文本）。

    `scope` 参数只用于标记条目级证据；槽位级证据在下面固定标为 `slot_evidence`。

    两个范围必须分开报，因为它们不是一回事：
    * `item_evidence`：条目级证据（`topics[i].evidence`），答「这条内容的依据找得到吗」；
    * `slot_evidence`：类型化槽位自己的证据（`pending_items[i].deadline.evidence`），
      答「这个取值本身的依据找得到吗」。
    实测两者合计 4,476 条，其中槽位级 454 条。混成一个数会让口径比条目级惯用口径多
    11%，而对外引用过的 4,022/3,927 正是条目级——混报等于在引用时悄悄换口径。
    """
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for entry in item.get("evidence") or []:
            if isinstance(entry, Mapping):
                yield scope, str(entry.get("quote") or "")
            elif isinstance(entry, str):
                yield scope, entry
        for value in item.values():
            if isinstance(value, Mapping):
                for entry in value.get("evidence") or []:
                    if isinstance(entry, Mapping):
                        yield "slot_evidence", str(entry.get("quote") or "")


def locatability_report(contracts: Any, cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """证据可定位率：逐条证据判它能否在转写里逐字定位，按范围分开报。

    判据**委托产品契约层**（`contracts.evidence_locatable`），不在这里另写一份子串
    匹配。实测代价：用朴素空白归一得到 95.5%，用共享归一（去标点、NFC、小写）得到
    97.6%——同一批产物，两个数字。差异全部来自标点与全半角，因此「归一化方式」必须
    是共享的，否则这个比率会随实现者变化。
    """
    scopes = ("item_evidence", "slot_evidence")
    counters: dict[str, dict[str, Counter[str]]] = {
        scope: {"baseline": Counter(), "revised": Counter()} for scope in scopes
    }
    # 审计样例按**每场每版本至多 2 条**分层收集，而不是收满 20 条就停。实测教训：
    # 按遍历顺序收满 20 条时，样例会被排在最前面的那一场全部占满（vcsum_108 一场占
    # 20/20），审计者看到的是"某一场的怪样例"，而不是"这套产物的典型失败形态"。
    unlocated_examples: list[dict[str, str]] = []
    per_case_examples: Counter[str] = Counter()
    # 未定位里有多少条是**模型用省略号截断引用**（"……"把两段拼起来）。这是对同一个
    # 计数的诊断拆分，不是质量判定：它不参与任何阈值，拆错了也只影响这一行拆分，不影响
    # 上面的可定位率。记它是因为实测 95 条未定位里 51 条属于这一类——不加区分会让
    # "1.1% 无法定位"被读成"1.1% 是编造的引用"。
    unmatchable_breakdown: Counter[str] = Counter()
    ellipsis_marker = re.compile(r"…|\.\.\.|⋯|\.\.")
    for case in cases:
        transcript = case.get("transcript") or ""
        source_norm = contracts.normalize_source(transcript)
        for label, output in (("baseline", case.get("baseline_output")),
                              ("revised", case.get("revised_output"))):
            if not isinstance(output, Mapping):
                continue
            upgraded = contracts.upgrade_output(output)
            for field in contracts.FIELD_ORDER:
                if field == "summary":
                    continue
                for scope, quote in _iter_evidence(upgraded.get(field), "item_evidence"):
                    counter = counters[scope][label]
                    counter["evidence"] += 1
                    if contracts.evidence_locatable(quote, source_norm):
                        counter["locatable"] += 1
                        continue
                    key = "ellipsis_elided" if ellipsis_marker.search(quote) else "verbatim_mismatch"
                    unmatchable_breakdown[key] += 1
                    case_id = str(case.get("case_id"))
                    if per_case_examples[case_id] < 2 and len(unlocated_examples) < 30:
                        per_case_examples[case_id] += 1
                        unlocated_examples.append({
                            "case_id": case_id,
                            "version": label,
                            "scope": scope,
                            "field": field,
                            "kind": key,
                            "quote": quote[:120],
                        })

    def summarize(counter: Counter[str]) -> dict[str, Any]:
        total = counter.get("evidence", 0)
        located = counter.get("locatable", 0)
        return {
            "evidence": total,
            "locatable": located,
            "unlocatable": total - located,
            "locatable_rate": stats.proportion_report(located, total) if total else None,
        }

    by_scope: dict[str, Any] = {}
    for scope in scopes:
        scope_combined = Counter()
        for counter in counters[scope].values():
            scope_combined.update(counter)
        by_scope[scope] = {
            "by_version": {label: summarize(c) for label, c in counters[scope].items()},
            "combined": summarize(scope_combined),
        }
    combined = Counter()
    for scope in scopes:
        for counter in counters[scope].values():
            combined.update(counter)
    return {
        "by_scope": by_scope,
        "combined": summarize(combined),
        "unlocated_breakdown": dict(unmatchable_breakdown),
        "unlocated_examples_for_audit": unlocated_examples,
        "criterion": (
            "逐条证据的 quote 经 common/textsim.normalize（NFC、去空白与标点、ASCII 小写化）"
            "后是转写的连续子串，且归一后长度 ≥ 2。判据来自产品契约层，不是评测侧另写的"
            "近似实现。"
        ),
        "scope_note": (
            "对外引用条目级证据数（本项目此前报的 4,022/3,927 即 item_evidence 口径）；"
            "槽位级证据单列，不并入。"
        ),
        "breakdown_note": (
            "`unlocated_breakdown` 只是对未定位计数的诊断拆分：`ellipsis_elided` 是引文里"
            "带省略号（模型把两段拼起来），`verbatim_mismatch` 是不带省略号却找不到。"
            "它不参与任何阈值，也不改变可定位率；记它的目的是避免把「引用被截断」读成"
            "「引用是编造的」。"
        ),
    }


def item_report(contracts: Any, cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """逐版本的条目数、带证据条目数、类型化槽位数（按 basis 分）。

    这是「模型是否真的按新契约产出」的直接读数：条目带证据的比例、槽位声明 basis 的
    比例。三个数都是数出来的，不涉及判断。
    """
    counters: dict[str, Counter[str]] = {"baseline": Counter(), "revised": Counter()}
    for case in cases:
        for label, output in (("baseline", case.get("baseline_output")),
                              ("revised", case.get("revised_output"))):
            if not isinstance(output, Mapping):
                continue
            upgraded = contracts.upgrade_output(output)
            for field in contracts.FIELD_ORDER:
                if field == "summary":
                    continue
                items = upgraded.get(field) or []
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    counters[label]["items"] += 1
                    if item.get("evidence"):
                        counters[label]["items_with_evidence"] += 1
                    for slot in (contracts.SLOT_KINDS.get(field) or {}):
                        value = item.get(slot)
                        if isinstance(value, Mapping) and value.get("basis"):
                            counters[label]["typed_slots"] += 1
                            counters[label][f"basis_{value['basis']}"] += 1

    def summarize(counter: Counter[str]) -> dict[str, Any]:
        items = counter.get("items", 0)
        return {
            "items": items,
            "items_with_evidence": counter.get("items_with_evidence", 0),
            "typed_slots": counter.get("typed_slots", 0),
            "basis_breakdown": {
                key[len("basis_"):]: value for key, value in sorted(counter.items())
                if key.startswith("basis_")
            },
            "evidence_coverage": (
                stats.proportion_report(counter.get("items_with_evidence", 0), items)
                if items else None
            ),
        }

    combined = Counter()
    for counter in counters.values():
        combined.update(counter)
    return {
        "by_version": {label: summarize(counter) for label, counter in counters.items()},
        "combined": summarize(combined),
    }


def build_report(
    cases: Sequence[Mapping[str, Any]], *, data_contract: str = LEGACY
) -> dict[str, Any]:
    """契约不变量报告。

    `data_contract` 必须由调用方显式给出：`legacy` 表示产物来自契约 v1（模型未被要求
    给证据），`v2` 表示产物来自契约 v2。**同一个计数在两代产物上的含义相反**，因此
    这不是可选的推测项——传错会把真实缺陷率报成假象，或者反过来。
    """
    if data_contract not in (LEGACY, V2):
        raise ValueError(f"unknown data_contract: {data_contract!r}")
    contracts = load_contracts()
    if contracts is None:
        return {
            "available": False,
            "data_contract": data_contract,
            "reason": (
                "无法载入产品契约层。契约判定没有近似实现，因此这里不提供替代数字。"
                "请用 ai_meeting/fastapi-app/.venv/bin/python 运行本套件。"
            ),
            "limits": RETRO_MEASUREMENT,
        }

    per_case = [measure_case(contracts, case) for case in cases]
    totals: dict[str, Counter[str]] = {"baseline": Counter(), "revised": Counter(),
                                       "draft_to_revised": Counter()}
    for row in per_case:
        for label in totals:
            for kind, count in (row.get(label) or {}).items():
                totals[label][kind] += int(count)

    invariable = set(RETRO_MEASUREMENT["possible_on_legacy_data"])
    if data_contract == V2:
        invariable |= set(EVIDENCE_INVARIANTS)
    meaningful = {
        label: {kind: count for kind, count in totals[label].items() if kind in invariable}
        for label in ("baseline", "revised", "draft_to_revised")
    }
    artifact = {
        label: {kind: count for kind, count in totals[label].items() if kind not in invariable}
        for label in ("baseline", "revised", "draft_to_revised")
    }

    issue_counts: Counter[str] = Counter()
    for case in cases:
        record = case.get("record") or {}
        for kind, count in measure_issue_evidence(
            contracts, record, case.get("transcript") or ""
        ).items():
            issue_counts[kind] += int(count)

    evidence_side = {
        "locatability": locatability_report(contracts, cases),
        "items": item_report(contracts, cases),
    }

    if data_contract == V2:
        result = {
            "available": True,
            "data_contract": data_contract,
            "case_count": len(per_case),
            "quoted_counts": meaningful,
            "item_and_evidence": evidence_side,
            "issue_evidence_check": dict(issue_counts),
            "per_case": per_case,
            "limits": RETRO_MEASUREMENT,
            "reading": (
                "产物来自契约 v2（模型被要求给出 basis 与 evidence），因此证据类不变量"
                "是**真实缺陷率**，可以与两条结构类一起引用。`item_and_evidence` 里的"
                "证据可定位率同样可以引用：它判的是「引用能不能在转写里逐字找到」，"
                "与「算不算有依据」的争议无关。"
            ),
        }
    else:
        result = {
            "available": True,
            "data_contract": data_contract,
            "case_count": len(per_case),
            "quoted_counts": meaningful,
            "legacy_artifact_counts": artifact,
            "item_and_evidence": evidence_side,
            "issue_evidence_check": dict(issue_counts),
            "per_case": per_case,
            "limits": RETRO_MEASUREMENT,
            "reading": (
                "quoted_counts 是在旧产物上**可以**测的两个量，但只有 "
                "silent_deletion（措辞无处留存的消失条目）是定义性的。"
                "`slot_type_mismatch` 在旧产物上是**上界**而不是定义性判定：见 "
                "`limits.legacy_slot_mismatch_caveat`——它的 basis 由升级路径推断，"
                "词表漏掉的「未定/开放时段」措辞会被算成「声明了却没有时间语义」。"
                "legacy_artifact_counts 与 issue_evidence_check 在旧产物上是假象，"
                "只在新契约真实运行后才有意义——那时它们就是真实的缺陷率。"
            ),
        }
    return result
