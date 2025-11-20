"""查询规划：把"最近七天""下午这场会"这类限定改写成可执行的条件，并判意图。

## 它是什么、不是什么

它是**一次调用产出一份"建议"**，进上下文的方式是**提示**（一条系统消息），
既不替换用户的问题、也不改工具集、更不强制 `tool_choice`：

- 提示式而不是强制式：**路由判错比不路由更糟**（把枚举问题送进单点检索、把寒暄送进
  知识库，用户拿不到本来能查到的内容）。所以先让它"说一句建议"，用逐类 F1 量准了
  再谈要不要升级成强制。
- 改写是**可归因**的：整份规划（触发词、意图、改写结果、建议工具）落 `chat_message.plan_json`。
  一轮答错时能分开"改写把问题带偏了"与"检索没找到"——这两件事的修法完全不同。
- **不确定就不改写**：模型落不下来（会议清单里没有对应日期/录音）时要求它照抄原问题，
  并在 `needs_retrieval=false` 时明确标注。实测的教训：让模型自己猜日期会猜错
  （把 2026 年的会猜成 2024 年，一轮 3 条金标事实一条没检索到）。

## 触发门管的是**改写**，不是"做不做规划"

它一度被写成"没问题里没有相对时间/指代就跳过整次规划"——那是错的：**意图与建议工具
对每一道实质问题都有用**（枚举类问题往往一个时间词都没有，而它正是路由最该提醒的那类：
"首周复盘要看哪五项数据"没有触发词，但模型很容易只走一次检索）。实测也因此暴露：
按旧口径跑 100 题只有 12 题拿到决策，F1 根本量不了。

现在的分工：开关打开时**每轮一次规划调用**；触发门只决定**改写**那一句要不要带上
（没有相对时间/指代时没有改写空间，带上只是噪声）。开关默认关，成本口径见文档。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Mapping, Sequence

from models import AiModelConfig

from services import model_factory

logger = logging.getLogger("meetingbot.query_plan")

# 调用类型与开关（新调用类型必须登记 CALL_TYPE_OWNERSHIP，见 observability_service）
PLAN_CALL_TYPE = "KB_QUERY_PLAN"
PLAN_ENABLED_ENV = "KB_QUERY_PLAN_ENABLED"

# 触发门：相对时间与指代。命中才规划（见模块文档"触发门"）
TRIGGER_PATTERN = re.compile(
    r"最近|近七天|近一周|这周|本周|上周|上个?月|本月|今天|昨天|前天|明天|"
    r"上午|下午|晚上|这场|那场|上一场|上一次|上次|这次|刚才|近期|前几?天|"
    r"七日|一周内|一个月内"
)
# 意图枚举：与 `evaluation/gold/qa_seed.json` 的 `intent_taxonomy` 一一对应。
# 两边必须同形，否则逐类 F1 会静默错位——有测试钉住这个集合。
INTENTS = ("single_fact", "aggregation", "cross_meeting", "procedure", "smalltalk")
# 可建议的工具名（与 kb_tools.TOOL_SPECS 一致；多了就是幻觉）
TOOL_NAMES = ("search_knowledge", "list_facts", "get_minutes", "get_transcript_excerpt")

PLAN_SYSTEM = (
    "你在做**查询规划**：读一个用户问题，输出三样东西——意图、改写后的问题、建议的工具。\n"
    "1. 意图（intent）从这五类里选一个：\n"
    "   - single_fact：问一个具体事实（时间、数值、负责人、某一条决定）\n"
    "   - aggregation：枚举/聚合（有哪些、哪几项、分别是什么）\n"
    "   - cross_meeting：答案必须来自多场会议或多段录音的比较/汇总\n"
    "   - procedure：流程与机制解释（怎么处理、流程是什么、为什么这样做）\n"
    "   - smalltalk：寒暄或与会议内容无关的请求\n"
    "2. 改写（rewritten）：只把**相对时间与指代**落到已知事实上：\n"
    "   - 只有用户问题里出现相对时间/指代（最近、上周、下午这场会…）时才改；\n"
    "   - 落不下来就**原样返回问题**，不要编造日期或会议名；\n"
    "   - 已知信息只有下面给的会议清单（含日期）与今天的日期，别的都不要猜。\n"
    "3. 建议工具（suggested_tools）：从 search_knowledge / list_facts / get_minutes / "
    "get_transcript_excerpt 里选 0–2 个（枚举类先 list_facts 看日期或清单，单点事实走检索）。\n"
    "4. needs_retrieval：这个问题要不要查会议资料（寒暄类为 false）。\n"
    "只输出 JSON，不要解释。"
)
PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string"},
        "rewritten": {"type": "string"},
        "suggested_tools": {"type": "array", "items": {"type": "string"}},
        "needs_retrieval": {"type": "boolean"},
    },
    "required": ["intent", "rewritten"],
}


def plan_enabled() -> bool:
    """规划开关。**默认关**：新机制默认关是本仓规矩（未实测就开默认=把没量过的
    机制塞进主链路），要开就显式配 KB_QUERY_PLAN_ENABLED=1。"""
    return os.getenv(PLAN_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def trigger_terms(question: str) -> list[str]:
    """命中的触发词（去重、保序）。空列表表示这一轮不值得规划。"""
    found: list[str] = []
    for match in TRIGGER_PATTERN.finditer(question or ""):
        term = match.group(0)
        if term not in found:
            found.append(term)
    return found


def normalize(parsed: Mapping[str, Any] | None, question: str) -> dict[str, Any]:
    """把模型给的规划收成一份可信的结构：意图不许越界、工具名不许幻觉、改写不许为空。

    越界的值**不静默丢弃也不照单全收**：改成最近的合法值并记 `issues`，
    让"模型给了什么"与"我们用了什么"都能在 plan_json 里对上。
    """
    issues: list[str] = []
    data = dict(parsed or {})
    intent = str(data.get("intent") or "").strip()
    if intent not in INTENTS:
        issues.append(f"意图越界：{intent or '（空）'}")
        intent = "single_fact"          # 默认走"单点事实"：不会去枚举、也不会跳过检索
    rewritten = str(data.get("rewritten") or "").strip()
    if not rewritten:
        issues.append("改写为空，回退原问题")
        rewritten = question
    tools: list[str] = []
    for raw in data.get("suggested_tools") or []:
        name = str(raw).strip()
        if name in TOOL_NAMES and name not in tools:
            tools.append(name)
        elif name:
            issues.append(f"工具名幻觉：{name}")
    needs_retrieval = data.get("needs_retrieval")
    return {
        "intent": intent,
        "rewritten": rewritten,
        "suggested_tools": tools,
        "needs_retrieval": True if needs_retrieval is None else bool(needs_retrieval),
        "issues": issues,
    }


def plan_hint(plan: Mapping[str, Any]) -> str:
    """把规划渲染成一条给模型的提示。**不替换问题**，只是放在问题前面的一句话。

    改写那一句只在触发门命中时才带上（`rewrite_in_scope`）——没有相对时间/指代的问题
    本来就没有改写空间，带上它只是噪声。
    """
    parts = [f"查询规划：这个问题属于「{_intent_label(str(plan.get('intent')))}」"]
    rewritten = str(plan.get("rewritten") or "").strip()
    if rewritten and (plan.get("rewrite_in_scope") or rewritten != str(plan.get("question") or "").strip()):
        parts.append(f"（把相对时间与指代落到事实后是：{rewritten}）")
    tools = plan.get("suggested_tools") or []
    if tools:
        parts.append(f"；建议先用 {'、'.join(tools)}")
    if plan.get("needs_retrieval") is False:
        parts.append("；这轮不需要检索会议资料")
    parts.append("。这只是建议：以事实为准，落不下来就照原问题回答。")
    return "".join(parts)


_INTENT_LABELS = {
    "single_fact": "单点事实", "aggregation": "枚举聚合", "cross_meeting": "跨会议汇总",
    "procedure": "流程解释", "smalltalk": "寒暄",
}


def _intent_label(intent: str) -> str:
    return _INTENT_LABELS.get(intent, intent)


async def _chat_config() -> AiModelConfig:
    config = await AiModelConfig.filter(model_type="AGENT", enabled=True).order_by("-id").first()
    if config is None:
        raise RuntimeError("没有启用中的 AGENT 用途模型配置，无法做查询规划")
    return config


def scope_lines(meetings: Sequence[Mapping[str, Any]], recordings: Sequence[Mapping[str, Any]],
                today: str) -> str:
    """给规划模型的"已知事实"：今天日期 + 会议清单（含日期）+ 录音段数。

    只给这些：改写要落到**系统确实知道**的实体上；给多了它就会去猜。
    """
    lines = [f"今天的日期：{today}", "可检索的会议："]
    if not meetings:
        lines.append("（无可见会议）")
    for meeting in meetings:
        lines.append(f"- id={meeting.get('id')} 标题={meeting.get('title')} "
                     f"会议日期={meeting.get('start_time')}")
    if recordings:
        lines.append("各会议的录音段数与时间：")
        for row in recordings:
            lines.append(f"- 会议 id={row.get('meeting_id')}：{row.get('count')} 段，"
                         f"时间 {row.get('first')} ～ {row.get('last')}")
    return "\n".join(lines)


async def plan_query(question: str, *, scope: str, biz_id: int,
                     model: Any = None) -> dict[str, Any]:
    """跑一次规划。**失败一律降级为"不注入"**，绝不让它挡住这一轮问答。

    返回结构（无论成功失败都同形，便于落 `plan_json`）：
    `{"enabled", "used", "triggers", "intent", "rewritten", "suggested_tools", ...}`
    """
    question = (question or "").strip()
    triggers = trigger_terms(question)
    base: dict[str, Any] = {"enabled": True, "used": False, "triggers": triggers,
                            "rewrite_in_scope": bool(triggers)}
    user = (f"{scope}\n\n【用户问题】\n{question}\n\n"
            + ("问题里出现了相对时间/指代，请把改写落到上方已知事实上；"
               if triggers else "问题里没有相对时间或指代，改写请原样返回问题；")
            + "请按格式输出 JSON。")
    try:
        config = await _chat_config()
        parsed = await model_factory.invoke_json(
            config, PLAN_SYSTEM, user, PLAN_SCHEMA, PLAN_CALL_TYPE, biz_id, "查询规划模型",
            validator=lambda data: normalize(data, question))
    except Exception as error:  # noqa: BLE001 - 规划失败不该挡住这一轮问答
        logger.exception("查询规划失败，本轮不注入规划提示（biz_id=%s）", biz_id)
        return {**base, "reason": f"规划调用失败：{(str(error) or type(error).__name__)[:200]}"}
    return {**base, "used": True, "question": question, **parsed}


__all__ = [
    "PLAN_CALL_TYPE", "PLAN_ENABLED_ENV", "INTENTS", "TOOL_NAMES", "PLAN_SYSTEM",
    "plan_enabled", "trigger_terms", "normalize", "plan_hint", "scope_lines", "plan_query",
]
