"""Offline lexical grounding checks for generated minutes.

The product's central claim is that its review step removes content with no
basis in the transcript. Measuring that claim needs a *grounding* judgement for
every generated item. Human gold does not exist for Chinese meeting minutes
(see ``docs`` in ``METRICS_RESEARCH.md``), so this module builds a deliberately
narrow proxy: it extracts only high-precision *asserting* atoms -- dates,
quantities, named entities -- and asks whether each one has any lexical trace in
the transcript.

Design rules, in priority order:

1. **Never count an explicit non-assertion as a fabrication.** ``无``, ``待确认``
   and ``未明确提及`` are correct outputs, not unsupported claims, so they are
   classified as ``declared_unknown`` and excluded from the denominator.
2. **Err toward "supported".** A false *support* judgement only makes the
   reported unsupported rate more conservative. Every matching rule here is
   therefore permissive (separator-insensitive skeletons, Chinese/ASCII numeral
   equivalence, substring containment).
3. **Report every flagged atom with its surrounding text**, so a reviewer can
   audit the false positives instead of trusting the aggregate.

This is a proxy, not a factuality metric. It cannot see paraphrase, implication
or correct derivation. Its output must be labelled ``需复核`` in any report.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

# A value that explicitly declines to assert anything. Not an atom.
# 来历：为观测"显式未知被算成无依据原子"加的——旧版把 `无`/`待确认` 当断言值，
# 于是"答案正确"被记成"无依据"（见 README 校准发现第 3 条）。
# 复核：`deadline_state` 把命中这些标记的值单列为 not_mentioned；若该分类下出现
# 大批非"未知"语义的取值，说明词表在吃别的东西。
DECLARED_UNKNOWN_MARKERS = (
    "无", "待确认", "待定", "未明确", "未提及", "未指定", "暂无", "尚未",
    "不确定", "未确定", "待补充", "待核实", "需确认", "没有明确", "暂无明确",
    "n/a", "na", "none", "null", "-", "—", "不涉及", "无需",
)

# A time expression that asserts a period but not a checkable point in time.
# Derived phrasing is common and legitimate, so these are reported separately
# rather than counted as unsupported.
# 来历：为观测"派生时段表述被算成无依据"加的——`持续推进`、`尽快` 不是可核查断言。
# 复核：`deadline_state` 的 open_ended 类目；若其中混入可解析的具体时间，说明词表过宽。
VAGUE_TIME_MARKERS = (
    "持续推进", "持续进行", "持续执行", "持续推进中", "尽快", "立即", "后续",
    "长期", "常态化", "逐步", "适时", "择期", "随时", "日常", "定期",
    "进行中", "落实中", "跟进中", "过程中", "期间", "之后", "之前",
)

# Generic role/relational nouns that can be derived from context rather than
# quoted. Kept out of the hard-entity class so the metric does not overclaim.
# 来历：为观测"通用角色词被切成杜撰人名"加的（`全行业参与者` 曾被切成 `全行业参`）。
# 复核：entity 类的无依据率与 `examples_for_audit`；这张表越宽，entity 类越保守
# （代理本就偏向"有支撑"），因此它只影响召回、不构成质量结论。
GENERIC_ENTITY_WORDS = (
    "主持人", "主讲人", "嘉宾", "专家", "老师", "同学", "家长", "考生", "学生",
    "团队", "小组", "组织方", "主办方", "承办方", "参会人", "与会者", "发言人",
    "全体", "大家", "双方", "各方", "相关方", "负责人", "责任人", "所有人",
    "公司", "集团", "部门", "机构", "单位", "委员会", "理事会", "基金会",
    "政府", "学校", "企业", "银行", "医院", "中心", "处", "科", "室", "组",
    "代表", "同事", "人士", "人员", "成员", "伙伴", "朋友", "领导", "者", "方",
    "厂商", "制造商", "供应商", "服务商", "运营商", "提供商", "参与方", "参与者",
    "商", "协会", "高校", "院校", "行业", "领域", "各界", "海外", "业内",
    "学长", "学姐", "学弟", "学妹", "同学", "管理层", "事业部", "部门",
)

# A segment beginning with one of these is relational or anaphoric
# ("其基金会同事", "各终端厂商"), so the exact string need not appear.
# 来历：与 GENERIC_ENTITY_WORDS 同源的观测——关系/回指短语（`其基金会同事`）不是具名实体，
# 不要求字面出现。
# 复核：同 GENERIC_ENTITY_WORDS。
RELATIONAL_ENTITY_PREFIXES = (
    "相关", "有关", "其他", "其它", "各类", "各自", "本人", "该", "此", "其",
    "所属", "上述", "以上", "所", "同", "各", "全",
)

# Domain words that make a phrase non-specific even when they are not the
# trailing morpheme ("行业HR", "行业专家"). Checked by containment.
# 来历：同上，处理"领域词出现在词中"（`行业HR`、`行业专家`），按包含匹配。
# 复核：同上。
GENERIC_ENTITY_FRAGMENTS = (
    "行业", "领域", "业界", "各界", "业内", "海内外", "各",
)

# Trailing markers of an open-ended enumeration; the enumerated example still
# has to be checkable, so the marker is removed before matching rather than
# used to excuse the whole segment.
# 来历：为观测"开放列举被整体判为杜撰"加的。
# **注意它的处理方式不是豁免**：移除后缀后仍要求被列举的例子可逐字核验。
# 复核：开放列举句的抽样核对（`examples_for_audit`）。
OPEN_ENUMERATION_SUFFIXES = ("等等", "等", "之类", "等类")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "貳": 2,
              "贰": 2, "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
              "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}

_FULL_DATE_RE = re.compile(
    r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
)
_MD_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_YEAR_RE = re.compile(r"(20\d{2})\s*年")
_SLASH_MD_RE = re.compile(r"(?<![\d/])(\d{1,2})\s*/\s*(\d{1,2})(?![\d/])")
_QUANTITY_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?|[零〇一壹二两貳贰三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟]+)"
    r"\s*(份|个|人|位|名|天|小时|分钟|周|个月|月|年|次|条|台|套|页|版|件|项|款|"
    r"场|轮|步|阶段|%|％|倍)"
)
_SPLIT_RE = re.compile(r"[、,，;；/\s]+|(?:以及)|(?:及其)|(?:暨)")
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")

# Connectors that may also occur inside a word (参与, 涉及). They are not used to
# decide how many entities an owner names -- doing that truncated 全行业参与者
# into a spurious name -- but they do decide whether every named part is present
# in the transcript, which is the question that matters for support.
_CONNECTOR_SPLIT_RE = re.compile(r"[、,，;；/\s]+|(?:以及)|(?:及其)|(?:暨)|及|和|与|跟")


def normalize(text: str) -> str:
    """NFC, keep letter/number code points, drop whitespace and punctuation."""
    if not isinstance(text, str):
        raise TypeError("normalize expects a string")
    normalized = unicodedata.normalize("NFC", text)
    return "".join(
        char for char in normalized if unicodedata.category(char)[0] in "LN"
    ).casefold()


def digit_skeleton(text: str) -> str:
    """Collapse CJK date separators so date variants compare equal.

    ``2026年9月28日`` and ``2026-09-28`` and ``2026/9/28`` all become
    ``2026-9-28``. Runs of separators collapse to one dash, and leading zeros
    are stripped per component so zero-padded and unpadded forms agree.
    """
    if not isinstance(text, str):
        raise TypeError("digit_skeleton expects a string")
    out = []
    for char in unicodedata.normalize("NFKC", text):
        if char.isdigit():
            out.append(char)
        elif char in "年月日/-._·":
            out.append("-")
    skeleton = re.sub(r"-+", "-", "".join(out)).strip("-")
    components = []
    for part in skeleton.split("-"):
        if part.isdigit():
            components.append(str(int(part)))
        elif part:
            components.append(part)
    return "-".join(components)


def chinese_numeral_to_int(token: str) -> int | None:
    """Convert a Chinese numeral such as ``十`` or ``二十三`` to an integer.

    Returns None when the token is not a numeral this converter understands.
    Only values up to 9999 are supported, which covers meeting quantities.
    """
    if not token:
        return None
    if token.isdigit():
        return int(token)
    total, section, seen = 0, 0, False
    for char in token:
        if char in _CN_DIGITS:
            section = _CN_DIGITS[char]
            seen = True
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            section = (section or 1) * unit
            total += section
            section = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + section


def normalize_numerals(text: str) -> str:
    """Rewrite Chinese numerals as Arabic so quantity bigrams compare equal."""
    def replace(match: re.Match[str]) -> str:
        value = chinese_numeral_to_int(match.group(0))
        return str(value) if value is not None else match.group(0)

    return re.sub(r"[零〇一壹二两貳贰三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟]+", replace, text)


_KEEP_CHARS_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def canonicalize_quantities(text: str) -> str:
    """Unify quantity notation so equal assertions compare equal.

    Four notation differences caused most of the quantity false positives in the
    audit, and all four are notation rather than fabrication:

    * the transcript writes 百分之三十 where the model writes 30%;
    * the transcript writes 五位 where the model writes 5人;
    * the transcript writes 个月 where the model writes 月;
    * ``%`` is punctuation, so the standard text normaliser deletes it entirely,
      which made ``30%`` unmatchable against a transcript that literally said
      ``占比30%``.

    Percentages are therefore rewritten to the token ``pct``, which survives the
    character filter the same way a Chinese character does. Substitutions are
    anchored to a preceding digit so ordinary words are not rewritten.
    """
    result = re.sub(r"百分之(\d+(?:\.\d+)?)", r"\1pct", text)
    result = re.sub(r"(\d+(?:\.\d+)?)\s*[%％]", r"\1pct", result)
    result = re.sub(r"(\d+(?:\.\d+)?)\s*(?:百分点|百分比)", r"\1pct", result)
    result = re.sub(r"(\d+)\s*个?\s*(?:位|名|员)", r"\1人", result)
    result = re.sub(r"(\d+)\s*个月", r"\1月", result)
    result = re.sub(r"(\d+)\s*(?:个小时|钟头)", r"\1小时", result)
    return result


def canonical_quantity_skeleton(number: str, unit: str) -> str:
    """Apply the same notation normalisation used for the transcript."""
    return _KEEP_CHARS_RE.sub("", canonicalize_quantities(f"{number}{unit}"))


def _is_declared_unknown(value: str) -> bool:
    stripped = normalize(value)
    if not stripped:
        return True
    return any(marker in stripped for marker in DECLARED_UNKNOWN_MARKERS)


def _is_vague_time(value: str) -> bool:
    return any(marker in value for marker in VAGUE_TIME_MARKERS)


def extract_date_atoms(value: str) -> list[dict[str, str]]:
    """Extract date assertions, each with a skeleton of acceptable forms."""
    atoms: list[dict[str, str]] = []
    consumed: list[tuple[int, int]] = []

    for match in _FULL_DATE_RE.finditer(value):
        year, month, day = match.group(1), str(int(match.group(2))), str(int(match.group(3)))
        atoms.append({
            "kind": "date",
            "surface": match.group(0),
            "skeleton": f"{year}-{month}-{day}",
            "span": list(match.span()),
        })
        consumed.append(match.span())

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < end and start < span[1] for start, end in consumed)

    for match in _MD_DATE_RE.finditer(value):
        if overlaps(match.span()):
            continue
        atoms.append({
            "kind": "date",
            "surface": match.group(0),
            "skeleton": f"{int(match.group(1))}-{int(match.group(2))}",
            "span": list(match.span()),
        })
        consumed.append(match.span())

    for match in _SLASH_MD_RE.finditer(value):
        if overlaps(match.span()):
            continue
        atoms.append({
            "kind": "date",
            "surface": match.group(0),
            "skeleton": f"{int(match.group(1))}-{int(match.group(2))}",
            "span": list(match.span()),
        })
        consumed.append(match.span())

    for match in _YEAR_RE.finditer(value):
        if overlaps(match.span()):
            continue
        atoms.append({
            "kind": "date",
            "surface": match.group(0),
            "skeleton": match.group(1),
            "span": list(match.span()),
        })

    return atoms


def date_spans(value: str) -> list[tuple[int, int]]:
    """Character spans already claimed by a date, so quantities do not re-read them."""
    return [tuple(atom["span"]) for atom in extract_date_atoms(value) if "span" in atom]


# A numeral directly after one of these is an ordinal or indefinite reference
# (第3条, 另一条, 每一条), not an assertion about a count.
# 来历：为观测"第/另/同/这/那/某/每 引入的数字被当成计数"加的（"第3季度"不是"3 个"）。
# 复核：quantity 类无依据率及其 `examples_for_audit`。
_QUANTITY_REFERENCE_PREFIXES = ("第", "另", "同", "这", "那", "某", "每", "任", "前", "后")


def extract_quantity_atoms(value: str, *, exclude: Sequence[tuple[int, int]] = ()) -> list[dict[str, str]]:
    """Extract ``number + unit`` bigrams, which are tight enough to check.

    Three deliberate narrowings keep this class precise, because a false
    "unsupported" flag costs far more credibility than a missed one:

    * spans already consumed by a date are skipped, so ``2026年9月28日`` does not
      also yield a bogus ``2026年`` quantity;
    * a numeral introduced by 第/另/同/这/那/某/每/任 is a reference, not a count;
    * a bare Chinese ``一`` measure phrase (``一条``, ``一个``) is excluded, since
      in meeting prose it is overwhelmingly a classifier rather than a quantity.
      Arabic digits are always kept, so ``1份`` still counts.
    """
    atoms: list[dict[str, str]] = []
    for match in _QUANTITY_RE.finditer(value):
        start, end = match.span()
        if any(start < other_end and other_start < end for other_start, other_end in exclude):
            continue
        if start > 0 and value[start - 1] in _QUANTITY_REFERENCE_PREFIXES:
            continue
        number = chinese_numeral_to_int(match.group(1))
        if number is None:
            continue
        if number == 1 and not match.group(1).isdigit():
            continue
        # ``2020项目`` is a year plus a noun, not a count of twenty-two hundred
        # items. A four-digit year followed by a classifier is almost always a
        # year, so it is skipped rather than reported.
        if 1900 <= number <= 2100 and len(match.group(1)) == 4:
            continue
        unit = match.group(2)
        atoms.append({
            "kind": "quantity",
            "surface": match.group(0),
            "skeleton": canonical_quantity_skeleton(str(number), unit),
            "span": [start, end],
        })
    return atoms


def _is_generic_part(part: str) -> bool:
    """Is this part a role/relational phrase rather than a specific name?

    Containment is used rather than suffix matching because the owner field
    frequently holds a *sentence* instead of a name
    (``主持人统筹安排``, ``具体分享嘉宾依据原文议程安排确定``). Requiring a
    literal trace for those would flag correct output. The cost is that a
    specific name containing a generic word (``中国人民银行``) is treated as
    non-specific, which under-detects rather than over-reports.
    """
    if any(marker in part for marker in RELATIONAL_ENTITY_PREFIXES):
        return True
    if any(fragment in part for fragment in GENERIC_ENTITY_FRAGMENTS):
        return True
    return any(word in part for word in GENERIC_ENTITY_WORDS)


def split_entities(value: str) -> list[dict[str, str]]:
    """Split an owner/speaker value into entity segments with their parts.

    Classification and matching deliberately use different granularity:

    * a **segment** is ``hard`` when at least one of its parts looks like a
      specific named entity, and only ``hard`` segments can be counted as
      fabricated;
    * **support** is required only for those specific parts, so ``相关政府部门``
      or ``行业协会`` never has to appear verbatim, while ``小米及咪咕`` still
      counts as present when both names appear.

    Auditing three successive runs drove both rules: splitting on ``与``/``及``
    truncated ``全行业参与者`` into a spurious name, checking a joined owner
    string as one token flagged ``小米及咪咕`` although both names were present,
    and requiring generic parts to appear verbatim flagged ``科研院所及相关
    政府部门`` although only the generic half was missing.
    """
    without_parentheses = _PAREN_RE.sub(" ", value)
    segments: list[dict[str, str]] = []
    for raw in _SPLIT_RE.split(without_parentheses):
        segment = raw.strip()
        for marker in OPEN_ENUMERATION_SUFFIXES:
            if segment.endswith(marker) and len(segment) > len(marker) + 1:
                segment = segment[: -len(marker)]
                break
        if len(segment) < 2:
            continue
        all_parts = [part for part in _CONNECTOR_SPLIT_RE.split(segment) if len(part) >= 2]
        if not all_parts:
            all_parts = [segment]
        required_parts = [part for part in all_parts if not _is_generic_part(part)]
        segments.append({
            "kind": "entity",
            "surface": segment,
            "class": "hard" if required_parts else "descriptive",
            "parts": all_parts,
            "required_parts": required_parts,
        })
    return segments


def build_transcript_index(transcript: str) -> dict[str, str]:
    """Pre-compute the normalized views of the transcript used for matching."""
    plain = normalize(transcript)
    compact = re.sub(r"\s+", "", transcript)
    return {
        "plain": plain,
        "skeleton": digit_skeleton(transcript),
        "numerals": normalize_numerals(plain),
        # Built from the raw text, not from ``plain``: ``plain`` has already had
        # ``%`` stripped, so percentages could never match against it.
        "quantities": _KEEP_CHARS_RE.sub(
            "", canonicalize_quantities(normalize_numerals(compact))
        ),
    }


def atom_is_supported(atom: Mapping[str, str], index: Mapping[str, str]) -> bool:
    """Is there any lexical trace of this atom in the transcript?

    Matching is intentionally permissive; see the module docstring, rule 2.
    """
    kind = atom.get("kind")
    if kind == "date":
        skeleton = str(atom.get("skeleton", ""))
        if not skeleton:
            return True
        parts = skeleton.split("-")
        candidates = {skeleton}
        if len(parts) == 3:
            year, month, day = parts
            candidates |= {f"{year}-{month}-{day}", f"{year}{month}{day}",
                           f"{int(year)}-{int(month)}-{int(day)}"}
        transcript_skeleton = index.get("skeleton", "")
        return any(candidate in transcript_skeleton for candidate in candidates)

    if kind == "quantity":
        skeleton = str(atom.get("skeleton", ""))
        if not skeleton:
            return True
        return (
            skeleton in index.get("quantities", "")
            or skeleton in index.get("numerals", "")
            or skeleton in index.get("plain", "")
        )

    if kind == "entity":
        # Only the specific parts need a lexical trace; role and relational
        # phrases are derivable and must not force a segment to be flagged.
        required = [str(part) for part in (atom.get("required_parts") or []) if part]
        if not required:
            return True
        return all(
            normalize(part) in index.get("plain", "")
            or normalize(part) in index.get("numerals", "")
            for part in required
        )

    return True


# Per field: which subfields assert checkable content, and how to read them.
#
# **这里的键名有两个来源，必须先解析再读**：契约 v1 把负责人/期限写成裸字符串
# （`owner_suggestion` / `deadline_suggestion`），契约 v2 把它们换成类型化槽位
# （`owner` / `deadline`，值是 `{text, basis, evidence}`）。只按其中一个写，
# 另一代产物上那一类原子会**静默为零**——实测：按 v1 键名读 v2 产物，
# entity 与 time 两类原子全数消失，而报告里看不出是"没有问题"还是"根本没测"。
# 因此下面给出两代键名，并在读取时同时接受字符串与类型化槽位（见 `_slot_text`）。
FIELD_SPEC: dict[str, dict[str, Any]] = {
    "summary": {"text_fields": ("summary",), "time_fields": (), "entity_fields": ()},
    "topics": {"text_fields": ("title", "summary"), "time_fields": (), "entity_fields": ()},
    "viewpoints": {"text_fields": ("viewpoint",), "time_fields": (), "entity_fields": ("speaker",)},
    "decisions": {
        "text_fields": ("content", "basis"),
        "time_fields": ("deadline", "deadline_suggestion"),
        "entity_fields": ("owner", "owner_suggestion"),
    },
    "pending_items": {
        "text_fields": ("content",),
        "time_fields": ("deadline", "deadline_suggestion"),
        "entity_fields": ("owner", "owner_suggestion"),
    },
    "risks": {"text_fields": ("content", "suggestion"), "time_fields": (), "entity_fields": ()},
}

# 类型化槽位里 basis 取这些值时**不算断言**，因此不产原子。
NON_ASSERTING_BASES = frozenset({"not_mentioned", "open_ended", "model_inferred"})


def _slot_text(value: Any) -> tuple[str, str]:
    """把一个槽位读成（文本，basis）。

    兼容两代形态：
    * v1：裸字符串 → 视为 `stated`（与产品升级路径一致）；
    * v2：`{text, basis, ...}` 类型化槽位 → 原样取出 basis。

    basis 交给调用方判断是否产原子：`not_mentioned` 是"原文没提"（正确答案，不是断言），
    `open_ended` 只对时间槽有意义，`model_inferred` 是模型自己的推断（不是原文断言）。
    """
    if isinstance(value, Mapping):
        text = value.get("text")
        basis = value.get("basis")
        return (str(text or "").strip(), str(basis or "stated"))
    if isinstance(value, str):
        return (value.strip(), "stated")
    return ("", "not_mentioned")

LIST_FIELDS = ("topics", "viewpoints", "decisions", "pending_items", "risks")


def analyze_value(value: str) -> dict[str, Any]:
    """Classify one string value into atoms, vague time and declared unknowns."""
    result: dict[str, Any] = {
        "declared_unknown": False,
        "vague_time": False,
        "atoms": [],
    }
    if not isinstance(value, str) or not value.strip():
        result["declared_unknown"] = True
        return result
    if _is_declared_unknown(value):
        result["declared_unknown"] = True
        return result
    if _is_vague_time(value):
        result["vague_time"] = True
    dates = extract_date_atoms(value)
    # A bare year is not treated as checkable. Auditing the dev split showed a
    # single case contributing ten flags for "2026年" solely because the model
    # resolved 今年 correctly: deictic resolution of a relative time expression is
    # right behaviour, and the transcript will never contain the resolved year.
    # A month-and-day date remains checkable, since it is a real assertion.
    precise = [atom for atom in dates if "-" in str(atom.get("skeleton", ""))]
    coarse = [atom for atom in dates if "-" not in str(atom.get("skeleton", ""))]
    result["coarse_date"] = coarse
    result["atoms"] = precise + extract_quantity_atoms(value, exclude=date_spans(value))
    return result


def analyze_item(field: str, item: Any, index: Mapping[str, str]) -> dict[str, Any]:
    """Grounding verdict for one field item (a string for summary, dict otherwise)."""
    spec = FIELD_SPEC[field]
    atoms: list[dict[str, Any]] = []
    entity_atoms: list[dict[str, Any]] = []
    declared_unknown: list[str] = []
    vague_time: list[str] = []
    coarse_date: list[str] = []

    if isinstance(item, str):
        source = {spec["text_fields"][0]: item} if spec["text_fields"] else {}
    elif isinstance(item, Mapping):
        source = item
    else:
        source = {}

    for key in spec["text_fields"]:
        value = source.get(key)
        if isinstance(value, str):
            analysis = analyze_value(value)
            if analysis["declared_unknown"] and value.strip():
                declared_unknown.append(f"{key}:{value.strip()[:24]}")
            if analysis["vague_time"]:
                vague_time.append(f"{key}:{value.strip()[:24]}")
            for atom in analysis.get("coarse_date", []):
                coarse_date.append(f"{key}:{atom['surface']}")
            for atom in analysis["atoms"]:
                atoms.append({**atom, "from": key})

    for key in spec["time_fields"]:
        text, basis = _slot_text(source.get(key))
        if not text:
            continue
        if basis in NON_ASSERTING_BASES:
            # 契约自己声明了"没断言"，不再用词表猜
            if basis == "not_mentioned":
                declared_unknown.append(f"{key}:{text[:24]}")
            else:
                vague_time.append(f"{key}:{text[:24]}")
            continue
        analysis = analyze_value(text)
        if analysis["declared_unknown"]:
            declared_unknown.append(f"{key}:{text[:24]}")
        elif analysis["vague_time"]:
            vague_time.append(f"{key}:{text[:24]}")
        else:
            for atom in analysis.get("coarse_date", []):
                coarse_date.append(f"{key}:{atom['surface']}")
            for atom in analysis["atoms"]:
                atoms.append({**atom, "from": key})

    for key in spec["entity_fields"]:
        text, basis = _slot_text(source.get(key))
        if not text or basis in NON_ASSERTING_BASES:
            continue
        if _is_declared_unknown(text):
            declared_unknown.append(f"{key}:{text[:24]}")
            continue
        for segment in split_entities(text):
            entity_atoms.append({**segment, "from": key})

    unsupported: list[dict[str, Any]] = []
    for atom in atoms:
        if atom_is_supported(atom, index):
            atom["supported"] = True
        else:
            atom["supported"] = False
            unsupported.append(atom)
    for atom in entity_atoms:
        atom["supported"] = atom_is_supported(atom, index)
        if not atom["supported"] and atom["class"] == "hard":
            unsupported.append(atom)

    checkable_count = len(atoms) + len([a for a in entity_atoms if a["class"] == "hard"])
    prose = item_text_for_content(field, item)
    content = content_support_ratio(prose, index)
    invented = invented_spans(prose, index)
    commitment_present = any(verb in prose for verb in COMMITMENT_VERBS)
    owner_present = any(
        isinstance(source.get(key), str) and not _is_declared_unknown(source.get(key))
        for key in spec.get("entity_fields", ())
    )
    result: dict[str, Any] = {
        "field": field,
        "atoms": atoms + entity_atoms,
        "checkable_count": checkable_count,
        "unsupported_atoms": unsupported,
        "has_unsupported": bool(unsupported),
        "declared_unknown": declared_unknown,
        "vague_time": vague_time,
        "coarse_date": coarse_date,
        "content_support": content,
        "invented_spans": invented,
        "has_invented_span": invented["span_count"] > 0,
        "vagueness": vagueness_signals(prose),
        "commitment_present": commitment_present,
        "owner_present": owner_present,
    }
    result["actionability"] = actionability_signals(result)
    return result


def analyze_output(output: Mapping[str, Any] | None, transcript: str) -> dict[str, Any]:
    """Grounding verdict for a whole six-field minutes object."""
    index = build_transcript_index(transcript or "")
    per_field: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(output, Mapping):
        output = {}

    summary_value = output.get("summary")
    per_field["summary"] = [analyze_item("summary", summary_value, index)]

    for field in LIST_FIELDS:
        items = output.get(field)
        if not isinstance(items, list):
            items = []
        per_field[field] = [analyze_item(field, item, index) for item in items]

    checkable = sum(r["checkable_count"] for rs in per_field.values() for r in rs)
    unsupported = sum(len(r["unsupported_atoms"]) for rs in per_field.values() for r in rs)
    items_total = sum(len(rs) for rs in per_field.values())
    items_flagged = sum(1 for rs in per_field.values() for r in rs if r["has_unsupported"])

    content_ratios = [
        r["content_support"]["ratio"]
        for rs in per_field.values() for r in rs
        if r.get("content_support")
    ]
    content_gram_counts = sum(
        r["content_support"]["gram_count"]
        for rs in per_field.values() for r in rs
        if r.get("content_support")
    )

    # Recall proxy for MISSING_ITEM: transcript action candidates against the
    # union of everything the minutes assert.
    all_item_texts = [
        item_text_for_content(field, item)
        for field in FIELD_SPEC
        for item in iter_items(output, field)
    ]
    coverage = candidate_coverage(action_candidates(transcript or ""), all_item_texts)

    invented_total = sum(
        r["invented_spans"]["span_count"] for rs in per_field.values() for r in rs
    )
    actionable_total = sum(
        1 for rs in per_field.values() for r in rs
        if field_is_task_like(r["field"]) and r.get("actionability", {}).get("actionable")
    )
    task_like_total = sum(
        1 for rs in per_field.values() for r in rs if field_is_task_like(r["field"])
    )

    return {
        "per_field": per_field,
        "checkable_atom_count": checkable,
        "unsupported_atom_count": unsupported,
        "item_count": items_total,
        "flagged_item_count": items_flagged,
        "unsupported_atom_rate": (unsupported / checkable) if checkable else None,
        "flagged_item_rate": (items_flagged / items_total) if items_total else None,
        "declared_unknown_count": sum(
            len(r["declared_unknown"]) for rs in per_field.values() for r in rs
        ),
        "vague_time_count": sum(
            len(r["vague_time"]) for rs in per_field.values() for r in rs
        ),
        "coarse_date_count": sum(
            len(r["coarse_date"]) for rs in per_field.values() for r in rs
        ),
        "content_checked_item_count": len(content_ratios),
        "content_gram_count": content_gram_counts,
        "content_support_mean": (
            sum(content_ratios) / len(content_ratios) if content_ratios else None
        ),
        "content_support_values": content_ratios,
        "action_candidate_coverage": coverage,
        "invented_span_count": invented_total,
        "items_with_invented_span": sum(
            1 for rs in per_field.values() for r in rs if r.get("has_invented_span")
        ),
        "task_like_item_count": task_like_total,
        "actionable_item_count": actionable_total,
        "actionable_rate": (actionable_total / task_like_total) if task_like_total else None,
        "risk_level_distribution": risk_level_distribution(output),
        "summary_decision_coverage": summary_decision_coverage(output, index),
    }


TASK_LIKE_FIELDS = ("decisions", "pending_items")


def field_is_task_like(field: str) -> bool:
    return field in TASK_LIKE_FIELDS


def risk_level_distribution(output: Mapping[str, Any] | None) -> dict[str, int]:
    """Count of risk levels. A constant level across a corpus is a calibration defect."""
    counts: dict[str, int] = {}
    for item in iter_items(output, "risks"):
        if isinstance(item, Mapping):
            level = item.get("level")
            if isinstance(level, str):
                counts[level] = counts.get(level, 0) + 1
    return counts


def summary_decision_coverage(output: Mapping[str, Any] | None, index: Mapping[str, str]) -> dict[str, Any]:
    """Do the decisions the minutes recorded also appear in its own summary?

    An internal-consistency measure that needs no transcript: a summary that omits
    every decision is not a summary of the meeting the rest of the document
    describes. Measured by character 4-gram overlap between each decision's
    content and the summary text.
    """
    if not isinstance(output, Mapping):
        return {"decision_count": 0, "covered_count": 0, "coverage_rate": None}
    summary = output.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return {"decision_count": 0, "covered_count": 0, "coverage_rate": None}
    summary_grams = content_gram_set(summary)
    decisions = [item for item in iter_items(output, "decisions") if isinstance(item, Mapping)]
    if not decisions:
        return {"decision_count": 0, "covered_count": 0, "coverage_rate": None}
    covered = 0
    for item in decisions:
        grams = content_gram_set(item.get("content") or "")
        if grams and is_coverable_by(grams, summary_grams, 0.35):
            covered += 1
    return {
        "decision_count": len(decisions),
        "covered_count": covered,
        "coverage_rate": covered / len(decisions),
        "note": "4-gram overlap at 0.35; paraphrase lowers it, so read it as a floor",
    }


def collect_examples(
    analysis: Mapping[str, Any], *, limit_per_field: int = 3
) -> dict[str, list[dict[str, Any]]]:
    """Flagged atoms per field, for manual audit of false positives."""
    examples: dict[str, list[dict[str, Any]]] = {}
    for field, results in analysis.get("per_field", {}).items():
        picked: list[dict[str, Any]] = []
        for item_index, result in enumerate(results):
            for atom in result["unsupported_atoms"]:
                if len(picked) >= limit_per_field:
                    break
                picked.append({
                    "item_index": item_index,
                    "kind": atom.get("kind"),
                    "from": atom.get("from"),
                    "surface": atom.get("surface"),
                })
            if len(picked) >= limit_per_field:
                break
        if picked:
            examples[field] = picked
    return examples


def item_text(field: str, item: Any) -> str:
    """Canonical text of an item, used for pairing baseline against revised."""
    if isinstance(item, str):
        return item
    if not isinstance(item, Mapping):
        return ""
    spec = FIELD_SPEC.get(field, {})
    keys: Iterable[str] = tuple(spec.get("text_fields", ())) + tuple(spec.get("entity_fields", ()))
    return " ".join(str(item[k]) for k in keys if isinstance(item.get(k), str))


def iter_items(output: Mapping[str, Any] | None, field: str) -> list[Any]:
    """Items of a list field, or a single-element list for the summary."""
    if not isinstance(output, Mapping):
        return []
    if field == "summary":
        value = output.get("summary")
        return [value] if isinstance(value, str) else []
    value = output.get(field)
    return list(value) if isinstance(value, list) else []


def normalize_transcript_for_review(transcript: str) -> str:
    """Whitespace-collapsed transcript, safe to embed in a JSON report."""
    return re.sub(r"\s+", " ", transcript or "").strip()


def normalize_with_offsets(text: str) -> tuple[str, list[int]]:
    """Normalize while remembering where each normalized character came from.

    Returns ``(normalized, offsets)`` where ``offsets[i]`` is the index in
    ``text`` of the character that produced ``normalized[i]``. This is what lets
    a match found in normalized space be mapped back to a transcript window.
    """
    if not isinstance(text, str):
        raise TypeError("normalize_with_offsets expects a string")
    normalized_chars: list[str] = []
    offsets: list[int] = []
    for position, char in enumerate(unicodedata.normalize("NFC", text)):
        if unicodedata.category(char)[0] in "LN":
            normalized_chars.append(char.casefold())
            offsets.append(position)
    return "".join(normalized_chars), offsets


def find_source_window(
    item_text: str, transcript: str, *, min_shingle: int = 6, padding: int = 300
) -> dict[str, Any] | None:
    """Locate the transcript region an item was most likely derived from.

    Uses the longest substring of the item that appears verbatim in the
    transcript, then returns a padded window around it. Returns None when no
    substring of at least ``min_shingle`` characters matches, which means the
    item is either heavily paraphrased or unsupported.
    """
    item_norm = normalize(item_text)
    if len(item_norm) < min_shingle or not transcript:
        return None
    transcript_norm, offsets = normalize_with_offsets(transcript)
    longest = min(len(item_norm), 24)
    for size in range(longest, min_shingle - 1, -1):
        for start in range(0, len(item_norm) - size + 1):
            shingle = item_norm[start:start + size]
            hit = transcript_norm.find(shingle)
            if hit >= 0:
                original_start = offsets[hit]
                original_end = offsets[hit + size - 1] + 1
                window_start = max(0, original_start - padding)
                window_end = min(len(transcript), original_end + padding)
                return {
                    "matched_shingle_chars": size,
                    "transcript_start": original_start,
                    "window_start": window_start,
                    "window_end": window_end,
                    "window": transcript[window_start:window_end],
                }
    return None


def window_contains_date(window: str) -> bool:
    """Does this transcript window assert any date at all?"""
    return bool(extract_date_atoms(window))


_CLAUSE_SPLIT_RE = re.compile(r"[。！？；\n，、,;!?]+")


def content_support_ratio(
    text: str, index: Mapping[str, str], *, n: int = 4, max_grams: int = 400
) -> dict[str, Any] | None:
    """Fraction of a claim's distinctive character n-grams present in the transcript.

    The atom classes above only cover dates, quantities and named entities, so
    they cannot see the failure mode the reviewer actually flags most: an
    invented noun phrase or an inferred suggestion (``加强前期规划``,
    ``无待确认事项``). This is the blunt instrument for that class.

    Text is split into clauses first, because a 4-gram spanning a comma
    boundary is not a phrase anyone asserted. The result is a *continuous*
    ratio rather than a pass/fail: paraphrase legitimately lowers it, so the
    number is reported and any threshold is justified against the observed
    distribution rather than assumed.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    plain_transcript = index.get("plain", "")
    if not plain_transcript:
        return None
    grams: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(text):
        normalized = normalize(clause)
        if len(normalized) < n:
            continue
        for start in range(len(normalized) - n + 1):
            grams.append(normalized[start:start + n])
    if not grams:
        return None
    unique = list(dict.fromkeys(grams))
    sampled = unique[:max_grams]
    hits = sum(1 for gram in sampled if gram in plain_transcript)
    return {
        "ratio": hits / len(sampled),
        "gram_count": len(sampled),
        "gram_size": n,
        "truncated": len(unique) > max_grams,
    }


def item_text_for_content(field: str, item: Any) -> str:
    """The prose of an item, excluding fields already covered by atom checks."""
    if isinstance(item, str):
        return item
    if not isinstance(item, Mapping):
        return ""
    spec = FIELD_SPEC.get(field, {})
    parts = [str(item[k]) for k in spec.get("text_fields", ()) if isinstance(item.get(k), str)]
    return " ".join(parts)


# Content-overlap support is only interpretable for fields that are supposed to
# restate what was said. Measured on VCSum test26, the by-field means are:
# summary 0.250, topics 0.192, viewpoints 0.312, decisions 0.321,
# pending_items 0.213, risks 0.125. ``risks`` has by far the lowest overlap
# because risks are model-authored analysis, and within ``viewpoints`` the items
# the reviewer flags score *higher* than average (0.333 vs 0.312). Pooling all
# six fields would therefore produce a number that points the wrong way for half
# the schema, so discrimination is only computed over the two fields where the
# direction is correct.
CONTENT_SUPPORT_FIELDS = ("decisions", "pending_items")


def content_gram_set(text: str, *, n: int = 4) -> set[str]:
    """The distinctive character n-grams of a text, used for merge detection."""
    grams: set[str] = set()
    for clause in _CLAUSE_SPLIT_RE.split(text or ""):
        normalized = normalize(clause)
        if len(normalized) < n:
            continue
        for start in range(len(normalized) - n + 1):
            grams.add(normalized[start:start + n])
    return grams


def is_coverable_by(grams: set[str], candidates: set[str], threshold: float = 0.6) -> bool:
    """Is most of this item's wording also present across some other items?

    Used to separate a genuine deletion from a consolidation: an item that was
    folded into its neighbours keeps most of its wording somewhere in the
    revised field.
    """
    if not grams:
        return False
    covered = sum(1 for gram in grams if gram in candidates)
    return covered / len(grams) >= threshold


# 来历：为 VAGUE 代理加的（模糊词密度）。
# **当前状态：它支撑的 `vagueness` 代理已被否证**（AUC 0.59 但方向相反——被标记项模糊词
# 反而更少），登记在 PROXY_STATUS。因此这张词表目前**不支撑任何可引用结论**，
# 保留它是为了复现那次否证。
# 复核：`critique` 一节重新测量代理对齐，若方向翻转再重新评估。
HEDGE_MARKERS = (
    "若干", "一些", "相关", "适当", "尽快", "进一步", "酌情", "视情况", "相应",
    "有关", "大力", "切实", "积极", "努力", "加强", "提升", "优化", "完善",
    "推进", "深化", "强化", "落实", "确保", "有效", "充分", "不断",
)

# A verifiable item names who, what, or when. Absence of all three is the
# operational definition of "cannot be turned into a task".
SPECIFICITY_SIGNALS = ("owner", "time", "quantity", "date", "entity")


def vagueness_signals(text: str) -> dict[str, Any]:
    """Hedge words plus a lack of specifics; the proxy for the VAGUE issue type.

    A text can be full of hedge words and still be precise ("尽快在 3 月 1 日前"),
    so hedge count alone is not the signal. The reported value is hedges per
    specific token, and the raw counts are kept beside it so the ratio can be
    audited rather than trusted.
    """
    if not isinstance(text, str) or not text.strip():
        return {"hedge_count": 0, "hedge_density": 0.0}
    hedge_count = sum(text.count(marker) for marker in HEDGE_MARKERS)
    specific_count = len(extract_date_atoms(text)) + len(extract_quantity_atoms(text))
    return {
        "hedge_count": hedge_count,
        "hedge_density": hedge_count / max(1, len(normalize(text)) / 20),
        "specific_count": specific_count,
    }


def actionability_signals(analysis_result: Mapping[str, Any]) -> dict[str, Any]:
    """Does an item carry a commitment together with any anchor?

    The proxy for the NOT_ACTIONABLE issue type. An item is treated as
    actionable when it names a commitment *and* supplies at least one of an
    owner, a time, a quantity or a named entity. This mirrors the product's own
    reasoning and is calibrated against the reviewer's NOT_ACTIONABLE issues.
    """
    atoms = analysis_result.get("atoms") or []
    atoms = [a for a in atoms if a.get("kind") != "entity" or a.get("class") != "descriptive"]
    owner_present = bool(analysis_result.get("owner_present"))
    time_present = bool(
        analysis_result.get("atoms") and
        any(a.get("from") == "deadline_suggestion" for a in (analysis_result.get("atoms") or []))
    )
    return {
        "commitment_present": bool(analysis_result.get("commitment_present")),
        "owner_present": owner_present,
        "time_present": time_present,
        "anchor_count": len(atoms) + (1 if owner_present else 0) + (1 if time_present else 0),
        "actionable": bool(analysis_result.get("commitment_present"))
        and (owner_present or time_present or bool(atoms)),
    }


# ---------------------------------------------------------------------------- #
# Deadline value classification
# ---------------------------------------------------------------------------- #

# A real time reference that deliberately leaves the exact point open. Legitimate:
# the meeting genuinely had not fixed a date.
# 来历：为观测"合法开放式时间被算成期限误用"加的。
# `deadline_state` 据此把原先笼统的 `unparsed` 拆成 open_ended（如 `年内`、`上线后`）
# 与 field_misuse（动作文字进了期限槽）——后者是**已证实的真缺陷**。
# 复核：field_misuse 的取值是否真的都是动作文字（`deadline_state` 的样例）。
OPEN_ENDED_TIME_MARKERS = (
    "年内", "本年度", "本学期", "下学期", "本学期内", "上年", "明年", "今年", "去年",
    "上线后", "上线之后", "投产后", "开学前", "开学后", "结束后", "完成后", "明确后",
    "短期内", "近期", "远期", "初期", "中期", "后期", "初期阶段",
    "每学期", "每月", "每周", "每年", "按季", "定期", "循环",
    "规划中", "评估中", "推进中", "筹建中", "待定后", "视情况", "视技术",
)

# Commitments and deliverables. Their presence in a *deadline* value is the
# documented defect in which an action is written into the time slot
# ("把动作文字填入期限字段"), so it is counted rather than excused.
# 来历：为 field_misuse 判定加的（动作写进期限槽），同时供行动候选（MISSING_ITEM 代理）。
# **两个用途的状态不同**：field_misuse 是已验证的缺陷类；`action_candidate_coverage`
# 代理已被否证（符号随归一化翻转），因此后者不支撑可引用结论。
# 复核：`deadline_state` 的 field_misuse 计数与样例。
COMMITMENT_VERBS = (
    "负责", "完成", "推进", "提交", "确认", "评估", "补齐", "上线", "落实",
    "跟进", "制定", "准备", "安排", "组织", "梳理", "核对", "整理", "建立",
    "搭建", "优化", "修复", "发布", "启动", "开展", "推动", "督促", "协调",
    "撰写", "编制", "收集", "分析", "调研", "培训", "对接", "明确", "确定",
)


def deadline_state(value: Any) -> str:
    """Classify a deadline value into one of six states.

    ``field_misuse`` is a real defect: the value carries a commitment but no time
    reference at all, which means an action was written into the time slot.
    ``open_ended`` is not a defect: the meeting genuinely left the point open.
    Before this split the two were pooled into ``unparsed`` and excluded, which
    hid a documented failure mode behind a classification gap.
    """
    if not isinstance(value, str) or not value.strip():
        return "empty"
    text = value.strip()
    if _is_declared_unknown(text):
        return "declared_unknown"
    atoms = extract_date_atoms(text) + extract_quantity_atoms(text)
    if atoms:
        return "asserting"
    has_time = any(marker in text for marker in OPEN_ENDED_TIME_MARKERS) or _is_vague_time(text)
    has_commitment = any(verb in text for verb in COMMITMENT_VERBS)
    if has_time:
        return "open_ended"
    if has_commitment:
        return "field_misuse"
    return "unparsed"


# ---------------------------------------------------------------------------- #
# Proxy status registry
# ---------------------------------------------------------------------------- #

# Every proxy in this module is calibrated against something external before it
# is allowed into a report, and the outcome is recorded here. Rejected proxies
# stay in the code so the negative result is reproducible and so nobody
# re-invents them; they are simply not reportable.
PROXY_STATUS: dict[str, dict[str, str]] = {
    "atom_grounding": {
        "status": "validated",
        "evidence": (
            "five false-positive classes found and fixed by auditing flagged samples "
            "(entity splitting, match granularity, percent notation, bare years, "
            "year+classifier); self-check and unit tests cover each"
        ),
    },
    "merge_detection": {
        "status": "validated",
        "evidence": (
            "26 of 101 unmatched draft items reclassified as consolidations on VCSum test26; "
            "four sampled by hand were all genuine merges; stable for thresholds 0.6-0.7"
        ),
    },
    "content_support": {
        "status": "field_limited",
        "evidence": (
            "AUC 0.624 for reviewer flags on decisions+pending_items (test26), "
            "0.501 on dev22 (no discrimination); pooling across fields reverses direction"
        ),
    },
    "deadline_state": {
        "status": "validated",
        "evidence": (
            "splits the former unparsed bucket into open_ended (61) and field_misuse (14); "
            "field_misuse is the documented defect of writing an action into the time slot"
        ),
    },
    "action_candidate_coverage": {
        "status": "rejected",
        "evidence": (
            "the association with the reviewer's MISSING_ITEM count flips sign across four "
            "defensible normalisations on VCSum test26: count -0.24, rate +0.44, per-1k +0.33. "
            "The rate variant is confounded by transcript length (longer meetings yield both more "
            "candidates and more missing items). A quantity whose sign depends on the denominator "
            "choice carries no evidential weight, so MISSING_ITEM remains unmeasured"
        ),
    },
    "risk_level_distribution": {
        "status": "descriptive",
        "evidence": "spread check only; HIGH/MEDIUM/LOW all present, so level is not degenerate",
    },
    "summary_decision_coverage": {
        "status": "descriptive",
        "evidence": "internal consistency between two output fields; no external reference exists",
    },
    "invented_spans": {
        "status": "rejected",
        "evidence": (
            "same instability: count +0.34, density -0.21, per-1k +0.55 against the reviewer's "
            "UNSUPPORTED count. Transcript length correlates -0.78 with the density, so the "
            "length-normalised variants are driven by a shared denominator rather than by "
            "invention. Content-level UNSUPPORTED therefore remains unmeasured"
        ),
    },
    "actionability": {
        "status": "rejected",
        "evidence": "AUC 0.53 against NOT_ACTIONABLE flags; no usable discrimination",
    },
    "vagueness": {
        "status": "rejected",
        "evidence": (
            "AUC 0.59 with flagged items carrying LOWER hedge density than clean ones, "
            "so the hedge-count hypothesis is falsified rather than merely weak"
        ),
    },
}


def reportable_proxies() -> dict[str, dict[str, str]]:
    """Proxies whose evidence supports quoting them, with the evidence attached."""
    return {
        name: entry for name, entry in PROXY_STATUS.items()
        if entry["status"] in {"validated", "field_limited", "descriptive"}
    }


# ---------------------------------------------------------------------------- #
# Action-candidate recall proxy (targets MISSING_ITEM)
# ---------------------------------------------------------------------------- #

_SENTENCE_SPLIT_RE = re.compile(r"[。！？；\n]+")

# Past-completion markers: a sentence reporting something already done is not a
# pending action, and including it would flood the candidate set.
# 来历：为观测"已完成的陈述被当成待办行动候选"加的（行动候选只收承诺，不收汇报）。
# 复核：`action_candidates` 的抽样；它服务的代理已被否证，故只作诊断。
PAST_MARKERS = ("已完成", "已经", "已于", "上次", "去年", "上个月", "此前", "之前已", "早已", "过去")


def action_candidates(transcript: str, *, max_candidates: int = 400) -> list[str]:
    """Transcript sentences that look like they carry a commitment or deliverable.

    This is the recall-side proxy for ``MISSING_ITEM``. It is deliberately
    rule-based and narrow: a sentence qualifies only if it names a commitment
    verb and is not merely reporting something already finished. The output is
    still noisy, so the aggregate is only reported after calibration against the
    reviewer's own ``MISSING_ITEM`` issues.
    """
    candidates: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(transcript or ""):
        stripped = sentence.strip()
        if len(stripped) < 8:
            continue
        if not any(verb in stripped for verb in COMMITMENT_VERBS):
            continue
        if any(marker in stripped for marker in PAST_MARKERS):
            continue
        candidates.append(stripped)
        if len(candidates) >= max_candidates:
            break
    return candidates


def candidate_coverage(
    candidates: Sequence[str], item_texts: Sequence[str], *, threshold: float = 0.5
) -> dict[str, Any]:
    """How many transcript action candidates are reflected in any output item.

    A candidate counts as covered when at least ``threshold`` of its character
    4-grams appear somewhere across the output items. Coverage is a ceiling-style
    measure: the minutes are not expected to restate every transcript sentence,
    so a low absolute number is normal and only the *relative* value across
    meetings is interpretable.
    """
    output_pool: set[str] = set()
    for text in item_texts:
        output_pool |= content_gram_set(text)
    covered: list[str] = []
    uncovered: list[str] = []
    for candidate in candidates:
        grams = content_gram_set(candidate)
        if not grams:
            continue
        (covered if is_coverable_by(grams, output_pool, threshold) else uncovered).append(candidate)
    total = len(covered) + len(uncovered)
    return {
        "candidate_count": total,
        "covered_count": len(covered),
        "uncovered_count": len(uncovered),
        "coverage_rate": (len(covered) / total) if total else None,
        "uncovered_examples": uncovered[:8],
    }


# ---------------------------------------------------------------------------- #
# Invented-span proxy (targets content-level UNSUPPORTED)
# ---------------------------------------------------------------------------- #

# Function words and hedges that make a span uninformative rather than invented.
# 来历：为观测"功能词片段被当成凭空术语"加的。
# **当前状态：它支撑的 `invented_spans` 代理已被否证**（count +0.34 / density −0.21 /
# per-1k +0.55，符号随归一化翻转），登记在 PROXY_STATUS。因此这张词表目前
# **不支撑任何可引用结论**，保留它是为了复现那次否证。
# 复核：`critique` 一节的代理对齐。
SPAN_STOPWORDS = (
    "的", "了", "和", "与", "及", "或", "是", "在", "有", "为", "对", "以", "并",
    "可", "会", "要", "需", "应", "将", "已", "不", "也", "都", "还", "就", "而",
    "建议", "应该", "需要", "可以", "可能", "进一步", "相关", "有关", "进行",
)


def invented_spans(text: str, index: Mapping[str, str], *, n: int = 3, min_span: int = 4) -> dict[str, Any]:
    """Maximal runs of a claim that have no lexical trace in the transcript.

    Slides an n-gram window, merges adjacent absent windows into maximal spans,
    and keeps spans at least ``min_span`` characters long that are not mostly
    function words. The spans are returned verbatim so a reviewer can judge them:
    a genuine paraphrase can still produce a span, so the count is a proxy and
    the examples are the evidence.
    """
    plain_transcript = index.get("plain", "")
    if not isinstance(text, str) or not plain_transcript:
        return {"span_count": 0, "longest_span_chars": 0, "spans": []}
    spans: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(text):
        normalized = normalize(clause)
        if len(normalized) < n:
            continue
        absent = [normalized[i:i + n] not in plain_transcript
                  for i in range(len(normalized) - n + 1)]
        start: int | None = None
        for position, is_absent in enumerate(absent):
            if is_absent and start is None:
                start = position
            elif not is_absent and start is not None:
                spans.append(normalized[start:position + n - 1])
                start = None
        if start is not None:
            spans.append(normalized[start:])
    filtered = []
    for span in spans:
        if len(span) < min_span:
            continue
        content_chars = sum(1 for char in span if char not in SPAN_STOPWORDS)
        if content_chars / len(span) < 0.5:
            continue
        filtered.append(span)
    return {
        "span_count": len(filtered),
        "longest_span_chars": max((len(s) for s in filtered), default=0),
        "spans": filtered[:8],
    }

# --------------------------------------------------------------------------- #
# 与产品共用同一份定义
# --------------------------------------------------------------------------- #

# 文本归一、条目身份判据、时间语义判定这三件事，产品与评测以前各写了一份，
# 结果两边必然漂移（评测里同一份代理换三种归一化就给出相反的符号，产品里
# 期限字段误用只能靠词表猜）。现在统一委托产品的 common/ 模块：**契约只有一处定义**，
# 评测读结构而不是再猜一遍。
#
# 产品目录不可用时（例如把 evaluation/ 单独拷出去跑）退回本模块的本地实现，
# 并置 shared_contract=False 让报告能如实标注用的是哪一份。
def _load_shared_contract():
    import sys
    from pathlib import Path as _Path
    product_root = _Path(__file__).resolve().parents[2] / "ai_meeting" / "fastapi-app"
    if not (product_root / "common" / "timex.py").exists():
        return None
    if str(product_root) not in sys.path:
        sys.path.insert(0, str(product_root))
    try:
        from common import textsim as _textsim, timex as _timex  # type: ignore
    except Exception:
        return None
    return {"textsim": _textsim, "timex": _timex}


SHARED = _load_shared_contract()
shared_contract = SHARED is not None

# 产品目录不可用时，本地兜底判据所用的阈值与相似度实现。**必须与 common/textsim.py
# 保持一致**，而它们只在"产品目录缺失"这一条路径上生效——该路径在开发环境里从不触发，
# 因此曾经写成引用两个根本不存在的名字（`MATCH_THRESHOLD`、`_similarity`，后者只在
# analysis.py 里定义，而 analysis 又反过来 import 本模块）。一旦真的退化，它会
# NameError 崩掉，而不是按声明"退回本地实现并标注用的是哪一份"。
# 现在这里给出可执行的定义，并由 test_metrics 强制走该分支的测试守住。
FALLBACK_SIMILARITY_THRESHOLD = 0.5
FALLBACK_MIN_MATCHED_BLOCK = 4


def _fallback_similarity(first: str, second: str) -> tuple[float, int]:
    """兜底相似度：返回（相似度比，最长公共块长度）。与 textsim.similarity 同语义。"""
    from difflib import SequenceMatcher

    if not first or not second:
        return 0.0, 0
    if first == second:
        return 1.0, len(first)
    matcher = SequenceMatcher(None, first, second)
    longest = max((block.size for block in matcher.get_matching_blocks()), default=0)
    return matcher.ratio(), longest


def shared_normalize(text: str) -> str:
    """归一化：优先用产品定义，保证两侧对"同一段文本"的判据一致。"""
    if SHARED:
        return SHARED["textsim"].normalize(text)
    return normalize(text)


def shared_is_same_item(first: str, second: str) -> bool:
    """条目身份判据：产品与评测必须同源。"""
    if SHARED:
        return SHARED["textsim"].is_same_item(first, second)
    if not first or not second:
        return False
    ratio, longest = _fallback_similarity(first, second)
    if ratio < FALLBACK_SIMILARITY_THRESHOLD:
        return False
    return longest >= min(FALLBACK_MIN_MATCHED_BLOCK, len(first), len(second))


def shared_time_basis(value: str, *, reference_date: str = "") -> str:
    """时间语义判定：直接读产品契约层的判定，不再自行启发式推断。"""
    if SHARED:
        return SHARED["timex"].parse_time(value, reference_date=reference_date).basis
    return ""


def shared_matches_any(text: str, candidates: Sequence[str]) -> bool:
    """条目是否还在候选池里（三条分支：包含 / 身份判据 / 措辞覆盖）。

    **直接委托产品**：产品用同一个函数判断"初稿里的条目是不是被静默删掉了"，
    评测用另一个近似实现就会得出两套结论。产品目录不可用时退回只做身份判据，
    并在返回值里无法标注（因此调用方要读 `shared_contract` 才能知道退化了）。
    """
    if SHARED:
        return SHARED["textsim"].matches_any(text, list(candidates))
    return any(shared_is_same_item(text, candidate) for candidate in candidates)


def shared_thresholds() -> dict[str, float]:
    """当前**实际生效**的身份判据阈值，供报告如实标注。

    报告里手写阈值必然随校准漂移——写死的一句话会在阈值改掉之后继续宣称旧值。
    因此报告读这里，而不是抄一份常量。
    """
    if SHARED:
        textsim = SHARED["textsim"]
        return {
            "similarity": float(getattr(textsim, "SIMILARITY_THRESHOLD",
                                        FALLBACK_SIMILARITY_THRESHOLD)),
            "min_matched_block": float(getattr(textsim, "MIN_MATCHED_BLOCK",
                                               FALLBACK_MIN_MATCHED_BLOCK)),
            "source": "product/common/textsim.py",
        }
    return {
        "similarity": float(FALLBACK_SIMILARITY_THRESHOLD),
        "min_matched_block": float(FALLBACK_MIN_MATCHED_BLOCK),
        "source": "evaluation/metrics/grounding.py（产品目录不可用时的本地实现）",
    }


def shared_deadline_state(value: object) -> str:
    """期限字段状态。basis 由契约判定，评测只做聚合，不做二次解释。"""
    if SHARED:
        return SHARED["timex"].parse_time(value if isinstance(value, str) else "").basis or "unclassified"
    return ""
