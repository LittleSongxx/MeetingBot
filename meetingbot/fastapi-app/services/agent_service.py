import asyncio
import json
import logging
from dataclasses import dataclass
from time import perf_counter

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
from settings import AGENT_REVIEW_MAX_TRANSCRIPT_CHARS

from services.agent_graph import build_review_graph, recursion_limit
from services.evidence_service import load_task_evidence_context, merge_evidence
from services.minutes_service import MINUTES_SCHEMA, MinutesResult, render_prompt
from services.model_factory import invoke_json

logger = logging.getLogger(__name__)

# 审查提示词最多读取转写全文的前多少个字符由 settings 决定（默认 20000）。
MAX_TRANSCRIPT_CHARS = AGENT_REVIEW_MAX_TRANSCRIPT_CHARS

# 纪要里参与自检的六个字段，顺序和页面上的展示顺序一致
MINUTES_FIELDS = ("summary", "topics", "viewpoints", "decisions", "pending_items", "risks")

_running_agents: dict[int, asyncio.Task] = {}


# 纪要自检的审查结果结构。passed 为真或者 issues 为空，反思环就结束，
# 所以这一环到底转几圈是模型在这里说了算，代码只负责兜住轮数上限。
REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        # 这一版纪要是否通过审查，review_node 据此决定走汇总还是重写
        "passed": {"type": "boolean"},
        # 0 到 100 的得分，写进 agent_run.score，列表和详情都展示它
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    # 问题出在纪要的哪一块，取值限定为 MINUTES_FIELDS 里的六个字段名
                    "field": {"type": "string", "enum": list(MINUTES_FIELDS)},
                    # 这一块里的第几条，从 0 开始；整块为空时审查模板要求填 -1
                    "index": {"type": "integer"},
                    # 问题类型，六个枚举值和审查模板里的两组标准对应
                    "issue_type": {
                        "type": "string",
                        "enum": [
                            "MISSING_OWNER",
                            "MISSING_DEADLINE",
                            "NOT_ACTIONABLE",
                            "VAGUE",
                            "UNSUPPORTED",
                            "MISSING_ITEM",
                        ],
                    },
                    # 具体哪里不对，详情弹窗的问题卡片显示它
                    "detail": {"type": "string"},
                    # 修改建议，重写步骤拿到的问题清单里带着它
                    "suggestion": {"type": "string"},
                },
                "required": ["field", "index", "issue_type", "detail", "suggestion"],
            },
        },
        # 一句话总评，写进审查步骤的 observation_summary
        "conclusion": {"type": "string"},
    },
    "required": ["passed", "score", "issues", "conclusion"],
}

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
    # 来源转写任务 ID，重写后用它加载片段做 evidence 重绑定
    task_id: int


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
    # 截断后加一句说明，让模型知道自己看到的不是全文，不要因为后半段缺失就判成漏记
    return text[:MAX_TRANSCRIPT_CHARS] + "\n（转写原文过长，此处只截取前半部分）"


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
        refine_schema=MINUTES_SCHEMA,
        # 两条模板里当前纪要都写成 {minutes_json}
        content_variable="minutes_json",
        # 审查模板还有 {meeting_title} 和 {transcript}
        review_variables={"meeting_title": meeting.title, "transcript": transcript},
        # 重写模板还有 {meeting_title}，{issues} 在重写步骤里单独传
        refine_variables={"meeting_title": meeting.title},
        task_id=minutes.source_task_id,
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
        issues = result.get("issues") or []
        summary = result.get("conclusion") or ""
        # 步骤记录里除了总评，还把每条问题拼成一行，时间线上不读 payload 也能看懂
        if issues:
            summary = summary + "\n" + "\n".join(issue_line(issue) for issue in issues)
        await close_step(
            step,
            started,
            summary,
            # payload 里保留结构化的判定和问题，详情弹窗按它渲染问题卡片
            {"passed": bool(result.get("passed")), "score": int(result.get("score") or 0), "issues": issues},
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
    """重写步：把 working 这一版和上一步的问题清单交回模型，让它改出新的一版。"""
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
        # 改稿时保留/重绑定证据：内容没变继承旧引用，改动条目只接受能在转写里逐字找到的引用
        try:
            segments = await load_task_evidence_context(context.task_id)
            revised = merge_evidence(working, revised, segments)
        except Exception:
            logger.exception("自检修订的证据合并失败，保留修订稿原样", extra={"run_id": run.id})
        # payload 存改完的整份纪要
        await close_step(step, started, f"已按 {len(issues)} 条问题重写", revised)
        return revised
    except Exception as error:
        await fail_step(step, started, error)
        raise


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
    # 已被中断/失败的运行不再收尾，避免把 INTERRUPTED 覆盖成终态
    if run.status != "RUNNING":
        logger.info("运行已不在 RUNNING，跳过汇总", extra={"run_id": run_id, "status": run.status})
        return
    step = await open_step(run, run.current_round, "FINAL", "汇总自检结论")
    started = perf_counter()
    final = final_summary(run)
    await close_step(step, started, final)
    if run.revised_json is None:
        # 第一次审查就通过，内容一个字没动，运行到此结束
        updated = await AgentRun.filter(id=run.id, status="RUNNING").update(
            status="SUCCEEDED", stage="自检通过", progress=100, final_result=final, finished_at=now()
        )
        if not updated:
            logger.info("运行状态已变化，跳过收尾", extra={"run_id": run.id})
        return
    # 产出不会自己写回业务表，必须由人点确认，这是本项目 Agent 唯一的写操作确认点
    updated = await AgentRun.filter(id=run.id, status="RUNNING").update(
        status="WAITING_CONFIRMATION", stage="等待人工确认", progress=100,
        final_result=final, finished_at=now(),
    )
    if not updated:
        logger.info("运行状态已变化，跳过等待确认", extra={"run_id": run.id})


async def execute_agent_run(run_id: int):
    """反思环主流程。

    一轮等于「审查一次 + 按问题改一版」。流程画在 agent_graph 的状态图里，
    这里负责备好材料，再把审查、重写、汇总、中断检查四个动作交给状态图调度。
    """
    try:
        # 领取式抢占：重复调度或多进程时，只有一个执行者能开始跑这次运行
        claimed = await AgentRun.filter(id=run_id, status="RUNNING").exclude(stage="已领取").update(stage="已领取")
        if not claimed:
            return
        run = await AgentRun.get_or_none(id=run_id)
        # 状态不是 RUNNING 说明已经被中断或者已经跑完，直接退出，不重复执行
        if run is None or run.status != "RUNNING":
            return
        # 服务上次被强行关闭时，正在执行的那一步没来得及写结果，续跑前先把它记成失败
        await AgentStep.filter(run_id=run.id, status="RUNNING").update(
            status="FAILED", error_message="上次执行被打断，本步未完成", finish_time=now()
        )
        # 先判断“中断发生在最后一轮重写步”这种情况：此时 current_round 已经等于
        # max_rounds，但这一版修订稿没落库，不能直接收尾，必须先补完重写。
        resume_issues: list[dict] = []
        last_step = await AgentStep.filter(run_id=run.id).order_by("-step_no").first()
        if (
            run.revised_json is None
            and run.current_round >= 1
            and run.review_json
            and last_step is not None
            and last_step.step_type == "REFINE"
            and last_step.status == "FAILED"
        ):
            resume_issues = list(run.review_json)
            logger.info("恢复时从重写步续跑", extra={"run_id": run.id, "round": run.current_round})
        # 已到轮数上限且没有“未完成的重写”要续：不再多审一轮，直接收尾
        if run.current_round >= run.max_rounds and not resume_issues:
            logger.info("已达轮数上限，直接汇总", extra={"run_id": run.id})
            await finalize_run(run.id)
            return
        logger.info("自检运行开始", extra={"run_id": run.id, "round": run.current_round})
        # 发起时记下的自检模型配置，整次运行都用这一条
        config = await AiModelConfig.get(id=run.model_config_id)
        meeting = await Meeting.get(id=run.meeting_id)
        minutes = await MeetingMinutes.get(id=run.minutes_id)
        context = await build_minutes_context(run, minutes, meeting)

        async def do_review(working: dict, round_no: int) -> dict:
            """审查一次，并把这一轮的判定落到运行记录上。"""
            result = await review_round(run, config, context, working, round_no)
            issues = result.get("issues") or []
            await AgentRun.filter(id=run.id).update(
                # 轮次加一，页面上的「2 / 2」前半段跟着变
                current_round=round_no,
                # 模型说通过，或者虽然没说通过但一条问题都没挑出来，都记为通过
                passed=bool(result.get("passed")) or not issues,
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
                "issues": resume_issues,
                "resume_issues": resume_issues,
                "max_rounds": run.max_rounds,
                "review": do_review,
                "refine": do_refine,
                "finalize": do_finalize,
                "should_stop": should_stop,
            },
            {"recursion_limit": recursion_limit(run.max_rounds)},
        )
    except asyncio.CancelledError:
        logger.info("自检运行被中断", extra={"run_id": run_id})
        # 页面点中断时会 cancel 这个任务，运行状态已经在接口里改成 INTERRUPTED。
        # 被打断的那一步停在模型调用中途，把它记成失败，时间线上就不会一直显示 RUNNING
        await AgentStep.filter(run_id=run_id, status="RUNNING").update(
            status="FAILED", error_message="运行已中断，本步未完成", finish_time=now()
        )
        return
    except Exception as error:
        logger.exception("自检运行失败", extra={"run_id": run_id})
        # 模型调用失败、结构校验失败、模板被停用等异常，都把整次运行记为 FAILED。
        # 带 RUNNING 条件，避免把用户先点“中断”产生的 INTERRUPTED 覆盖成 FAILED
        await AgentRun.filter(id=run_id, status="RUNNING").update(
            status="FAILED",
            stage="自检失败",
            error_message=(str(error) or type(error).__name__)[:2000],
            finished_at=now(),
        )


def _reschedule_after_current(run_id: int) -> None:
    """旧协程退出后再检查一次状态：仍是 RUNNING 就补一次调度。

    覆盖“中断后立刻恢复、旧任务尚未处理完 cancel”的窗口，避免库里 RUNNING
    但没有任何执行者的假运行状态。
    """

    async def _later():
        await asyncio.sleep(0.5)
        run = await AgentRun.get_or_none(id=run_id)
        if run is not None and run.status == "RUNNING":
            schedule_agent_run(run_id)

    asyncio.create_task(_later())


def schedule_agent_run(run_id: int):
    # 同一次运行已经在跑：等旧任务退出后再补一次调度，而不是静默丢弃
    running = _running_agents.get(run_id)
    if running and not running.done():
        running.add_done_callback(lambda _: _reschedule_after_current(run_id))
        return
    task = asyncio.create_task(execute_agent_run(run_id))
    _running_agents[run_id] = task
    # 任务结束后把自己从字典里摘掉，避免字典一直变大
    task.add_done_callback(lambda _: _running_agents.pop(run_id, None))


def cancel_agent_run(run_id: int):
    # _running_agents 是 schedule_agent_run 往里塞的任务字典
    task = _running_agents.get(run_id)
    if task and not task.done():
        # cancel 会在任务里抛出 CancelledError，主流程捕获后直接返回，不改状态
        task.cancel()


async def recover_agent_runs() -> int:
    # 上次进程退出时被领取但没跑完的，先退回可领取状态
    await AgentRun.filter(status="RUNNING", stage="已领取").update(stage="等待恢复执行")
    # 上次进程结束时状态还停在 RUNNING 的，就是被强行打断的那些
    ids = list(await AgentRun.filter(status="RUNNING").values_list("id", flat=True))
    for run_id in ids:
        # 走的还是同一个调度函数，主流程照样从 current_round 和 revised_json 接着跑
        schedule_agent_run(run_id)
    if ids:
        logger.info("自检运行已重新入队", extra={"count": len(ids)})
    return len(ids)
