"""时间表达的解析与归一化。纯标准库，产品与评测共用同一份定义。

为什么要单独抽出来：期限字段的缺陷（把动作写进时间槽）在旧实现里只能靠
"看这个字符串像不像时间"来事后猜测，评测套件又自己写了一套正则去猜同一件事，
两套实现必然漂移。这里把"什么是一个时间表达"变成**一个可复用的类型判定**，
于是产品可以在写入时校验、评测可以直接读取判定结果，不再各自猜。

设计上刻意不做词表堆砌：
- 语法由**结构**决定（年月日的组合关系），而不是把见过的写法逐个列进正则；
- 开放语义（今年、年内、上线后）与相对语义（下周三）分别建模，因为它们
  的校验要求不同：前者是合法的"不确定"，后者必须能解析出锚点才算成立；
- 抽取结果同时给出**命中的字符区间**，让调用方能回写证据 span。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# 时间值的语义类别。这是契约的一部分，不要随意增减：
# 下游的校验、指标、前端展示都按这个封闭集合分支。
TIME_BASES = (
    "stated",         # 原文明确给出的时间点或时段
    "derived",        # 由明确锚点推出（下周三 -> 2026-03-10），必须带锚点
    "open_ended",     # 真实但开放（年内、上线后、持续推进）
    "not_mentioned",  # 会议确实没有提到期限
    "model_inferred", # 模型自己设定的期限，会上无人提出
)

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2, "貳": 2,
    "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6,
    "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}

_NUMBER = r"(?:\d{1,4}|[零〇一壹二两贰貳三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟]+)"
_SEP = r"[-/.年月日\s]*"

# 结构化的时间点：由"年/月/日"的层级组合而成，而不是枚举写法。
_DATE_POINT = re.compile(
    rf"(?P<year>\d{{4}})\s*[-/.年]\s*(?P<month>\d{{1,2}})(?:\s*[-/.月]\s*(?P<day>\d{{1,2}})\s*日?)?"
    r"|(?P<month2>\d{1,2})\s*月\s*(?P<day2>\d{1,2})\s*日"
    r"|(?P<year2>\d{4})\s*年"
)
_DATE_RANGE = re.compile(
    rf"(?P<a>{_NUMBER})\s*[-~至到]\s*(?P<b>{_NUMBER})\s*(?P<unit>月|日|周|年|月份)"
)
_RELATIVE = re.compile(
    r"(?P<offset>[这|本|上|下|前|后|明|去|今]+)\s*(?P<unit>周|月|年|季度|学期|天|日)"
    r"|(?P<quant>[0-9一二两三四五六七八九十]+)\s*(?P<qunit>个?)\s*(?:工作日|小时|天|周|个月|月|年)\s*(?:内|后|之内|以内)?"
)
_OPEN_ENDED = re.compile(
    r"年内|本年度|本学期|下学期|上学期|上半年|下半年|季度内|"
    r"上线(?:之)?后|投产(?:之)?后|完成(?:之)?后|结束(?:之)?后|明确(?:之)?后|"
    r"开学(?:之)?前|开学(?:之)?后|[^，。；]{0,6}期间|"
    r"近期|短期内|远期|初期|中期|后期|持续推进|持续进行|尽快|尽早|后续|随时|定期|每(?:周|月|学期|年)"
)
_PERIODIC = re.compile(r"每\s*(?:日|周|月|季|学期|年)")

# "没有取值"的表达方式。这是**契约的一部分**：它规定了如何声明"此处无值"，
# 因此必须由契约层声明、由产品和评测共用，而不是各自维护一份词表。
#
# 分成两组的理由是实测踩到的：短标记（"-"、"na"）如果按"包含即命中"判定，
# "2026-09-28" 会因为含连字符被判成"没有取值"。所以短而有歧义的标记必须**整体相等**，
# 只有本身就是短语的标记才允许包含匹配。
NON_ASSERTION_TOKENS = (
    "无", "待定", "暂无", "尚未", "不涉及", "无需", "na", "none", "null", "-", "—",
)
NON_ASSERTION_PHRASES = (
    "待确认", "待补充", "待核实", "待商定", "待定稿", "需确认", "未明确", "未提及",
    "未指定", "不确定", "未确定", "没有明确", "暂无明确", "视情况而定", "未给出",
    "原文未给出", "无法确定", "未说明",
)


def is_non_assertion(value: str) -> bool:
    """这段文本是否在声明"此处没有取值"，而不是断言一个值。"""
    if not isinstance(value, str) or not value.strip():
        return True
    text = unicodedata.normalize("NFKC", value.strip())
    # 精确匹配先于包含匹配：只由短标记构成的文本才算声明"无值"。
    # 比较前去掉标点，这样 "n/a" 与 "n.a." 都能对上 "na"。
    skeleton = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", normalize_digits(text)).casefold()
    if skeleton in {re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", token.casefold())
                    for token in NON_ASSERTION_TOKENS}:
        return True
    return any(phrase in text for phrase in NON_ASSERTION_PHRASES)


@dataclass(frozen=True)
class TimeParse:
    """一段文本里的时间语义判定结果。"""

    basis: str
    normalized: str = ""
    surface: str = ""
    span: tuple[int, int] | None = None
    anchor: str = ""
    alternatives: tuple[str, ...] = field(default_factory=tuple)


def chinese_numeral_to_int(token: str) -> int | None:
    """把中文数词转成整数；不认识的返回 None。"""
    if not token:
        return None
    if token.isdigit():
        return int(token)
    total = section = 0
    seen = False
    for char in token:
        if char in _CN_DIGITS:
            section = _CN_DIGITS[char]
            seen = True
        elif char in _CN_UNITS:
            section = (section or 1) * _CN_UNITS[char]
            total += section
            section = 0
            seen = True
        else:
            return None
    return (total + section) if seen else None


def normalize_digits(value: str) -> str:
    return re.sub(
        r"[零〇一壹二两贰貳三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟]+",
        lambda m: str(chinese_numeral_to_int(m.group(0)) or m.group(0)),
        value,
    )


def _iso(year: int, month: int | None, day: int | None) -> str:
    if month is None:
        return f"{year:04d}"
    if day is None:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_time(value: str, *, reference_date: str = "") -> TimeParse:
    """判定一段文本的时间语义。

    `reference_date`（ISO 日期）用于给相对表达提供锚点。没有锚点时不臆造
    具体日期——那正是旧实现把"今年"错算成具体年份的来源。
    """
    if not isinstance(value, str) or not value.strip():
        return TimeParse(basis="not_mentioned")
    if is_non_assertion(value):
        return TimeParse(basis="not_mentioned", surface=value.strip()[:40])
    text = unicodedata.normalize("NFKC", value.strip())

    match = _DATE_POINT.search(text)
    if match:
        groups = match.groupdict()
        if groups.get("year"):
            year = int(groups["year"])
            month = int(groups["month"]) if groups.get("month") else None
            day = int(groups.get("day")) if groups.get("day") else None
            if month and not 1 <= month <= 12:
                month = None
            if day and not 1 <= day <= 31:
                day = None
            return TimeParse(
                basis="stated", normalized=_iso(year, month, day),
                surface=match.group(0), span=match.span(),
            )
        if groups.get("month2"):
            month = int(groups["month2"])
            day = int(groups["day2"]) if groups.get("day2") else None
            if 1 <= month <= 12:
                year = int(reference_date[:4]) if reference_date[:4].isdigit() else None
                if year:
                    return TimeParse(
                        basis="derived", normalized=_iso(year, month, day),
                        surface=match.group(0), span=match.span(),
                        anchor="reference_date",
                    )
                return TimeParse(
                    basis="stated", normalized=f"{month:02d}-{day:02d}" if day else f"{month:02d}",
                    surface=match.group(0), span=match.span(),
                )
        if groups.get("year2"):
            return TimeParse(
                basis="stated", normalized=groups["year2"],
                surface=match.group(0), span=match.span(),
            )

    match = _DATE_RANGE.search(text)
    if match:
        return TimeParse(
            basis="stated",
            normalized=f"{normalize_digits(match.group('a'))}-{normalize_digits(match.group('b'))}{match.group('unit')}",
            surface=match.group(0), span=match.span(),
        )

    match = _RELATIVE.search(text)
    if match:
        surface = match.group(0)
        groups = match.groupdict()
        # 时长（"十五个工作日内"）只确立长度，不确立时点：起点未给定时
        # 不能拿会议日期去推，否则就是过度推断。只有"下周三/本月"这类
        # 偏移表达才有资格由 reference_date 解出具体日期。
        if groups.get("offset"):
            if reference_date:
                resolved = _resolve_relative(match, reference_date)
                if resolved:
                    return TimeParse(
                        basis="derived", normalized=resolved, surface=surface,
                        span=match.span(), anchor=reference_date,
                    )
            return TimeParse(
                basis="open_ended", surface=surface, span=match.span(),
                anchor="" if reference_date else "no_reference_date",
                alternatives=("relative_expression_needs_anchor",),
            )
        return TimeParse(
            basis="open_ended", surface=surface, span=match.span(),
            alternatives=("duration_without_start_anchor",),
        )

    match = _PERIODIC.search(text) or _OPEN_ENDED.search(text)
    if match:
        return TimeParse(basis="open_ended", surface=match.group(0), span=match.span())

    return TimeParse(basis="", surface=text[:40])


def _resolve_relative(match: "re.Match[str]", reference_date: str) -> str:
    """把"下周三 / 本月 / 3 天后"解到具体日期；无法确定时返回空串。"""
    try:
        from datetime import date, timedelta
        ref = date.fromisoformat(reference_date)
    except (ValueError, TypeError):
        return ""
    groups = match.groupdict()
    quant = groups.get("quant")
    offset, unit = groups.get("offset"), groups.get("unit")
    try:
        if quant:
            amount = chinese_numeral_to_int(quant)
            if amount is None:
                return ""
            if "小时" in match.group(0) or "工作日" in match.group(0) or unit == "天":
                return (ref + timedelta(days=amount)).isoformat()
            if unit == "周":
                return (ref + timedelta(weeks=amount)).isoformat()
            if unit == "月":
                month = ref.month + amount
                year = ref.year + (month - 1) // 12
                return _iso(year, (month - 1) % 12 + 1, ref.day)
            if unit == "年":
                return _iso(ref.year + amount, ref.month, ref.day)
            return ""
        if not offset:
            return ""
        if unit == "天" or unit == "日":
            delta = {"今": 0, "本": 0, "明": 1, "后": 2, "前": -1, "上": -1, "去": -1}.get(offset[:1])
            return (ref + timedelta(days=delta)).isoformat() if delta is not None else ""
        if unit == "周":
            base = {"这": 0, "本": 0, "下": 1, "上": -1, "前": -1, "后": 1}.get(offset[:1])
            return (ref + timedelta(weeks=base)).isoformat() if base is not None else ""
        if unit == "月":
            base = {"这": 0, "本": 0, "下": 1, "上": -1, "前": -1, "明": 0}.get(offset[:1])
            if base is None:
                return ""
            month = ref.month + base
            year = ref.year + (month - 1) // 12
            return _iso(year, (month - 1) % 12 + 1, 1)
        if unit == "年":
            base = {"今": 0, "本": 0, "明": 1, "去": -1, "上": -1}.get(offset[:1])
            return _iso(ref.year + base, 1, 1) if base is not None else ""
        if unit in {"季度", "学期"}:
            return ""
    except (OverflowError, ValueError):
        return ""
    return ""


def looks_like_a_time(text: str, *, reference_date: str = "") -> bool:
    """这个文本是否承载时间语义（含开放与相对）。供契约层校验用。"""
    return bool(parse_time(text, reference_date=reference_date).basis)


def render_time_value(payload: object) -> str:
    """把类型化时间渲染成人类可读字符串。

    这是**表现适配**的唯一出口：存储是结构化的，展示是字符串的，
    前端与导出只依赖这里，因此契约升级不会波及消费方。
    """
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    basis = payload.get("basis")
    text = str(payload.get("text") or "")
    normalized = str(payload.get("normalized") or "")
    if basis == "not_mentioned":
        return "未提及"
    if basis == "open_ended":
        return text or "未明确"
    if basis == "derived":
        if text and normalized:
            return f"{text}（{normalized}）"
        return normalized or text
    if basis == "model_inferred":
        return f"{text or normalized}（模型建议，会上未提出）" if (text or normalized) else "模型建议"
    return text or normalized
