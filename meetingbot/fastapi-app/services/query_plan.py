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
#
# **四类，不是五类**：曾经的 `procedure`（流程/机制解释）与 `single_fact` **路由动作完全相同**
# （都走检索），而两版提示词下它的 F1 从 53% 摆到 12%——说明这条边界不可稳定测量，
# 只产出标签噪声。行业口径一致：分类只在"动作不同"的地方才有价值
# （Anthropic《Building Effective Agents》的 routing 一节、Amazon REIC 的分层路由）。
# 合并后每一类都对应一个不同动作：smalltalk→不检索；cross_meeting→必须分别检索多来源；
# aggregation→先列清单再检索；single_fact→检索。
INTENTS = ("single_fact", "aggregation", "cross_meeting", "smalltalk")
# 可建议的工具名（与 kb_tools.TOOL_SPECS 一致；多了就是幻觉）
TOOL_NAMES = ("search_knowledge", "list_facts", "get_minutes", "get_transcript_excerpt")

PLAN_SYSTEM = (
    "你在做**查询规划**：读一个用户问题，输出意图、改写后的问题、建议的工具，"
    "必要时把并列的多问句拆成子问题。\n"
    "\n"
    "## 意图：按顺序判，第一个「是」就停\n"
    "1. 这是寒暄、道谢，或与会议内容无关的请求（打招呼、问你能做什么、问天气）？→ smalltalk\n"
    "2. 答案必须**同时**来自多段录音或多场会议的比较/汇总（「分别」「各自」「两段」「两场」这类\n"
    "   比对语义）？→ cross_meeting\n"
    "3. 答案天然是一张**列表**（≥2 项：「有哪些」「哪几项」「分别是什么」）？→ aggregation\n"
    "4. 以上都不是（答案是一个具体的时间、数值、负责人、某一条决定，或一套怎么处理的说明）\n"
    "   → single_fact\n"
    "\n"
    "**按「答案形态」判，不按疑问词判**：问原因、后果、触发条件、状态、处理方式的，答案仍然\n"
    "是可以说清的一件事 → single_fact（「怎么处理」「为什么」不是另一个类别）。\n"
    "**复合问题按「动作更稀缺的那一类」判**（判成哪一类决定下游做哪些额外动作，\n"
    "动作有包含关系时取更严格的那个）：\n"
    "- 既要跨来源（多段录音/多场会议）又要枚举 → **cross_meeting**（跨来源本来就要分别检索，\n"
    "  枚举在其中自然完成）；\n"
    "- 既要枚举又有单点（「谁负责、什么时候完成」）→ **aggregation**（要说多个并列项）；\n"
    "- 都不是复合的，按上面的顺序判。\n"
    "\n"
    "## 置信度与弃权\n"
    "confidence（0–1，**必填**）：边界清楚（上面的判据一句话能对上）给 0.8 以上；\n"
    "两类之间摇摆、或问题本身含糊给 0.5 以下。**宁可给低分也不要硬猜**——\n"
    "低分时下游会退回「按检索处理」，那比送错工具安全。\n"
    "\n"
    "## 改写（rewritten）\n"
    "- 只有问题里出现相对时间或指代（最近、上周、下午这场会…）时才改；\n"
    "- 只把它们落到下面给定的已知事实上，**落不下来就原样返回问题**，不要编造日期或会议名；\n"
    "- 已知信息只有下面给的会议清单与今天的日期，别的都不要猜。\n"
    "\n"
    "## 分解（sub_questions）\n"
    "问题里包含**并列的多个问题**（一次问了两件以上、每件各自需要检索）时，拆成自包含的\n"
    "子问题：每条脱离上下文也能读懂（代词换成人名/会议名，补上必要限定），不要照抄长句。\n"
    "没有并列就填 `[]`（**这个字段必须出现**）——不要为了凑数把单问句拆开。\n"
    "\n"
    "## 建议工具（suggested_tools）与 needs_retrieval\n"
    "- 从 search_knowledge / list_facts / get_minutes / get_transcript_excerpt 里选 0–2 个：\n"
    "  枚举类先 list_facts 看清单或日期，单点事实走 search_knowledge。\n"
    "- needs_retrieval：这个问题要不要查会议资料（只有寒暄类才是 false）。\n"
    "只输出 JSON，不要解释。"
)
PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string"},
        "confidence": {"type": "number"},
        "rewritten": {"type": "string"},
        "sub_questions": {"type": "array", "items": {"type": "string"}},
        "suggested_tools": {"type": "array", "items": {"type": "string"}},
        "needs_retrieval": {"type": "boolean"},
    },
    "required": ["intent", "confidence", "rewritten", "sub_questions"],
}

# 低于这个置信度就按"弃权"处理：提示里明说按检索走、并且**不给工具建议**
# （行业共识：**宁可多检索，也不要送错工具**；低置信度下的建议本身就是噪声）
LOW_CONFIDENCE = 0.6
# 子问句上限：超过这个数说明它不是在拆并列，而是在替用户重写整段需求
MAX_SUB_QUESTIONS = 4


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
    # 置信度：范围外的值不丢也不照收，夹到 [0,1] 并记一笔（观测页能看到模型给了什么）
    confidence: float | None = None
    raw_confidence = data.get("confidence")
    if raw_confidence is None:
        issues.append("模型没给置信度（按「未知」处理，不当作低置信）")
    if raw_confidence is not None:
        try:
            confidence = min(1.0, max(0.0, float(raw_confidence)))
        except (TypeError, ValueError):
            issues.append(f"置信度不是数字：{raw_confidence!r}")
    # 子问句：自包含的并列子问题（去掉空串、去重、与整句重复的丢掉、限个上限）
    sub_questions: list[str] = []
    for raw in data.get("sub_questions") or []:
        text = str(raw).strip()
        if text and text != question and text not in sub_questions:
            sub_questions.append(text)
    if len(sub_questions) > MAX_SUB_QUESTIONS:
        issues.append(f"子问句过多（{len(sub_questions)} 条），只取前 {MAX_SUB_QUESTIONS} 条")
        sub_questions = sub_questions[:MAX_SUB_QUESTIONS]
    if len(sub_questions) == 1:
        # 只有一条子问题不算分解（那只是把问题换了个说法）
        sub_questions = []
    needs_retrieval = data.get("needs_retrieval")
    low_confidence = confidence is not None and confidence < LOW_CONFIDENCE
    if low_confidence and tools:
        # 弃权：低置信度下的工具建议本身就是噪声，丢掉它、按检索走
        issues.append(f"置信度 {confidence:.2f} 低于 {LOW_CONFIDENCE}：按检索处理，不用工具建议")
        tools = []
    return {
        "intent": intent,
        "confidence": confidence,
        "low_confidence": low_confidence,
        "rewritten": rewritten,
        "sub_questions": sub_questions,
        "suggested_tools": tools,
        # 低置信度时**一律要求检索**：宁可多检索，也不要因为一次猜错而跳过检索
        "needs_retrieval": True if (needs_retrieval is None or low_confidence)
        else bool(needs_retrieval),
        "issues": issues,
    }


def plan_hint(plan: Mapping[str, Any]) -> str:
    """把规划渲染成一条给模型的提示。**不替换问题**，只是放在问题前面的一句话。

    三处按行业共识做的取舍：
    - 改写那一句只在触发门命中时才带上（`rewrite_in_scope`）——没有相对时间/指代的问题
      本来就没有改写空间，带上它只是噪声；
    - **低置信度时明说"按检索处理"**（弃权）。路由判错比不路由更糟，弃权就是那条回退路径；
    - **并列子问题逐条列出并要求分别检索**（Azure RAG 的分解口径：子问题自包含、
      各检索一次、再合并上下文），而不是让模型自己在长问题里找边界。
    """
    parts = [f"查询规划：这个问题属于「{_intent_label(str(plan.get('intent')))}」"]
    rewritten = str(plan.get("rewritten") or "").strip()
    if rewritten and (plan.get("rewrite_in_scope") or rewritten != str(plan.get("question") or "").strip()):
        parts.append(f"（把相对时间与指代落到事实后是：{rewritten}）")
    sub_questions = plan.get("sub_questions") or []
    if sub_questions:
        listing = "；".join(f"{index}. {text}" for index, text in enumerate(sub_questions, start=1))
        parts.append(f"。这是并列的 {len(sub_questions)} 件事，请**分别检索**后再合并回答：{listing}")
    tools = plan.get("suggested_tools") or []
    if tools:
        parts.append(f"；建议先用 {'、'.join(tools)}")
    if plan.get("low_confidence"):
        parts.append("；规划置信度低，**按检索处理**（不要跳过检索、不要少查，"
                     "**也不要用日期或来源把范围缩小**——限定猜错会把正确内容挡在外面）")
    elif plan.get("needs_retrieval") is False:
        parts.append("；这轮不需要检索会议资料")
    parts.append("。这只是建议：以事实为准，落不下来就照原问题回答。")
    return "".join(parts)


_INTENT_LABELS = {
    "single_fact": "单点事实", "aggregation": "枚举聚合", "cross_meeting": "跨会议汇总",
    "smalltalk": "寒暄",
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
