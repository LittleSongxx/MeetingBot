"""纪要契约层：把"不许凭空断言"从提示词规矩变成数据结构的不变量。

## 为什么要这一层

旧实现里，六类缺陷各有一堆启发式去事后检测：期限槽里出现动作、负责人是编的、
风险是模型自撰的……每加一类就要加一条规则，规则又要靠词表维护，词表每漏一个
写法就多一个假阳性。这是**过拟合式的修法**：缺陷类有穷，写法的变体无穷。

这一层的做法是把不变量**下沉到契约**，让违规在结构上可判定甚至不可表达：

1. **类型化槽位**：任何"断言了一个值"的槽位都要声明 `basis`（stated / derived /
   open_ended / not_mentioned / model_inferred）。于是"把动作写进时间槽"不是一个
   要检测的模式，而是**类型不符**——`basis=stated` 且文本不承载时间语义。
2. **来源闭合**：每个条目都要声明 `origin`，且 `origin` 不是 `model_inferred` 时
   必须给出可核验的证据 span。于是"模型自撰的风险"不是靠判断，而是靠 `origin`
   如实标注；标注不实会被校验器抓。
3. **证据只在断言处允许**：`not_mentioned` / `model_inferred` 的槽位**不允许**带证据。
   伪造引用因此也是一个可判定的违规，而不是需要识别的行为。
4. **槽位类型表驱动**：`FIELD_SLOTS` 一张表声明"哪个字段的哪个槽位是什么类型"。
   以后新增字段或槽位只加表行，**不需要新检测代码**。
5. **审查证据策略表驱动**：`ISSUE_EVIDENCE_POLICY` 声明每类审查意见必须提供哪种
   证据（正向：原文确实有 / 反向：原文确实没有 / 不适用）。于是"期限规则 77.8%
   误报"暴露出的门控问题，对六类规则**一次性**解决，而不是只给期限打补丁。
6. **保默认的编辑语义**：refine 输出编辑指令而不是重写全文，`keep` 免费而
   `delete`/`replace` 必须起因于某条 issue 并附证据。抑制过度删除是**结构性的**，
   不依赖提示词里写"请尽量保留"。

## 契约版本与表现适配

存储是类型化且带版本的；展示是字符串的。`upgrade_output` 读旧数据、
`present_output` 产出展示形态，消费方（导出、前端、API）只依赖后者，
因此契约升级不会波及它们。

纯 pydantic + 标准库；时间语义判定复用 `common/timex.py`，与评测套件同一份定义。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, Field, model_validator

from common import textsim, timex

CONTRACT_VERSION = 2

# --------------------------------------------------------------------------- #
# 槽位类型：一张表声明"哪个字段的哪个槽位是什么类型"
# --------------------------------------------------------------------------- #

SLOT_TIME = "time"
SLOT_ENTITY = "entity"

SLOT_KINDS: dict[str, dict[str, str]] = {
    "decisions": {"owner": SLOT_ENTITY, "deadline": SLOT_TIME},
    "pending_items": {"owner": SLOT_ENTITY, "deadline": SLOT_TIME},
    "risks": {"raised_by": SLOT_ENTITY},
}

# `basis` 的封闭集合。与 timex.TIME_BASES 保持一致，但实体槽位不用 derived 之外的
# 时间语义，所以单独声明一份可读的取值。
BASES = ("stated", "derived", "open_ended", "not_mentioned", "model_inferred")

ORIGINS = ("stated", "derived", "model_inferred")


class Evidence(BaseModel):
    """一条可核验的出处。quote 必须是转写的连续子串。"""

    quote: str = ""
    speaker: str = ""


class SourcedValue(BaseModel):
    """类型化的值槽。`basis` 说明这个值是怎么来的，`evidence` 说明凭什么。"""

    text: str = ""
    basis: Literal["stated", "derived", "open_ended", "not_mentioned", "model_inferred"] = "not_mentioned"
    normalized: str = ""
    anchor: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 审查意见的证据策略：一张表覆盖全部六类
# --------------------------------------------------------------------------- #

EVIDENCE_POSITIVE = "positive"  # 必须证明"原文确实有"（缺失类规则）
EVIDENCE_NEGATIVE = "negative"  # 必须证明"原文确实没有"（无依据类规则）
EVIDENCE_NONE = "none"          # 与原文出处无关（可执行性、表述质量）

ISSUE_EVIDENCE_POLICY: dict[str, str] = {
    "MISSING_OWNER": EVIDENCE_POSITIVE,
    "MISSING_DEADLINE": EVIDENCE_POSITIVE,
    "MISSING_ITEM": EVIDENCE_POSITIVE,
    "UNSUPPORTED": EVIDENCE_NEGATIVE,
    "NOT_ACTIONABLE": EVIDENCE_NONE,
    "VAGUE": EVIDENCE_NONE,
}



# --------------------------------------------------------------------------- #
# 违规记录
# --------------------------------------------------------------------------- #


class Violation(BaseModel):
    kind: str
    path: str
    detail: str = ""
    severity: Literal["error", "warning"] = "error"


def _norm(text: object) -> str:
    """文本归一。委托 common.textsim，产品与评测共用一份定义。"""
    return textsim.normalize(text)


def _findable(quote: str, source_norm: str) -> bool:
    """证据是否真的出现在原文里。空引用一律不算。"""
    normalized = _norm(quote)
    return len(normalized) >= 2 and normalized in source_norm


def normalize_source(text: object) -> str:
    """把转写归一成证据定位所用的形式。与产品校验内部是同一份定义。"""
    return _norm(text)


def evidence_locatable(quote: object, source_norm: str) -> bool:
    """公开出口：这条证据能否在**已归一化**的原文里逐字定位。

    评测套件要逐条报"证据可定位率"，产品要逐条判来源闭合，两者必须是**同一判据**：
    各写一份必然漂移，而这里的漂移会让"产品判合规、指标判不可定位"同时成立。
    调用方先用 `normalize_source(transcript)` 归一一次长转写，再逐条调用本函数，
    避免对每个引用重复归一全文。
    """
    return _findable(str(quote or ""), source_norm)


# --------------------------------------------------------------------------- #
# 槽位校验：按 SLOT_KINDS 表驱动，不针对具体字段
# --------------------------------------------------------------------------- #


def validate_slot(
    path: str, slot_kind: str, value: SourcedValue, source_norm: str,
    *, reference_date: str = "",
) -> list[Violation]:
    """校验一个类型化槽位。所有规则都由 slot_kind 决定，与字段无关。"""
    problems: list[Violation] = []
    basis = value.basis if value.basis in BASES else "not_mentioned"
    text = (value.text or "").strip()

    # 1) 断言必须有内容：basis 说"有"就不能是空的
    asserting = basis in {"stated", "derived", "model_inferred"}
    if asserting and not text and not value.normalized:
        problems.append(Violation(
            kind="asserted_slot_without_value", path=path,
            detail=f"basis={basis} 表示有取值，但 text 与 normalized 都为空",
        ))

    # 2) 非断言不得携带证据：伪造引用是可判定的
    if not asserting and value.evidence:
        problems.append(Violation(
            kind="evidence_on_non_assertion", path=path,
            detail=f"basis={basis} 不应带证据，却给了 {len(value.evidence)} 条",
        ))
    # 2b) 非断言也不该有归一化值。实测（DeepSeek 首轮）发现模型会把"待确认""未明确"
    # 这类字面照样填进 normalized——不算语义错误，但会污染下游取值，故按 warning 记。
    if not asserting and (value.normalized or value.anchor):
        problems.append(Violation(
            kind="normalized_on_non_assertion", path=path, severity="warning",
            detail=f"basis={basis} 表示没有取值，normalized/anchor 应为空，"
                   f"实际为 {value.normalized!r}/{value.anchor!r}",
        ))

    # 3) 类型相符：时间槽声称 stated 时，文本必须承载时间语义
    if slot_kind == SLOT_TIME:
        if basis == "stated" and text and not timex.looks_like_a_time(text, reference_date=reference_date):
            problems.append(Violation(
                kind="slot_type_mismatch", path=path,
                detail=f"时间槽 basis=stated 但文本不承载时间语义：{text[:40]!r}",
            ))
        if basis == "stated" and text:
            parsed = timex.parse_time(text, reference_date=reference_date)
            if value.normalized and parsed.normalized and value.normalized != parsed.normalized:
                problems.append(Violation(
                    kind="normalized_value_mismatch", path=path, severity="warning",
                    detail=f"归一化 {value.normalized!r} 与解析结果 {parsed.normalized!r} 不一致",
                ))
    elif slot_kind == SLOT_ENTITY:
        # 实体槽位不得用开放时段语义——那是时间槽的取值
        if basis == "open_ended":
            problems.append(Violation(
                kind="slot_type_mismatch", path=path,
                detail="实体槽位不应取 open_ended，该取值只对时间槽有意义",
            ))

    # 4) 来源闭合：断言类必须可核验或声明锚点
    if basis == "stated":
        usable = [e for e in value.evidence if _findable(e.quote, source_norm)]
        if not usable:
            problems.append(Violation(
                kind="stated_without_verifiable_evidence", path=path,
                detail="basis=stated 但没有任何一条证据能在原文中定位",
            ))
    elif basis == "derived":
        if not value.anchor and not [e for e in value.evidence if _findable(e.quote, source_norm)]:
            problems.append(Violation(
                kind="derived_without_anchor", path=path,
                detail="basis=derived 但既没给 anchor 也没有可定位的证据",
            ))

    return problems


def _value_of(item: Mapping[str, Any], key: str) -> SourcedValue:
    raw = item.get(key)
    if isinstance(raw, Mapping):
        return SourcedValue.model_validate(raw)
    if isinstance(raw, str):
        # 兼容旧形态：裸字符串按"原文陈述"处理，但证据由探测补
        return SourcedValue(text=raw, basis="stated" if raw.strip() else "not_mentioned")
    return SourcedValue()


def validate_minutes(
    output: Mapping[str, Any] | None,
    transcript: str = "",
    *,
    reference_date: str = "",
) -> list[Violation]:
    """校验整份纪要。表驱动：遍历 SLOT_KINDS + 逐条目的 origin/evidence 闭合。"""
    problems: list[Violation] = []
    if not isinstance(output, Mapping):
        return [Violation(kind="not_an_object", path="root", detail="输出不是对象")]
    source_norm = _norm(transcript)

    for field_name, slots in SLOT_KINDS.items():
        items = output.get(field_name) or []
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            for slot, kind in slots.items():
                value = _value_of(item, slot)
                base_path = f"{field_name}[{index}].{slot}"
                problems.extend(validate_slot(base_path, kind, value, source_norm,
                                              reference_date=reference_date))

    # 条目级来源闭合：非 model_inferred 的条目必须给出可定位的证据
    for field_name in ("summary", "topics", "viewpoints", "decisions", "pending_items", "risks"):
        items = output.get(field_name) or []
        if field_name == "summary":
            items = [{"summary": items}] if isinstance(items, str) else []
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            origin = item.get("origin") or ("model_inferred" if field_name == "risks" else "stated")
            if origin == "model_inferred":
                continue
            evidence = item.get("evidence") or []
            usable = [
                e for e in evidence
                if isinstance(e, Mapping) and _findable(str(e.get("quote") or ""), source_norm)
            ]
            if not usable:
                problems.append(Violation(
                    kind="item_without_verifiable_evidence",
                    path=f"{field_name}[{index}]",
                    detail=f"origin={origin} 但无可定位证据",
                    severity="warning",
                ))
    return problems


# --------------------------------------------------------------------------- #
# 审查意见的证据门控：策略表驱动
# --------------------------------------------------------------------------- #


def issue_evidence_verdicts(
    issues: Sequence[Mapping[str, Any]], transcript: str
) -> list[dict[str, Any]]:
    """**唯一的实现**：逐条意见给出"证据要求是否满足"的判定。

    返回 `[{index, issue_type, policy, ok, violations:[Violation]}]`。
    `check_issue_evidence`（报告用）与 `gate_issues_by_evidence`（硬门控用）
    都调用它，因此"记什么"与"拦什么"永远是同一套规则——两处各写一遍必然漂移。
    """
    source_norm = _norm(transcript)
    verdicts: list[dict[str, Any]] = []
    for index, issue in enumerate(issues or []):
        if not isinstance(issue, Mapping):
            continue
        issue_type = str(issue.get("issue_type") or "")
        policy = ISSUE_EVIDENCE_POLICY.get(issue_type, EVIDENCE_NONE)
        span = str(issue.get("source_span") or "")
        target = str(issue.get("target_text") or "")
        problems: list[Violation] = []

        if policy == EVIDENCE_POSITIVE:
            # 缺失类：必须先证明"原文里**有**"，否则无权声称输出"缺了"
            if not span.strip():
                problems.append(Violation(
                    kind="missing_rule_without_premise", path=f"issues[{index}]({issue_type})",
                    detail="缺失类规则未给出原文依据，无法确认该信息确实存在",
                ))
            elif not _findable(span, source_norm):
                problems.append(Violation(
                    kind="missing_rule_with_unsupported_premise",
                    path=f"issues[{index}]({issue_type})",
                    detail=f"所给原文依据无法在转写中定位：{span[:40]!r}",
                ))
        elif policy == EVIDENCE_NEGATIVE:
            # 无依据类：必须先指明被判定的文本，且该文本**不在**原文里
            if not target.strip():
                problems.append(Violation(
                    kind="unsupported_rule_without_target", path=f"issues[{index}]({issue_type})",
                    detail="无依据类规则未指明被判定的文本",
                ))
            elif _findable(target, source_norm):
                problems.append(Violation(
                    kind="unsupported_claim_present_in_source",
                    path=f"issues[{index}]({issue_type})",
                    detail="被判为无依据的文本能在原文中定位，判定可能错误",
                ))
        verdicts.append({"index": index, "issue_type": issue_type, "policy": policy,
                         "ok": not problems, "violations": problems})
    return verdicts


def check_issue_evidence(
    issues: Sequence[Mapping[str, Any]], transcript: str
) -> list[Violation]:
    """按 `ISSUE_EVIDENCE_POLICY` 校验审查意见是否满足各自的证据要求（报告口径）。

    这是"条件化规则"的通用实现：缺失类规则必须先证明原文里**有**该信息，
    才允许声称输出里**缺**了它；无依据类规则反过来，必须先证明原文里**没有**。
    新增 issue 类型只加一行策略，不加校验代码。

    本函数只是 `issue_evidence_verdicts` 的扁平化视图，两者不会漂移。
    """
    return [v for verdict in issue_evidence_verdicts(issues, transcript)
            for v in verdict["violations"]]


def gate_issues_by_evidence(
    issues: Sequence[Mapping[str, Any]], transcript: str
) -> dict[str, Any]:
    """**硬门控**：把证据要求不满足的意见挡在重写步之外。

    为什么必须硬拦（2026-09-25/26 实测）：门控此前只记录不拦，于是"没有任何前提的
    主观意见"照样推动重写，实测后果是删除不按依据筛选（被删条目被判有依据 41%，
    比保留下来的 25% 还高），而独立模型族在业界口径下只认可 25% 的 `UNSUPPORTED` 意见。
    冻结标准（`JUDGMENT_STANDARD.md`）要求"任何重写都必须说明它依据哪条前提"，
    只记录不拦等于没有标准。

    返回 `{"kept": [...], "dropped": [{issue, reasons}], "by_kind": {...}}`；
    **被丢弃的意见原样保留在 dropped 里**，供页面与评测查看，不静默消失。
    """
    verdicts = issue_evidence_verdicts(issues, transcript)
    kept: list[Mapping[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    by_kind: Counter[str] = Counter()
    for verdict in verdicts:
        issue = issues[verdict["index"]]
        if verdict["ok"]:
            kept.append(issue)
            continue
        reasons = [v.kind for v in verdict["violations"]]
        for reason in reasons:
            by_kind[reason] += 1
        dropped.append({
            "issue": dict(issue) if isinstance(issue, Mapping) else issue,
            "reasons": reasons,
            "detail": "; ".join(v.detail for v in verdict["violations"])[:300],
        })
    return {"kept": kept, "dropped": dropped, "by_kind": dict(by_kind),
            "checked": len(verdicts)}


# --------------------------------------------------------------------------- #
# 编辑指令：保默认
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 契约版本与表现适配
# --------------------------------------------------------------------------- #


def _upgrade_item(item: Any) -> Any:
    """把 v1 的裸字符串槽位升级为类型化槽位，并据原文补齐 basis。"""
    if not isinstance(item, Mapping):
        return item
    upgraded = dict(item)
    for slot in ("owner", "deadline", "raised_by"):
        raw = upgraded.get(slot)
        if raw is None:
            legacy_key = f"{slot}_suggestion"
            raw = upgraded.get(legacy_key)
            if raw is not None:
                upgraded.pop(legacy_key, None)
        if raw is None or isinstance(raw, Mapping):
            continue
        text = str(raw).strip()
        if not text or timex.is_non_assertion(text):
            upgraded[slot] = SourcedValue(text=text, basis="not_mentioned").model_dump()
            continue
        if slot == "deadline":
            parsed = timex.parse_time(text)
            # 旧数据里"把动作写进时间槽"的值会落到 basis=""：如实保留文本并把 basis
            # 标为 stated，让校验器报 slot_type_mismatch，而不是在这里静默丢弃
            upgraded[slot] = SourcedValue(
                text=text,
                basis=parsed.basis if parsed.basis else "stated",
                normalized=parsed.normalized,
                anchor=parsed.anchor,
            ).model_dump()
        else:
            upgraded[slot] = SourcedValue(text=text, basis="stated").model_dump()
    upgraded.setdefault("contract_version", CONTRACT_VERSION)
    return upgraded


def upgrade_output(output: Mapping[str, Any] | None) -> dict[str, Any]:
    """读侧适配：把任意历史形态收敛到当前契约。幂等。"""
    if not isinstance(output, Mapping):
        return {}
    result: dict[str, Any] = dict(output)
    for field_name in ("decisions", "pending_items", "risks"):
        items = result.get(field_name)
        if isinstance(items, list):
            result[field_name] = [_upgrade_item(item) for item in items]
    result["contract_version"] = CONTRACT_VERSION
    return result


def present_output(output: Mapping[str, Any] | None) -> dict[str, Any]:
    """写侧适配：产出消费方需要的字符串形态。

    导出、API 与前端只依赖这里，因此契约继续演进不会波及它们。
    """
    if not isinstance(output, Mapping):
        return {}
    presented: dict[str, Any] = dict(output)
    for field_name, slots in SLOT_KINDS.items():
        items = presented.get(field_name)
        if not isinstance(items, list):
            continue
        rendered: list[Any] = []
        for item in items:
            if not isinstance(item, Mapping):
                rendered.append(item)
                continue
            clone = dict(item)
            for slot in slots:
                if slot in clone:
                    clone[f"{slot}_suggestion"] = timex.render_time_value(clone.pop(slot))
                elif f"{slot}_suggestion" in clone:
                    clone[f"{slot}_suggestion"] = timex.render_time_value(clone[f"{slot}_suggestion"])
            rendered.append(clone)
        presented[field_name] = rendered
    return presented


# --------------------------------------------------------------------------- #
# 一致性报告：供评测与观测复用
# --------------------------------------------------------------------------- #


def conformance_report(
    output: Mapping[str, Any] | None,
    transcript: str = "",
    *,
    issues: Sequence[Mapping[str, Any]] | None = None,
    reference_date: str = "",
) -> dict[str, Any]:
    """把全部不变量检查聚成一个报告。评测套件与观测页共用同一入口。"""
    violations = validate_minutes(output, transcript, reference_date=reference_date)
    if issues is not None:
        violations.extend(check_issue_evidence(issues, transcript))
    by_kind: dict[str, int] = {}
    for violation in violations:
        by_kind[violation.kind] = by_kind.get(violation.kind, 0) + 1
    errors = [v for v in violations if v.severity == "error"]
    return {
        "contract_version": CONTRACT_VERSION,
        "violation_count": len(violations),
        "error_count": len(errors),
        "warning_count": len(violations) - len(errors),
        "by_kind": by_kind,
        "violations": [v.model_dump() for v in violations],
        "reading": (
            "违规是可计数的缺陷率，不是致命错误：单条违规不应丢掉整份纪要。"
            "kind 的分布即缺陷类型分布，新增字段或槽位只需扩展 SLOT_KINDS / "
            "ISSUE_EVIDENCE_POLICY，不需要新增检测逻辑"
        ),
    }


def present_minutes_fields(
    source: Mapping[str, Any] | None,
    *,
    summary: Any = None,
    topics: Any = None,
    viewpoints: Any = None,
    decisions: Any = None,
    pending_items: Any = None,
    risks: Any = None,
) -> dict[str, Any]:
    """把存库的六个字段适配成消费方形态。

    这是**唯一的适配出口**：详情、导出、写回都走这里，因此契约从字符串演进为
    类型化结构时，前端与导出代码一行都不用改。
    """
    payload = {
        "summary": summary if summary is not None else (source or {}).get("summary"),
        "topics": topics if topics is not None else (source or {}).get("topics"),
        "viewpoints": viewpoints if viewpoints is not None else (source or {}).get("viewpoints"),
        "decisions": decisions if decisions is not None else (source or {}).get("decisions"),
        "pending_items": pending_items if pending_items is not None else (source or {}).get("pending_items"),
        "risks": risks if risks is not None else (source or {}).get("risks"),
    }
    return present_output(payload)


# 字段的主文本键：用来判断"同一个条目是否还在"，与顺序无关。
# 「同一条目」的**声明键**：有它就按键配对（改得再多也还是同一条），
# 没有就退回相似度判据。为什么需要键：只用相似度时，一条条目被改得多一点就会被拆成
# "删了一条 + 加了一条"，变更清单随即失去可读性——实测踩到（topics 标题不变、
# 摘要改写，结果同一标题同时出现在新增与删除里）。
# 这里是一张**声明表**：新增字段只加一行，不写字段专用分支。
CHANGE_IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "topics": ("title",),
}

PRIMARY_TEXT_KEY: dict[str, tuple[str, ...]] = {
    "summary": ("summary",),
    "topics": ("title", "summary"),
    "viewpoints": ("speaker", "viewpoint"),
    "decisions": ("content",),
    "pending_items": ("content",),
    "risks": ("content",),
}

DISPOSITION_OPS = ("keep", "replace", "delete", "add")


def _primary_text(field_name: str, item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    if field_name == "summary":
        return _norm(str(item.get("summary") or ""))
    keys = PRIMARY_TEXT_KEY.get(field_name, ("content",))
    return _norm(" ".join(str(item.get(key) or "") for key in keys))


def _item_key(field: str, item: Mapping[str, Any], key_names: Sequence[str]) -> str:
    """条目的声明键（归一化后）。任一键为空就返回空串，退回相似度判据。"""
    if not key_names:
        return ""
    parts = [_norm(str(item.get(name) or "")) for name in key_names]
    if not all(parts):
        return ""
    return "\x1f".join(parts)


def build_change_list(
    working: Mapping[str, Any] | None,
    revised: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """逐条列出这一版**改了什么**，让修订稿变成可审的变更清单而不是要人整篇比对。

    为什么这是结构性的：真实运行库里量到 **Zero Rate 只有 1.4%**（六字段全部未改的
    运行占比），编辑距离中位数 0.250——也就是说几乎每一次自检都把一份**全文改写**的稿子
    摆到人面前，人得拿两份长文档逐段比对。同一批数据里 114/145 次运行停在等人确认、
    155 份纪要只有 1 份被确认。**审阅成本高到没人审**，这不是 UI 问题，
    是"修订以整篇重写的形式交付"这个形态的问题。

    因此这里做的是**与模型无关的纯计算**：用同一条条目判据（`textsim.is_same_item`）
    把初稿与改写稿配对，逐条给 新增 / 删除 / 修改 / 未变。它不改协议、不加调用，
    也不依赖模型愿意输出声明——任何一次重写都能立刻算出"改了几条、改了哪几条"。

    返回的 `summary_line` 是给人看的一句话，页面上不必读 payload 也能知道这一版动了什么。
    """
    fields = ("topics", "viewpoints", "decisions", "pending_items", "risks")
    detail: dict[str, Any] = {}
    totals = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}
    for field in fields:
        before = [item for item in (working or {}).get(field) or [] if isinstance(item, Mapping)]
        after = [item for item in (revised or {}).get(field) or [] if isinstance(item, Mapping)]
        after_texts = [_primary_text(field, item) for item in after]
        key_names = CHANGE_IDENTITY_KEYS.get(field, ())
        after_keys = [_item_key(field, item, key_names) for item in after]
        matched_after: set[int] = set()
        added: list[str] = []
        removed: list[str] = []
        modified: list[str] = []
        unchanged = 0
        for item in before:
            text = _primary_text(field, item)
            key = _item_key(field, item, key_names)
            hit = None
            if key:
                # 键命中即为同一条目：标题没变、正文改了，那是**修改**而不是删+加
                hit = next((index for index, candidate in enumerate(after_keys)
                            if index not in matched_after and candidate == key), None)
            if hit is None:
                hit = next((index for index, candidate in enumerate(after_texts)
                            if index not in matched_after and candidate
                            and textsim.is_same_item(text, candidate)), None)
            if hit is None:
                removed.append(str(item.get("content") or item.get("title")
                                   or item.get("viewpoint") or text)[:120])
                continue
            matched_after.add(hit)
            if text == after_texts[hit]:
                unchanged += 1
            else:
                modified.append(str(item.get("content") or item.get("title")
                                    or item.get("viewpoint") or text)[:120])
        for index, item in enumerate(after):
            if index not in matched_after:
                added.append(str(item.get("content") or item.get("title")
                                 or item.get("viewpoint") or after_texts[index])[:120])
        detail[field] = {"added": added, "removed": removed, "modified": modified,
                         "unchanged": unchanged,
                         "before": len(before), "after": len(after)}
        totals["added"] += len(added)
        totals["removed"] += len(removed)
        totals["modified"] += len(modified)
        totals["unchanged"] += unchanged
    changed = totals["added"] + totals["removed"] + totals["modified"]
    return {
        "per_field": detail,
        "totals": {**totals, "changed": changed},
        "summary_line": (
            f"本版共 {changed} 处改动：新增 {totals['added']} 条、删除 {totals['removed']} 条、"
            f"修改 {totals['modified']} 条，其余 {totals['unchanged']} 条未动"
        ),
    }


def validate_dispositions(
    base: Mapping[str, Any] | None,
    refined: Mapping[str, Any] | None,
    dispositions: Sequence[Mapping[str, Any]] | None,
    issue_count: int,
) -> list[Violation]:
    """校验条目处置声明，核心是让**静默删除在结构上不可能**。

    只靠"重写全文"的修法，模型没重新写出来的条目就无声地消失了——实测 26 场里
    这样丢掉了 75 条有依据内容。这条不变量把问题变成可判定的：

    * 基础稿里存在、修订稿里找不到的条目 ∈ 必须逐条给出 delete 声明（否则
      `silent_deletion`）；
    * 每条 delete / replace 必须写明起因于哪条审查意见并附原文证据（否则
      `unjustified_deletion`）。

    该机制与具体字段无关：PRIMARY_TEXT_KEY 决定"同一条目"的判据，
    新增字段只加一行。
    """
    problems: list[Violation] = []
    declared_deletes: list[str] = []
    for index, raw in enumerate(dispositions or []):
        if not isinstance(raw, Mapping):
            continue
        op = str(raw.get("op") or "")
        if op not in DISPOSITION_OPS:
            problems.append(Violation(kind="unknown_disposition_op", path=f"dispositions[{index}]", detail=op))
            continue
        if op in {"delete", "replace"}:
            reason = raw.get("reason_issue_index", -1)
            if not isinstance(reason, int) or reason < 0 or reason >= max(issue_count, 0):
                problems.append(Violation(
                    kind="unjustified_deletion", path=f"dispositions[{index}]",
                    detail=f"{op} 未指向一条具体的审查意见",
                ))
            evidence = raw.get("evidence") or []
            if not [e for e in evidence if isinstance(e, Mapping) and str(e.get("quote") or "").strip()]:
                problems.append(Violation(
                    kind="unjustified_deletion", path=f"dispositions[{index}]",
                    detail=f"{op} 未附原文证据",
                ))
        if op == "delete":
            declared_deletes.append(_norm(str(raw.get("target_text") or "")))

    if not isinstance(base, Mapping) or not isinstance(refined, Mapping):
        return problems

    for field_name in FIELD_ORDER:
        base_items = base.get(field_name)
        if field_name == "summary":
            base_items = [{"summary": base_items}] if isinstance(base_items, str) else []
        refined_items = refined.get(field_name)
        if field_name == "summary":
            refined_items = [{"summary": refined_items}] if isinstance(refined_items, str) else []
        if not isinstance(base_items, list):
            continue
        if not isinstance(refined_items, list):
            refined_items = []
        refined_texts = [_primary_text(field_name, item) for item in refined_items]
        refined_pool: set[str] = set()
        for text in refined_texts:
            refined_pool |= textsim.content_grams(text)
        for index, item in enumerate(base_items):
            key = _primary_text(field_name, item)
            if not key:
                continue
            # "还在"的判据用共享谓词：同一件事（允许改写）或措辞被合并吸收都算在
            if textsim.matches_any(key, refined_texts):
                continue
            raw_key = " ".join(
                str(item.get(k) or "") for k in PRIMARY_TEXT_KEY.get(field_name, ("content",))
            ) if isinstance(item, Mapping) else ""
            if textsim.is_covered_by(textsim.content_grams(raw_key), refined_pool):
                continue
            if textsim.matches_any(raw_key or key, declared_deletes):
                continue
            problems.append(Violation(
                kind="silent_deletion",
                path=f"{field_name}[{index}]",
                detail=(
                    "基础稿中的该条目在修订稿里消失，且没有任何 delete 处置声明："
                    + str(item.get("content") or item.get("title") or item.get("summary") or "")[:60]
                ),
            ))
    return problems


# --------------------------------------------------------------------------- #
# 从契约表派生模型约束
# --------------------------------------------------------------------------- #

FIELD_ORDER = ("summary", "topics", "viewpoints", "decisions", "pending_items", "risks")
LIST_FIELDS = ("topics", "viewpoints", "decisions", "pending_items", "risks")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")

# 各字段除了"类型化槽位"之外的固定字段。槽位由 SLOT_KINDS 提供，
# 因此这张表只描述不随槽位类型变化的部分。
BASE_ITEM_FIELDS: dict[str, tuple[str, ...]] = {
    "topics": ("title", "summary"),
    "viewpoints": ("speaker", "viewpoint"),
    "decisions": ("content", "basis"),
    "pending_items": ("content",),
    "risks": ("content", "level", "suggestion"),
}

_ITEM_PROVENANCE_FIELDS = ("origin", "evidence")


def sourced_value_schema() -> dict:
    """类型化值槽的 JSON Schema。所有槽位共用，不区分字段。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "basis": {"type": "string", "enum": list(BASES)},
            "normalized": {"type": "string"},
            "anchor": {"type": "string"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"quote": {"type": "string"}, "speaker": {"type": "string"}},
                    "required": ["quote", "speaker"],
                },
            },
        },
        "required": ["text", "basis", "normalized", "anchor", "evidence"],
    }


def item_json_schema(field_name: str) -> dict:
    """某一列表字段的条目 schema。槽位由表驱动，固定字段由 BASE_ITEM_FIELDS 提供。"""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for key in BASE_ITEM_FIELDS[field_name]:
        if key == "level":
            properties[key] = {"type": "string", "enum": list(RISK_LEVELS)}
        else:
            properties[key] = {"type": "string"}
        required.append(key)
    for slot, kind in SLOT_KINDS.get(field_name, {}).items():
        properties[slot] = sourced_value_schema()
        required.append(slot)
    properties["origin"] = {"type": "string", "enum": list(ORIGINS)}
    required.append("origin")
    properties["evidence"] = {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"quote": {"type": "string"}, "speaker": {"type": "string"}},
            "required": ["quote", "speaker"],
        },
    }
    required.append("evidence")
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        },
    }


def minutes_json_schema() -> dict:
    """整份纪要的 JSON Schema，完全由契约表派生。

    模型约束、校验器与表现适配都读同一张表，因此三者不可能互相漂移——
    以前"提示词里写了但 schema 里没有"或反之这类问题在结构上消失。
    """
    properties: dict[str, Any] = {"summary": {"type": "string"}}
    for field_name in LIST_FIELDS:
        properties[field_name] = item_json_schema(field_name)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(FIELD_ORDER),
    }


def coerce_item(item: Any) -> Any:
    """把一条条目收敛到当前契约的槽位名与类型。**幂等**。

    存在的理由不是兼容测试，而是防数据丢失：模型或被管理员改过的提示词模板可能仍
    返回 `owner_suggestion` / `deadline_suggestion` 这类旧键名。若不识别，类型化槽位
    会**静默变成空值**——正是本层要消除的那类失败。因此契约显式接受自己的前身形态。
    """
    if not isinstance(item, Mapping):
        return item
    coerced: dict[str, Any] = {}
    for key, value in item.items():
        if key == "owner_suggestion":
            coerced.setdefault("owner", value)
        elif key == "deadline_suggestion":
            coerced.setdefault("deadline", value)
        else:
            coerced[key] = value
    for slot in ("owner", "deadline", "raised_by"):
        if slot not in coerced:
            continue
        raw = coerced[slot]
        if isinstance(raw, SourcedValue):
            continue
        if isinstance(raw, Mapping):
            coerced[slot] = SourcedValue.model_validate(raw)
            continue
        text = "" if raw is None else str(raw).strip()
        if not text or timex.is_non_assertion(text):
            coerced[slot] = SourcedValue(text=text, basis="not_mentioned")
            continue
        parsed = timex.parse_time(text)
        if parsed.basis:
            coerced[slot] = SourcedValue(
                text=text, basis=parsed.basis, normalized=parsed.normalized, anchor=parsed.anchor,
            )
        else:
            # 不承载时间语义的文本如实保留并标为 stated，交由校验器报 slot_type_mismatch，
            # 而不是在这里丢弃
            coerced[slot] = SourcedValue(text=text, basis="stated")
    coerced.setdefault("origin", "stated")
    # 列表字段的形态收敛：模型常把空数组写成空字符串或 null。
    # 实测一次 `MINUTES_MERGE` 就因为 `evidence: ""` 被 Pydantic 拒绝，
    # 整个 11k 字的纪要生成随之失败——一个空字符串毁掉整块成果。
    # 这与槽位的形态收敛是同一类问题：契约要能接住模型对自己声明结构的近义写法。
    evidence = coerced.get("evidence")
    if evidence is None or isinstance(evidence, str):
        coerced["evidence"] = []
    elif not isinstance(evidence, list):
        coerced["evidence"] = list(evidence) if isinstance(evidence, tuple) else []
    # 每条证据元素同样可能是近义写法
    coerced["evidence"] = [
        {"quote": str(e.get("quote") or ""), "speaker": str(e.get("speaker") or "")}
        if isinstance(e, Mapping) else {"quote": str(e or ""), "speaker": ""}
        for e in coerced["evidence"]
    ]
    if coerced.get("origin") not in ORIGINS:
        coerced["origin"] = "stated"
    return coerced


class _ContractItem(BaseModel):
    """条目模型基类：进入字段校验前先做一次形态收敛。"""

    model_config = {"extra": "allow"}

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_shape(cls, data: Any) -> Any:
        return coerce_item(data)


def minutes_pydantic_models() -> tuple[dict[str, type[BaseModel]], type[BaseModel]]:
    """与 JSON Schema 同源的 Pydantic 模型。返回 (字段名->条目模型, 整份纪要模型)。"""
    from typing import Literal as _Literal

    from pydantic import create_model

    item_models: dict[str, type[BaseModel]] = {}
    for field_name in LIST_FIELDS:
        fields: dict[str, Any] = {}
        for key in BASE_ITEM_FIELDS[field_name]:
            if key == "level":
                fields[key] = (_Literal[RISK_LEVELS], ...)  # type: ignore[valid-type]
            else:
                fields[key] = (str, ...)
        for slot in SLOT_KINDS.get(field_name, {}):
            fields[slot] = (SourcedValue, Field(default_factory=SourcedValue))
        fields["origin"] = (_Literal[ORIGINS], "stated")  # type: ignore[valid-type]
        fields["evidence"] = (list[Evidence], Field(default_factory=list))
        model_name = "".join(part.capitalize() for part in field_name.split("_")) + "Item"
        item_models[field_name] = create_model(  # type: ignore[call-overload]
            model_name, __base__=_ContractItem, **fields,
        )

    root_fields: dict[str, Any] = {"summary": (str, "")}
    for field_name in LIST_FIELDS:
        root_fields[field_name] = (
            list[item_models[field_name]],  # type: ignore[valid-type]
            Field(default_factory=list),
        )
    root = create_model("MinutesResult", **root_fields)  # type: ignore[call-overload]
    return item_models, root


# 判定"改写稿把内容整体归零"的阈值：初稿至少有这么多列表条目时，"五个列表字段全空"
# 才被视为异常而不是正常的清空。
CONTENT_EMPTY_MIN_BASELINE_ITEMS = 5


def list_item_count(output: Mapping[str, Any] | None) -> int:
    """五个列表字段的条目总数。"""
    if not isinstance(output, Mapping):
        return 0
    return sum(len(output.get(field) or []) for field in LIST_FIELDS)


def is_content_empty_rewrite(
    working: Mapping[str, Any] | None,
    revised: Mapping[str, Any] | None,
    *,
    min_baseline_items: int = CONTENT_EMPTY_MIN_BASELINE_ITEMS,
) -> bool:
    """改写稿是不是「结构合法但内容整体没了」。

    **这是一次真实事故的护栏。** 实测（2026-09-25，vcsum_21）：模型只返回了
    `{"summary": ...}`，而 `minutes_pydantic_models()` 给五个列表字段都设了默认空列表，
    于是 `model_validate` 通过、结构校验通过、运行被判定为「自检通过、得分 90」，
    状态停在 WAITING_CONFIRMATION——**等人点一下确认，初稿的 63 条就全没了**。

    危险的不是模型偶尔返回残缺 JSON，而是**默认值把「模型没给」变成了「模型给了空的」**：
    后续每一个环节都看到"合法的空"，没有任何一处会报警。

    因此这里单独判一次：初稿里本来有内容（≥ min_baseline_items 条），而改写稿五个列表
    字段全空 → 视为**失败的改写**，由调用方报错而不是当成一版内容更精简的稿子。
    """
    return (list_item_count(working) >= min_baseline_items
            and list_item_count(revised) == 0)


def unprovided_content_fields(model: Any) -> tuple[str, ...]:
    """模型**原始输出里根本没出现**的内容字段名（不是"出现了但是空的"）。

    为什么需要这一条，而不是继续用 `is_content_empty_rewrite`：那一条是**内容级代理**
    ——它拿两份输出的条目数相比，**只有在"有参照物"时才成立**（重写步有初稿、合并步有
    各分段）。单分块生成没有参照物，因此那条判据在单分块路径上根本无从下手。

    而事故的根因不在内容层，在**默认值层**：`minutes_pydantic_models()` 给 `summary`
    默认空串、给五个列表字段默认空数组，于是 `model_validate({"summary": ...})` 通过，
    "模型没给"被补成"模型给了空的"，后续每一环看到的都是合法的空。
    Pydantic 记录了这个区别（`model_fields_set` 只含输入里真正出现过的键），
    所以这里读的是根因，而不是再找一个代理。
    """
    provided = getattr(model, "model_fields_set", None)
    if provided is None:
        return ()
    return tuple(name for name in FIELD_ORDER if name not in provided)


def is_unprovided_content_generation(model: Any) -> bool:
    """这次生成是不是"模型一个列表字段都没给"——即事故形态本身。

    判据是**五个列表字段在原始输出里全部缺席**，与它们是否为空无关：

    * 个别字段缺席（例如会议确实没有决策）不算异常，照常交付；
    * 五个列表字段全缺席意味着模型返回的压根不是一份完整纪要，与内容多少无关。

    保守之处（写下来免得以后被当成疏漏）：如果某场会议真的什么都没产生，
    模型按提示词与 schema 仍应显式给出空数组；真出现"全缺席"时按失败处理是安全方向
    ——失败会留下消息并可重试，而被默认值补出来的空纪要会静默删掉用户已有的内容。
    """
    return all(name in unprovided_content_fields(model) for name in LIST_FIELDS)


# --------------------------------------------------------------------------- #
# 从证据策略表派生审查意见结构
# --------------------------------------------------------------------------- #

ISSUE_TYPES = tuple(ISSUE_EVIDENCE_POLICY)

EVIDENCE_FIELD_HINT = {
    EVIDENCE_POSITIVE: (
        "必须填 source_span：从转写里原样摘出「证明该信息确实存在」的那一段。"
        "缺失类规则的成立前提是「原文有、纪要没有」，因此必须先把前提摆出来；"
        "摘不出原文依据的缺失类意见视为无效。"
    ),
    EVIDENCE_NEGATIVE: (
        "必须填 target_text：被判定为无依据的那段纪要文字原样照抄。"
        "无依据类规则的成立前提是「原文确实没有」，target_text 会被回查；"
        "若它能在转写里定位到，该意见视为误判。"
    ),
    EVIDENCE_NONE: (
        "不需填证据类字段：该类意见与原文出处无关，只评价可执行性或表述质量。"
    ),
}


def review_json_schema() -> dict:
    """审查结果结构。issue 的枚举与证据要求全部由 ISSUE_EVIDENCE_POLICY 派生。

    严格 JSON Schema 无法表达"某个字段在特定取值下必填"，所以两个证据字段
    恒定为必填但允许空串，**条件性要求由 check_issue_evidence 在取值层面强制**。
    提示词里逐类说明该填什么，说明文字同样来自策略表——三处同源。
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "passed": {"type": "boolean"},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "field": {"type": "string", "enum": list(FIELD_ORDER)},
                        "index": {"type": "integer"},
                        "issue_type": {"type": "string", "enum": list(ISSUE_TYPES)},
                        "detail": {"type": "string"},
                        "suggestion": {"type": "string"},
                        "source_span": {
                            "type": "string",
                            "description": EVIDENCE_FIELD_HINT[EVIDENCE_POSITIVE],
                        },
                        "target_text": {
                            "type": "string",
                            "description": EVIDENCE_FIELD_HINT[EVIDENCE_NEGATIVE],
                        },
                    },
                    "required": [
                        "field", "index", "issue_type", "detail", "suggestion",
                        "source_span", "target_text",
                    ],
                },
            },
            "conclusion": {"type": "string"},
        },
        "required": ["passed", "score", "issues", "conclusion"],
    }


def issue_evidence_guide() -> str:
    """按策略表生成给模型看的证据要求说明，附在审查提示词后。"""
    lines = ["各类问题必须提供的依据："]
    for issue_type, policy in ISSUE_EVIDENCE_POLICY.items():
        lines.append(f"- {issue_type}：{EVIDENCE_FIELD_HINT[policy]}")
    return "\n".join(lines)

def enforce_preservation(
    base: Mapping[str, Any] | None,
    refined: Mapping[str, Any] | None,
    dispositions: Sequence[Mapping[str, Any]] | None,
    transcript: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把"保默认"从一句提示词变成**结构性保证**。

    实测：新版配置下静默删除仍有约 3 条/场，与旧配置持平——因为此前只做了**测量**
    （`validate_dispositions` 能算出删了多少），产品侧 refine 依然是"重写全文"，
    没有任何机制强制保留。

    这个函数补上那个机制：基础稿里存在、修订稿里消失、且**没有对应 delete 声明**的条目，
    一律**原样恢复**。于是"静默删除"这一状态在结构上不存在——条目要么被显式声明删除
    （带理由与证据），要么留在结果里。

    代价是一次纯内存的集合运算，不需要额外模型调用，也不改变生成质量。

    **证据门控（2026-09-26 加入，`transcript` 非空时生效）**：只恢复**本来有依据**的条目。
    实测依据：契约 v2 那批产物里 80 条消失条目中 **77 条（96%）带有能在转写中逐条定位的
    证据**——也就是说被删掉的绝大多数是**有依据的内容**（过度修订），只有 3 条是真正
    无依据的。若一味"全部恢复"，产品就再也删不掉无依据内容（丢失核心功能）；
    若只按声明判断，模型又极少给声明（旧构建 6 次重写 0 条声明，全量 25/26 场无声明）。
    因此判据改为看**条目自己的证据**：

    * 有可定位证据 → 无声明就恢复（防止丢掉有依据的内容）；
    * 证据缺失或定位不到 → 允许它在无声明的情况下被丢弃，并记入 `dropped_ungrounded`
      （它本来就该被清掉，这正是产品的核心功能）。

    `transcript` 传空串时退回旧行为（全部恢复），仅供测试与无转写可用的调用点。
    """
    import copy

    result = copy.deepcopy(dict(refined or {}))
    restored: list[dict[str, Any]] = []
    dropped_ungrounded: list[dict[str, Any]] = []
    if not isinstance(base, Mapping):
        return result, {"restored_count": 0, "restored": []}

    declared_deletes: list[str] = []
    for raw in dispositions or []:
        if isinstance(raw, Mapping) and str(raw.get("op") or "") == "delete":
            declared_deletes.append(str(raw.get("target_text") or ""))

    for field_name in FIELD_ORDER:
        base_items = base.get(field_name)
        if field_name == "summary" or not isinstance(base_items, list):
            continue
        refined_items = result.get(field_name)
        if not isinstance(refined_items, list):
            refined_items = []
            result[field_name] = refined_items
        refined_texts = [_primary_text(field_name, item) for item in refined_items]
        refined_pool: set[str] = set()
        for text in refined_texts:
            refined_pool |= textsim.content_grams(text)

        for item in base_items:
            if not isinstance(item, Mapping):
                continue
            key = _primary_text(field_name, item)
            if not key:
                continue
            # 还在（同一件事，允许改写；或措辞被合并吸收）就不动
            if textsim.matches_any(key, refined_texts):
                continue
            raw_key = " ".join(
                str(item.get(k) or "") for k in PRIMARY_TEXT_KEY.get(field_name, ("content",))
            )
            if textsim.is_covered_by(textsim.content_grams(raw_key), refined_pool):
                continue
            # 有显式删除声明也不动
            if textsim.matches_any(raw_key or key, declared_deletes):
                continue
            # 证据门控：本来就没有可定位证据的条目，允许无声明丢弃（它不该留在纪要里）
            if transcript:
                source_norm = _norm(transcript)
                evidence = [e for e in (item.get("evidence") or []) if isinstance(e, Mapping)]
                locatable = [e for e in evidence
                             if _findable(str(e.get("quote") or ""), source_norm)]
                if not locatable:
                    dropped_ungrounded.append({
                        "field": field_name,
                        "text": str(item.get("content") or item.get("title") or "")[:120],
                        "evidence_count": len(evidence),
                    })
                    continue
            refined_items.append(copy.deepcopy(dict(item)))
            refined_texts.append(key)
            refined_pool |= textsim.content_grams(key)
            restored.append({
                "field": field_name,
                "text": str(item.get("content") or item.get("title") or "")[:120],
            })

    return result, {
        "restored_count": len(restored),
        "restored": restored,
        "dropped_ungrounded_count": len(dropped_ungrounded),
        "dropped_ungrounded": dropped_ungrounded,
        "note": (
            "被恢复的条目是「消失、无删除声明、且本来有可定位证据」的那些——"
            "丢掉它们就是过度修订。"
            "反之，证据缺失或定位不到的条目允许在无声明的情况下被丢弃："
            "那正是产品要清掉的无依据内容，全盘恢复会丢掉核心功能。"
        ),
    }

def issue_support_prompt() -> tuple[str, str]:
    """逐条判定"所给依据是否**支撑**这条意见"的提示词（系统，用户模板）。

    与门控的关系：`gate_issues_by_evidence` 判的是"依据能不能**定位**"（字面子串），
    而这一层判的是"依据能不能**支撑**该判定"。两者都要有——实测：独立模型族在业界口径下
    只认可 25% 的 `UNSUPPORTED` 意见，而按字面定位它们全都"有依据"。
    因此定位通过之后再过一层支撑判定，审查才真正与业界口径一致。
    """
    system = (
        "你在复核一份会议纪要自检提出的问题清单。逐条判断：**所给的原文依据是否支撑这条意见**。\n"
        "只输出 JSON：{\"verdicts\": [{\"index\": 1, \"supported\": true, \"why\": \"一句话\"}]}\n"
        "判定规则：\n"
        "1. 依据只沾到边缘、或与该意见的判定无关 → supported = false；\n"
        "2. 意见声称「原文缺失」但依据本身就说到了该内容 → false；\n"
        "3. 意见声称「无依据」但依据其实支撑了被判定的文本 → false；\n"
        "4. 依据确实能让人接受该判定 → true。数量必须与问题条数一致。"
    )
    user = (
        "【转写窗口（审查看到的那一段）】\n{transcript}\n\n"
        "【问题清单】\n{issues}\n\n请逐条判定。"
    )
    return system, user


def issue_support_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "index": {"type": "integer"},
                        "supported": {"type": "boolean"},
                        "why": {"type": "string"},
                    },
                    "required": ["index", "supported", "why"],
                },
            }
        },
        "required": ["verdicts"],
    }


def filter_issues_by_support(
    issues: Sequence[Mapping[str, Any]], verdicts: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """按支撑判定筛意见。**缺失的判定按"不支撑"处理并标明**——宁可少改一版，
    也不要让一条没被验证过的意见推动删除；这与"证据不足即挡下"是同一个原则。"""
    by_index: dict[int, Mapping[str, Any]] = {}
    for entry in verdicts or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("index"), int):
            by_index[int(entry["index"])] = entry
    kept: list[Mapping[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for position, issue in enumerate(issues or [], start=1):
        verdict = by_index.get(position)
        if verdict is None:
            dropped.append({"issue": dict(issue) if isinstance(issue, Mapping) else issue,
                            "reasons": ["missing_support_verdict"], "detail": "未给出支撑判定"})
            continue
        if bool(verdict.get("supported")):
            kept.append(issue)
        else:
            dropped.append({"issue": dict(issue) if isinstance(issue, Mapping) else issue,
                            "reasons": ["premise_does_not_support_issue"],
                            "detail": str(verdict.get("why") or "")[:300]})
    return {"kept": kept, "dropped": dropped}


def refine_json_schema() -> dict:
    """重写步的结构：纪要本体 + 条目处置声明。

    与生成阶段共用同一份纪要 schema，另加 `dispositions`。之所以只给重写步加：
    生成阶段是从零起草，没有"保留/删除"的概念；重写阶段才有，而且必须有——
    否则代码无法区分「合法删除无依据内容」与「静默丢掉了有依据内容」，
    而前者是产品核心功能，后者是实测 3 条/场的缺陷。

    字段与 `Disposition` 模型、`validate_dispositions`、`enforce_preservation`
    同源：schema 派生自同一张声明，校验与执行读同一份数据。
    """
    base = minutes_json_schema()
    disposition = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "field": {"type": "string", "enum": list(LIST_FIELDS)},
            "op": {"type": "string", "enum": list(DISPOSITION_OPS)},
            "target_text": {"type": "string"},
            "reason_issue_index": {"type": "integer"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"quote": {"type": "string"}, "speaker": {"type": "string"}},
                    "required": ["quote", "speaker"],
                },
            },
        },
        "required": ["field", "op", "target_text", "reason_issue_index", "evidence"],
    }
    merge = dict(base)
    merge["properties"] = dict(base["properties"], dispositions={"type": "array", "items": disposition})
    merge["required"] = list(base["required"]) + ["dispositions"]
    return merge

