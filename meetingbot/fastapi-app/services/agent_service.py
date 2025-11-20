import asyncio
import json
import os
from dataclasses import dataclass
from time import perf_counter

from common.prompts import render_prompt
from common.times import now
from models import (
    AgentRun,
    AgentStep,
    AiModelConfig,
    Meeting,
    MeetingMinutes,
    PromptTemplate,
    TranscriptionTask,
)
from services.agent_graph import build_review_graph, recursion_limit, review_passed
from services.minutes_service import MinutesResult
from services import contracts
from services.model_factory import invoke_json

def get_transcript_char_limit() -> int:
    """读取审查原文字符预算；此值不是模型的 Token 上下文窗口。"""
    value = os.getenv("AGENT_REVIEW_MAX_TRANSCRIPT_CHARS", "100000").strip()
    try:
        limit = int(value)
    except ValueError as error:
        raise ValueError("AGENT_REVIEW_MAX_TRANSCRIPT_CHARS 必须为正整数") from error
    if limit <= 0:
        raise ValueError("AGENT_REVIEW_MAX_TRANSCRIPT_CHARS 必须为正整数")
    return limit


# 在服务进程启动时读取配置，默认覆盖当前公开集中 32728 字符的长会议全文。
# 还需为纪要、提示词和输出预留模型 Token 空间，字符上限不保证任意模型都能接收。
MAX_TRANSCRIPT_CHARS = get_transcript_char_limit()

# 纪要里参与自检的六个字段，顺序和页面上的展示顺序一致
MINUTES_FIELDS = ("summary", "topics", "viewpoints", "decisions", "pending_items", "risks")

# 纪要自检的审查结果结构。passed 为真或者 issues 为空，反思环就结束，
# 所以这一环到底转几圈是模型在这里说了算，代码只负责兜住轮数上限。
# 审查结果结构由 contracts.review_json_schema() 从证据策略表派生：
# issue 的枚举与每类要求的证据字段都来自 ISSUE_EVIDENCE_POLICY，因此模型被要求的
# 结构、校验器检查的条件、提示词里的说明三者同源，不可能互相漂移。
REVIEW_SCHEMA = contracts.review_json_schema()

# issue_type 的中文说明，拼进步骤记录和重写提示词
ISSUE_TYPE_TEXT = {
    "MISSING_OWNER": "没有明确负责人",
    "MISSING_DEADLINE": "没有明确完成时间",
    "NOT_ACTIONABLE": "无法落成可执行任务",
    "VAGUE": "表述含糊",
    "UNSUPPORTED": "转写原文里找不到依据",
    "MISSING_ITEM": "原文里讨论过但纪要漏记",
}

# field 的中文名称，拼出“决策第1条”这样的位置描述
FIELD_TEXT = {
    "summary": "会议摘要",
    "topics": "议题",
    "viewpoints": "发言观点",
    "decisions": "决策",
    "pending_items": "待确认事项",
    "risks": "风险与争议",
}


@dataclass
class ReviewContext:
    """一次自检运行要用到的全部材料。

    审查和重写两步要用的提示词、结构约束和变量都收在这里，
    下面的反思环主流程只管转圈，不关心材料是怎么来的。
    """

    # 当前待审的内容，反思环每重写一次就换成新的一版
    working: dict
    # 第 10 章维护的 MINUTES_REVIEW 和 MINUTES_REFINE 两条模板
    review_prompt: PromptTemplate
    refine_prompt: PromptTemplate
    # 审查用 REVIEW_SCHEMA，重写用 MINUTES_SCHEMA
    review_schema: dict
    refine_schema: dict
    # 当前内容填进提示词里的哪个花括号变量
    content_variable: str
    # 提示词里除了当前内容之外的固定变量，例如会议主题、转写原文、参会名单
    review_variables: dict
    refine_variables: dict


def minutes_content(minutes: MeetingMinutes) -> dict:
    """把纪要表里分散的六个字段拼成一份完整稿，纪要自检从头到尾操作的都是这个结构。"""
    return {
        # 摘要为空时用空字符串，五个 JSON 字段为空时用空数组
        "summary": minutes.summary or "",
        "topics": minutes.topics_json or [],
        "viewpoints": minutes.viewpoints_json or [],
        "decisions": minutes.decisions_json or [],
        "pending_items": minutes.pending_items_json or [],
        "risks": minutes.risks_json or [],
    }


async def transcript_excerpt(task_id: int) -> str:
    # task_id 是纪要的 source_task_id，full_text 在第 8 章转写完成时写入、第 9 章修订片段后重新拼接
    task = await TranscriptionTask.get_or_none(id=task_id)
    text = (task.full_text if task else "") or ""
    # 不超过上限时原样返回完整转写
    if len(text) <= MAX_TRANSCRIPT_CHARS:
        return text
    # 明确实际截取范围；前缀不一定是“前半部分”，未提供的后文也不等于没有依据。
    return text[:MAX_TRANSCRIPT_CHARS] + (
        f"\n（转写原文共{len(text)}个字符，超过审查上限；"
        f"此处仅提供前{MAX_TRANSCRIPT_CHARS}个字符，后续内容未提供，"
        "不能据此断言后文不存在相关依据。）"
    )


def issue_line(issue: dict) -> str:
    """把一条问题拼成人能读的一行，页面上的问题清单和喂给重写模型的文本都用它。"""
    # 字段名翻成中文，取不到时保留原值
    field = FIELD_TEXT.get(issue.get("field"), issue.get("field"))
    index = issue.get("index")
    # index 是数组下标从 0 开始；整块内容都空着时模型填 -1，这时候不写第几条
    position = f"{field}第{index + 1}条" if isinstance(index, int) and index >= 0 else f"{field}整体"
    kind = ISSUE_TYPE_TEXT.get(issue.get("issue_type"), issue.get("issue_type"))
    # 例如：决策第1条｜没有明确负责人：未写负责人 → 建议：补上张涛
    return f"{position}｜{kind}：{issue.get('detail')} → 建议：{issue.get('suggestion')}"


async def call_model(
    run: AgentRun,
    config: AiModelConfig,
    system_prompt: str,
    user_prompt: str,
    schema: dict,
    call_type: str,
) -> dict:
    """调一次自检模型并要求返回 JSON，成功和失败都写一条 AI 调用日志。"""
    # call_type 是 AGENT_REVIEW 或 AGENT_REFINE；biz_id 是这次自检运行，观测页按它判断员工能不能看这条日志
    return await invoke_json(config, system_prompt, user_prompt, schema, call_type, run.id, "自检模型")


async def open_step(run: AgentRun, round_no: int, step_type: str, decision: str) -> AgentStep:
    """开一条步骤记录，顺便把运行上的阶段和进度刷新一次。

    步骤号在整次运行内连续递增，轮次号把同一轮的审查和重写归到一起。
    """
    # 步骤号按已有条数加一算，中断恢复后接着往下排，不会和已有记录撞唯一索引
    step_no = await AgentStep.filter(run_id=run.id).count() + 1
    # 一轮两步（审查加重写），最后还有一步汇总，这就是整次运行最多的步数
    total_steps = run.max_rounds * 2 + 1
    # 封顶 95，留最后 5 个点给汇总步结束时置 100，免得进度条提前走满
    progress = min(95, int(step_no * 100 / total_steps))
    # decision 同时作为运行的当前阶段，列表和详情弹窗显示它
    await AgentRun.filter(id=run.id).update(stage=decision, progress=progress)
    # 新步骤的 status 取模型默认值 RUNNING
    return await AgentStep.create(
        run_id=run.id,
        step_no=step_no,
        round_no=round_no,
        step_type=step_type,
        decision_summary=decision,
    )


async def close_step(step: AgentStep, started: float, summary: str, payload: dict | None = None):
    await AgentStep.filter(id=step.id).update(
        status="SUCCEEDED",
        # 结论文本最多存 2000 字
        observation_summary=summary[:2000],
        # 审查步存判定、得分和问题清单，重写步存改完的整份纪要，汇总步为 None
        payload_json=payload,
        # started 是步骤开始时 perf_counter() 的读数，相减得到耗时毫秒
        elapsed_ms=int((perf_counter() - started) * 1000),
        finish_time=now(),
    )


async def fail_step(step: AgentStep, started: float, error: Exception):
    await AgentStep.filter(id=step.id).update(
        status="FAILED",
        # 异常信息为空字符串时记异常类型名
        error_message=(str(error) or type(error).__name__)[:2000],
        elapsed_ms=int((perf_counter() - started) * 1000),
        finish_time=now(),
    )


async def build_minutes_context(run: AgentRun, minutes: MeetingMinutes, meeting: Meeting) -> ReviewContext:
    """纪要自检的材料：审查要对着转写原文看，重写产出的是一份完整纪要。"""
    transcript = await transcript_excerpt(minutes.source_task_id)
    return ReviewContext(
        # 中断恢复时接着上一次改到的稿子继续，一次都没改过就从纪要原文开始
        working=run.revised_json or minutes_content(minutes),
        # 模板被停用时 PromptTemplate.get 抛异常，主流程把运行记为失败
        review_prompt=await PromptTemplate.get(code="MINUTES_REVIEW", enabled=True),
        # 重写模板按 MINUTES_REFINE 读取，当前稿件与本轮问题在 refine_round 填入。
        refine_prompt=await PromptTemplate.get(code="MINUTES_REFINE", enabled=True),
        review_schema=REVIEW_SCHEMA,
        # 重写的产出要能直接当纪要用，所以结构约束用纪要生成那份 SCHEMA
        refine_schema=contracts.refine_json_schema(),
        # 两条模板里当前纪要都写成 {minutes_json}
        content_variable="minutes_json",
        # 审查模板还有 {meeting_title} 和 {transcript}
        review_variables={"meeting_title": meeting.title, "transcript": transcript},
        # 重写也使用与审查相同的转写节选，避免仅凭审查意见补写无依据的事实。
        # 旧模板不含 {transcript} 时，render_prompt 会忽略这个额外变量。
        refine_variables={"meeting_title": meeting.title, "transcript": transcript},
    )


async def review_round(
    run: AgentRun,
    config: AiModelConfig,
    context: ReviewContext,
    working: dict,
    round_no: int,
) -> dict:
    """审查步：让模型检查 working 这一版，返回问题清单和是否通过。"""
    # decision 例如“第1轮：审查是否可落实”
    step = await open_step(run, round_no, "REVIEW", f"第{round_no}轮：审查是否可落实")
    started = perf_counter()
    try:
        result = await call_model(
            run,
            config,
            context.review_prompt.system_prompt,
            # 当前这一版填进 content_variable 指定的那个花括号变量，其余变量来自 review_variables
            render_prompt(
                context.review_prompt.user_prompt,
                **{context.content_variable: json.dumps(working, ensure_ascii=False)},
                **context.review_variables,
            ),
            context.review_schema,
            "AGENT_REVIEW",
        )
        passed = review_passed(result)
        issues = result["issues"]
        summary = result.get("conclusion") or ""
        # 步骤记录里除了总评，还把每条问题拼成一行，时间线上不读 payload 也能看懂
        if issues:
            summary = summary + "\n" + "\n".join(issue_line(issue) for issue in issues)
        # 审查意见的证据门控：**硬拦**，不只是记录。
        # 没有前提的主观意见不许推动重写——冻结标准要求"任何重写都必须说明它依据哪条前提"。
        # 于是：证据不足的意见被挡在重写步之外（原样留在 payload 里供人查看），
        # 若一条都不剩，这一轮就视为"通过"，不再为了无法证成的意见改稿。
        # 转写原文取自审查变量：那正是这一步实际看到的那一段（可能已被上限截断）。
        reviewed_transcript = str(context.review_variables.get("transcript") or "")
        gate = contracts.gate_issues_by_evidence(issues, reviewed_transcript)
        kept_issues = list(gate["kept"])
        if gate["dropped"]:
            summary = summary + (
                f"\n（证据门控：挡下 {len(gate['dropped'])} 条无依据意见"
                f"：{gate['by_kind']}）"
            )
        if not kept_issues and issues:
            # 全被挡下：没有可执行的修改依据，这一轮按通过处理
            result["passed"] = True
            passed = True
            summary = summary + "\n（无一条意见满足证据要求，本轮视为通过）"
        issues = kept_issues
        result["issues"] = kept_issues
        # 第二层：**支撑**判定。字面定位通过 ≠ 依据支撑该判定——实测独立模型族在业界口径下
        # 只认可 25% 的 UNSUPPORTED 意见，而按字面它们全都"有依据"。因此再过一层：
        # 把这一轮的意见连同其依据一起交给模型复核，判不出支撑的挡下。
        # 批量化（每轮只多一次调用，不是每条一次），失败时**按不支撑处理**并记录——
        # 宁可少改一版，也不让没验证过的意见推动删除。
        # 先初始化：首轮直接通过时 kept_issues 为空、不会进下面这个分支，
        # 但 payload 里要写它（这个 UnboundLocalError 被产品测试当场抓到）
        support_dropped: list[dict[str, str]] = []
        if kept_issues:
            try:
                support_system, support_user = contracts.issue_support_prompt()
                verdicts = await call_model(
                    run, config, support_system,
                    render_prompt(
                        support_user,
                        transcript=reviewed_transcript,
                        issues="\n".join(
                            f"{n}. [{i.get('issue_type')}] {i.get('detail') or ''} "
                            f"依据：{i.get('source_span') or i.get('target_text') or '（未给）'}"
                            for n, i in enumerate(kept_issues, start=1)
                        ),
                    ),
                    contracts.issue_support_schema(),
                    "AGENT_REVIEW_SUPPORT",
                )
                filtered = contracts.filter_issues_by_support(
                    kept_issues, (verdicts or {}).get("verdicts") or []
                )
                kept_issues = list(filtered["kept"])
                support_dropped = filtered["dropped"]
            except Exception as error:
                # 复核失败不阻断自检：把整轮意见按"未验证"挡下并留消息，而不是放行
                support_dropped = [{"issue": dict(i), "reasons": ["support_check_failed"],
                                    "detail": f"{type(error).__name__}: {str(error)[:160]}"}
                                   for i in kept_issues]
                kept_issues = []
            if support_dropped:
                summary = summary + (
                    f"\n（支撑复核：挡下 {len(support_dropped)} 条依据不支撑其判定的意见）"
                )
            if not kept_issues:
                result["passed"] = True
                passed = True
                summary = summary + "\n（没有一条意见通过证据与支撑双层门控，本轮视为通过）"
        result["issues"] = kept_issues
        evidence_report = contracts.conformance_report(
            working, reviewed_transcript, issues=kept_issues,
        )
        await close_step(
            step,
            started,
            summary,
            # payload 保留结构化判定、**留下的**问题、以及**被挡下的**问题；
            # 依据校验结果一并留存，观测与评测直接读它，不再各自实现一遍
            {
                "passed": passed,
                "score": int(result.get("score") or 0),
                "issues": kept_issues,
                "issues_dropped_by_evidence_gate": gate["dropped"],
                "evidence_gate": {"checked": gate["checked"],
                                  "kept_after_evidence": len(gate["kept"]),
                                  "dropped_by_evidence": len(gate["dropped"]),
                                  "dropped_by_support": len(support_dropped),
                                  "final_kept": len(kept_issues),
                                  "by_kind": gate["by_kind"]},
                "issues_dropped_by_support_check": support_dropped,
                "evidence_check": evidence_report,
            },
        )
        return result
    except Exception as error:
        # 模型调用失败时这一步记成 FAILED，异常继续往上抛，由主流程把整次运行记成失败
        await fail_step(step, started, error)
        raise


async def refine_round(
    run: AgentRun,
    config: AiModelConfig,
    context: ReviewContext,
    working: dict,
    issues: list[dict],
    round_no: int,
) -> dict:
    """重写步：把 working 这一版和上一步的问题清单交回模型，让它改出新的一版。

    自由重写整篇 + dispositions 删除声明 + 事后保默认（enforce_preservation）。
    （历史上的 edits 编辑指令协议已删——未过留出集验证的实验路径不留双版本。）
    """
    # decision 例如“第1轮：按 4 条问题重写”
    step = await open_step(run, round_no, "REFINE", f"第{round_no}轮：按 {len(issues)} 条问题重写")
    started = perf_counter()
    try:
        result = await call_model(
            run,
            config,
            context.refine_prompt.system_prompt,
            render_prompt(
                context.refine_prompt.user_prompt,
                **{context.content_variable: json.dumps(working, ensure_ascii=False)},
                # 问题清单逐条拼成一行，填进重写模板的 {issues}
                issues="\n".join(issue_line(issue) for issue in issues),
                **context.refine_variables,
            ),
            # MINUTES_SCHEMA，重写结果和第 11 章生成的纪要是同一个结构
            context.refine_schema,
            "AGENT_REFINE",
        )
        # 复用纪要生成时的校验模型，字段缺失或者类型不对会在这里直接抛出来
        revised = MinutesResult.model_validate(result).model_dump()
        # 内容整体归零必须当失败处理，不能当成"更精简的一版"。
        # 实测事故（vcsum_21）：模型只返回 {"summary": ...}，而模型五个列表字段都有默认空列表，
        # 于是校验通过、运行判定为"自检通过"，等人点确认就把初稿 63 条全删掉。
        # 这里拦在写库之前：宁可让这一步失败并留下消息，也不要给出一个"看起来通过"的空稿。
        if contracts.is_content_empty_rewrite(working, revised):
            raise ValueError(
                "重写结果内容整体为空：初稿有 "
                f"{contracts.list_item_count(working)} 条内容，改写稿五个列表字段全空。"
                "这通常意味着模型返回了残缺 JSON（只有 summary），而默认值把它补成了"
                "「合法的空」——按失败处理，不写入修订稿。"
            )
        # 保默认的**结构性保证**：初稿里有、这一版消失、又没有 delete 声明的条目一律恢复。
        # 此前只做了测量（能算出静默删除约 3 条/场）而没有机制，所以比率与旧配置持平。
        # 恢复是纯内存运算，不需要额外模型调用；要真正删掉某条，必须显式声明并附理由与证据。
        # 传入转写：保留判据从"一律恢复"升级为"**只恢复本来有依据的**"。
        # 实测：历史 80 条消失条目里 77 条（96%）带可定位证据（过度修订），
        # 只有 3 条真正无依据——全盘恢复会让产品删不掉无依据内容，全按声明又会因
        # 模型极少声明（25/26 场）而几乎删不动。判据看条目自己的证据。
        revised, preservation = contracts.enforce_preservation(
            working, revised, result.get("dispositions"), transcript=str(
                context.refine_variables.get("transcript") or ""
            )
        )
        # 变更清单：**与模型无关的纯计算**，把"整篇重写"变成"改了几条、改了哪几条"。
        # 依据：真实库量到 Zero Rate 只有 1.4%（六字段全未改），也就是几乎每次自检都
        # 把一份全文改写的稿子摆到人面前；同一批数据 114/145 停在等人确认。
        # 审阅成本高到没人审——这是交付形态的问题，不是 UI 问题。
        change_list = contracts.build_change_list(working, revised)
        if preservation["restored_count"] or preservation.get("dropped_ungrounded_count"):
            await AgentStep.filter(id=step.id).update(
                observation_summary=(
                    f"已按 {len(issues)} 条问题重写；恢复 {preservation['restored_count']} "
                    f"条未声明即消失的**有依据**条目（保默认），"
                    f"允许丢弃 {preservation.get('dropped_ungrounded_count', 0)} 条原本无依据的条目；"
                    f"{change_list['summary_line']}"
                )[:2000]
            )
        # payload 存改完的整份纪要 + 变更清单（页面据此渲染逐条改动，不必读整篇比对）
        payload = {**revised, "change_list": change_list}
        await close_step(step, started,
                         f"已按 {len(issues)} 条问题重写（{change_list['summary_line']}）",
                         payload)
        return revised
    except Exception as error:
        await fail_step(step, started, error)
        raise


def dump_final_result(payload: dict) -> str:
    """final_result 列是 TextField：结构化内容必须整体 JSON 序列化后才能落库。"""
    return json.dumps(payload, ensure_ascii=False)


def load_final_result(value) -> dict:
    """把 final_result 列读回 dict，兼容三种历史形态。

    dict 直接返回（尚未落库的内存对象）；JSON 字符串正常解析；
    纯文本（只有自检结论一句话的旧数据）归一成 {"text": 原文}，
    于是"结论文本"与"审阅埋点"两种用途可以合并在同一列而不互相覆盖。
    """
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {"text": str(value)}
    return parsed if isinstance(parsed, dict) else {"text": str(value)}


def final_summary(run: AgentRun) -> str:
    """反思环跑完时拼的一句话结论。"""
    # 没有修订稿：第一轮审查就通过
    if run.revised_json is None:
        return f"自检通过，得分 {run.score}，纪要无需修订。"
    # 有修订稿且最后一轮判定通过：改过之后复审通过
    if run.passed:
        return f"经过 {run.current_round} 轮自检，最后一轮判定通过，得分 {run.score}，等待人工确认是否应用修订。"
    # 有修订稿且最后一轮没通过：到轮数上限后收尾
    return (
        f"已按审查发现的 {run.issue_count} 条问题重写纪要，得分 {run.score}，"
        f"达到 {run.max_rounds} 轮上限后没有再复审，请人工判断是否采用修订稿。"
    )


async def finalize_run(run_id: int):
    """汇总步：记一条 FINAL 步骤，按有没有改过稿决定运行直接结束还是等人确认。"""
    # 重新读取运行记录，拿到 do_review、do_refine 写入的最新字段
    run = await AgentRun.get(id=run_id)
    step = await open_step(run, run.current_round, "FINAL", "汇总自检结论")
    started = perf_counter()
    final = final_summary(run)
    await close_step(step, started, final)
    if run.revised_json is None:
        # 第一次审查就通过，内容一个字没动，运行到此结束
        await AgentRun.filter(id=run.id).update(
            status="SUCCEEDED", stage="自检通过", progress=100,
            final_result=dump_final_result({"text": final}), finished_at=now()
        )
        return
    # 产出不会自己写回业务表，必须由人点确认，这是本项目 Agent 唯一的写操作确认点
    await AgentRun.filter(id=run.id).update(
        status="WAITING_CONFIRMATION", stage="等待人工确认", progress=100,
        final_result=dump_final_result({"text": final}), finished_at=now(),
    )


async def execute_agent_run(run_id: int):
    """反思环主流程。

    一轮等于「审查一次 + 按问题改一版」。流程画在 agent_graph 的状态图里，
    这里负责备好材料，再把审查、重写、汇总、中断检查四个动作交给状态图调度。
    """
    try:
        run = await AgentRun.get_or_none(id=run_id)
        # 状态不是 RUNNING 说明已经被中断或者已经跑完，直接退出，不重复执行
        if run is None or run.status != "RUNNING":
            return
        # 服务上次被强行关闭时，正在执行的那一步没来得及写结果，续跑前先把它记成失败
        await AgentStep.filter(run_id=run.id, status="RUNNING").update(
            status="FAILED", error_message="上次执行被打断，本步未完成", finish_time=now()
        )
        # 发起时记下的自检模型配置，整次运行都用这一条
        config = await AiModelConfig.get(id=run.model_config_id)
        meeting = await Meeting.get(id=run.meeting_id)
        minutes = await MeetingMinutes.get(id=run.minutes_id)
        context = await build_minutes_context(run, minutes, meeting)

        async def do_review(working: dict, round_no: int) -> dict:
            """审查一次，并把这一轮的判定落到运行记录上。"""
            result = await review_round(run, config, context, working, round_no)
            passed = review_passed(result)
            issues = result["issues"]
            await AgentRun.filter(id=run.id).update(
                # 轮次加一，页面上的「2 / 2」前半段跟着变
                current_round=round_no,
                passed=passed,
                score=int(result.get("score") or 0),
                issue_count=len(issues),
                # 只保留最后一轮的问题清单，前面几轮的在 agent_step 里能查到
                review_json=issues,
            )
            return result

        async def do_refine(working: dict, issues: list[dict], round_no: int) -> dict:
            """按问题改一版，改完就存进运行记录，中断之后恢复能接着这一版继续。"""
            revised = await refine_round(run, config, context, working, issues, round_no)
            await AgentRun.filter(id=run.id).update(revised_json=revised)
            return revised

        async def should_stop() -> bool:
            # 重新读一次运行记录，人在页面上点了中断，这里读到的状态就不是 RUNNING
            current = await AgentRun.get(id=run.id)
            return current.status != "RUNNING"

        async def do_finalize() -> None:
            await finalize_run(run.id)

        # 第一个参数是状态图的初始状态，第二个参数是运行配置
        await build_review_graph().ainvoke(
            {
                # 中断恢复时从上一次改到的稿子和已完成的轮次接着跑
                "working": context.working,
                "round_no": run.current_round,
                "max_rounds": run.max_rounds,
                "review": do_review,
                "refine": do_refine,
                "finalize": do_finalize,
                "should_stop": should_stop,
            },
            {"recursion_limit": recursion_limit(run.max_rounds)},
        )
    except asyncio.CancelledError:
        # 页面点中断时会 cancel 这个任务，运行状态已经在接口里改成 INTERRUPTED。
        # 被打断的那一步停在模型调用中途，把它记成失败，时间线上就不会一直显示 RUNNING
        await AgentStep.filter(run_id=run_id, status="RUNNING").update(
            status="FAILED", error_message="运行已中断，本步未完成", finish_time=now()
        )
        return
    except Exception as error:
        # 模型调用失败、结构校验失败、模板被停用等异常，都把整次运行记为 FAILED
        await AgentRun.filter(id=run_id).update(
            status="FAILED",
            stage="自检失败",
            error_message=(str(error) or type(error).__name__)[:2000],
            finished_at=now(),
        )


def schedule_agent_run(run_id: int):
    """把自检运行交给 Temporal 执行（同步启动，不等待完成）。"""
    from services import task_queue
    asyncio.get_running_loop().create_task(
        task_queue.enqueue(task_queue.KIND_AGENT_RUN, run_id, (run_id,)))


def cancel_agent_run(run_id: int):
    """请求取消自检 workflow；运行状态由中断接口先写好。"""
    from services import task_queue
    asyncio.get_running_loop().create_task(
        task_queue.cancel(task_queue.KIND_AGENT_RUN, run_id))


async def recover_agent_runs():
    """兜底扫尾：重新入队非终态运行行（崩溃续跑由 Temporal 原生完成）。"""
    from services import task_queue
    ids = await AgentRun.filter(status="RUNNING").values_list("id", flat=True)
    for run_id in ids:
        await task_queue.enqueue(task_queue.KIND_AGENT_RUN, run_id, (run_id,))
