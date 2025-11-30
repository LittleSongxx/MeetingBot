from collections import Counter
from datetime import date, datetime, time, timedelta
from math import ceil

from common.exception_handler import CustomException
from common.times import LOCAL_TZ, format_datetime, now, to_local
from models import (
    AgentRun,
    AgentStep,
    AiCallLog,
    AiModelConfig,
    ChatMessage,
    Meeting,
    MeetingMinutes,
    TranscriptionTask,
    User,
)

from services import pricing


def parse_date_range(date_from: str | None, date_to: str | None) -> tuple[date, date, datetime, datetime]:
    """把页面传来的日期范围转成东八区的起止时刻，不传就默认最近三十天。"""
    today = now().date()
    try:
        # 日期控件的 value-format 是 YYYY-MM-DD，按同样的格式解析
        start_date = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else today - timedelta(days=29)
        end_date = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else today
    except ValueError as error:
        raise CustomException("统计日期格式必须为YYYY-MM-DD") from error
    if start_date > end_date:
        raise CustomException("开始日期不能晚于结束日期")
    # 两端日期都算在内，相差 365 天就是 366 天
    if (end_date - start_date).days > 365:
        raise CustomException("单次统计范围不能超过366天")
    # 结束日期要包含当天，所以右边界取下一天的零点，查询用左闭右开
    start_time = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_time = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    return start_date, end_date, start_time, end_time


def percentile(values: list[int], p: float) -> int:
    """最近秩百分位（与基线报告口径一致；空样本返回 0）。"""
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil(p / 100 * len(ordered)) - 1))
    return ordered[index]


def token_usage(usage: dict | None) -> tuple[int, int, int]:
    """从 ai_call_log.usage_json 里取出输入、输出和总 Token 数。"""
    # 失败的调用没有用量，usage_json 为空，三项都按 0 算
    usage = usage or {}
    # LangChain 的 usage_metadata 用 input_tokens，另一种写法是 prompt_tokens
    input_tokens = int(
        usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    )
    # output_tokens 和 completion_tokens 同理
    output_tokens = int(
        usage.get("output_tokens") or usage.get("completion_tokens") or 0
    )
    # 没有 total_tokens 时用输入加输出补上
    total_tokens = int(
        usage.get("total_tokens") or input_tokens + output_tokens
    )
    return input_tokens, output_tokens, total_tokens


async def employee_ai_log_ids(user_id: int) -> dict[str, set[int]]:
    """查出员工名下的业务记录 ID，日志的 biz_id 落在这些集合里才算他的调用。

    **注意 biz_id 的 ID 空间**：不同 call_type 的 biz_id 指向不同的表，
    因此键也分得开（CHAT 是助手消息 id，CHAT_SESSION 是会话 id——它们数值上可能
    撞车，混进同一个集合就成了张冠李戴的可见性）。
    """
    from models import ChatSession, KbIndexTask

    session_ids = set(await ChatSession.filter(user_id=user_id).values_list("id", flat=True))
    return {
        # 转写任务：转写日志的 biz_id 存的是转写任务ID
        "TRANSCRIPTION": set(await TranscriptionTask.filter(initiator_id=user_id).values_list("id", flat=True)),
        # 会议纪要：分段和合并两类日志的 biz_id 存的都是纪要ID
        "MINUTES": set(await MeetingMinutes.filter(created_by=user_id).values_list("id", flat=True)),
        # 自检运行：审查和重写两类日志的 biz_id 存的都是运行ID
        "AGENT": set(await AgentRun.filter(user_id=user_id).values_list("id", flat=True)),
        # 问答：模型调用与检索嵌入的 biz_id 存的是**助手消息 id**
        "CHAT": set(await ChatMessage.filter(session_id__in=list(session_ids) or [-1])
                    .values_list("id", flat=True)),
        # 会话滚动摘要：biz_id 存的是**会话 id**（与上面是两个不同的 ID 空间）
        "CHAT_SESSION": session_ids,
        # 知识库索引：嵌入批次的 biz_id 存的是索引任务ID
        "KB": set(await KbIndexTask.filter(initiator_id=user_id).values_list("id", flat=True)),
        # 管理员专属调用（在线评测）：员工永远不可见
        "ADMIN": set(),
    }


# 调用类型 -> 它的 biz_id 属于哪类业务记录（对应 employee_ai_log_ids 的三个键）。
# 表驱动：新增调用类型只加一行，不会再出现"类型加了、这里没跟上"的员工观测盲区。
CALL_TYPE_OWNERSHIP: dict[str, str] = {
    "TRANSCRIPTION": "TRANSCRIPTION",
    "MINUTES": "MINUTES",
    "AGENT_REVIEW": "AGENT",
    "AGENT_REFINE": "AGENT",
    "AGENT_REVIEW_SUPPORT": "AGENT",
    # 跨族裁判（JUDGE 未配置时回退 AGENT；调用类型仍如实区分）
    "AGENT_JUDGE": "AGENT",
    # 问答与知识库：新调用类型必须在这里登记，否则员工看不到自己的调用
    #（登记表漏一行的表现是"观测页少一块"，而不是报错，很容易漏）
    "CHAT": "CHAT",
    "CHAT_SUMMARY": "CHAT_SESSION",
    "KB_EMBEDDING": "KB",
    # 索引时逐块生成上下文句：biz_id 是索引任务 id，归知识库
    "KB_CONTEXTUALIZE": "KB",
    # 检索用的查询嵌入：biz_id 是发起它的那条助手消息（接口预览时为 0，归平台级）
    "KB_QUERY_EMBED": "CHAT",
    # 检索结果重排：同一条助手消息发起，因此同样归到这条问答上
    "KB_RERANK": "CHAT",
    # CRAG 式上下文过滤：同一条助手消息发起，归到该问答
    "KB_RELEVANCE_FILTER": "CHAT",
    # turn 级上下文过滤：同一条助手消息发起
    "KB_TURN_FILTER": "CHAT",
    # 查询规划（相对时间/指代改写 + 意图）：同样归到这条问答
    "KB_QUERY_PLAN": "CHAT",
    # 在线评测判定：管理员手动触发，biz_id 是运行ID；员工不可见
    "ONLINE_EVAL_JUDGE": "ADMIN",
}


def employee_can_see_log(log: AiCallLog, owned: dict[str, set[int]]) -> bool:
    """按调用类型选对应的 ID 集合，判断这条日志是不是员工本人业务产生的。"""
    owner_key = CALL_TYPE_OWNERSHIP.get(log.call_type)
    if owner_key is None:
        # 其余调用类型（第 9 章的 SPEAKER_MATCH）不在员工的观测范围内
        return False
    return log.biz_id in owned.get(owner_key, set())


async def build_observability(current_user: User, date_from: str | None, date_to: str | None, model_config_id: int | None) -> dict:
    """运行观测页一次查询的全部数据：八张卡片、四张图、模型排行和最近失败记录。"""
    start_date, end_date, start_time, end_time = parse_date_range(date_from, date_to)
    # 模型下拉框传来的配置主键必须存在
    if model_config_id is not None and not await AiModelConfig.filter(id=model_config_id).exists():
        raise CustomException("筛选模型配置不存在")

    # AI 调用日志按 create_time 落在日期范围内筛选
    log_query = AiCallLog.filter(create_time__gte=start_time, create_time__lt=end_time)
    if model_config_id is not None:
        log_query = log_query.filter(model_config_id=model_config_id)
    # 单价表：判定"未定价"以及给页面显示单位用。一次查出，不逐行查库
    config_ids = {log.model_config_id for log in await log_query.order_by("create_time", "id")}
    price_map = {
        row["id"]: (int(row["input_price_micro_per_1k"] or 0),
                    int(row["output_price_micro_per_1k"] or 0))
        for row in await AiModelConfig.filter(id__in=list(config_ids) or [-1]).values(
            "id", "input_price_micro_per_1k", "output_price_micro_per_1k")
    }
    # 按时间正序取出，后面取最近失败记录时再倒过来
    logs = await log_query.order_by("create_time", "id")
    if current_user.role != "ADMIN":
        # 员工只保留本人业务产生的日志，之后的全部统计都基于过滤后的列表
        owned = await employee_ai_log_ids(current_user.id)
        logs = [log for log in logs if employee_can_see_log(log, owned)]

    # 自检运行按 started_at 落在日期范围内筛选
    run_query = AgentRun.filter(started_at__gte=start_time, started_at__lt=end_time)
    # 员工只统计自己发起的运行
    if current_user.role != "ADMIN":
        run_query = run_query.filter(user_id=current_user.id)
    if model_config_id is not None:
        run_query = run_query.filter(model_config_id=model_config_id)
    runs = await run_query.order_by("started_at", "id")
    run_ids = [run.id for run in runs]
    # 步骤只取上面筛出的运行下面的，不再单独按日期筛
    steps = await AgentStep.filter(run_id__in=run_ids).order_by("create_time", "id") if run_ids else []

    total_input_tokens = 0
    total_output_tokens = 0
    total_tokens = 0
    total_cost_micro = 0
    unpriced_count = 0
    # 先给日期范围内每一天放一个零值，没有调用的日期在趋势图上也显示
    daily = {
        (start_date + timedelta(days=offset)).isoformat(): {
            "call_count": 0, "failed_count": 0, "total_tokens": 0, "cost_micro": 0,
            # 未定价的调用单独计数：成本为 0 与"根本不知道多少钱"是两件事，
            # 页面要能把"这个数只是有价调用之和"如实标出来
            "unpriced_count": 0,
        }
        for offset in range((end_date - start_date).days + 1)
    }
    # 两个分组字典：键分别是 call_type 和 model_config_id
    call_type_map = {}
    model_map = {}
    for log in logs:
        input_tokens, output_tokens, current_total = token_usage(log.usage_json)
        total_input_tokens += input_tokens
        total_output_tokens += output_tokens
        total_tokens += current_total
        cost_micro = int(log.cost_micro or 0)
        total_cost_micro += cost_micro
        # "未定价"按模型配置判定：单价两列都为 0 说明这套配置没填价
        priced = any(price_map.get(log.model_config_id, (0, 0)))
        if not priced:
            unpriced_count += 1
        # 日期按东八区自然日归档，数据库里存的是 UTC，直接取 date() 会把凌晨的调用算到前一天
        day = daily.get(to_local(log.create_time).date().isoformat())
        if day is not None:
            day["call_count"] += 1
            day["total_tokens"] += current_total
            day["cost_micro"] += cost_micro
            if not priced:
                day["unpriced_count"] += 1
            if log.status == "FAILED":
                day["failed_count"] += 1

        # 按调用类型累计次数、耗时、Token 和失败数，调用类型分布图用
        call_type = call_type_map.setdefault(log.call_type, {"call_type": log.call_type, "call_count": 0, "failed_count": 0, "elapsed_ms": 0, "total_tokens": 0, "cost_micro": 0, "elapsed_samples": [], "ttft_samples": []})
        call_type["call_count"] += 1
        call_type["elapsed_ms"] += log.elapsed_ms
        call_type["elapsed_samples"].append(int(log.elapsed_ms or 0))
        if int(log.ttft_ms or 0) > 0:
            call_type["ttft_samples"].append(int(log.ttft_ms))
        call_type["total_tokens"] += current_total
        call_type["cost_micro"] += cost_micro
        if log.status == "FAILED":
            call_type["failed_count"] += 1

        # 按模型配置主键累计，两个配置用同一个模型名也分开统计，模型排行表用
        model = model_map.setdefault(log.model_config_id, {"model_config_id": log.model_config_id, "model_name": log.model_name, "call_count": 0, "failed_count": 0, "elapsed_ms": 0, "total_tokens": 0, "cost_micro": 0})
        model["call_count"] += 1
        model["elapsed_ms"] += log.elapsed_ms
        model["total_tokens"] += current_total
        model["cost_micro"] += cost_micro
        if log.status == "FAILED":
            model["failed_count"] += 1

    call_types = []
    for item in call_type_map.values():
        # 成功率保留两位小数
        item["success_rate"] = round((item["call_count"] - item["failed_count"]) * 100 / item["call_count"], 2)
        # 累计耗时换成平均耗时；样本同时算 p50/p95（最近秩，空样本为 0）
        item["average_elapsed_ms"] = round(item.pop("elapsed_ms") / item["call_count"], 2)
        samples = item.pop("elapsed_samples")
        ttft_samples = item.pop("ttft_samples")
        item["p50_elapsed_ms"] = percentile(samples, 50)
        item["p95_elapsed_ms"] = percentile(samples, 95)
        item["p50_ttft_ms"] = percentile(ttft_samples, 50)
        item["p95_ttft_ms"] = percentile(ttft_samples, 95)
        call_types.append(item)
    # 调用次数多的排前面，次数相同按类型名排
    call_types.sort(key=lambda item: (-item["call_count"], item["call_type"]))

    model_ranking = []
    for item in model_map.values():
        item["success_rate"] = round((item["call_count"] - item["failed_count"]) * 100 / item["call_count"], 2)
        item["average_elapsed_ms"] = round(item.pop("elapsed_ms") / item["call_count"], 2)
        model_ranking.append(item)
    # 调用次数多的排前面，次数相同按模型名排
    model_ranking.sort(key=lambda item: (-item["call_count"], item["model_name"]))

    # 反思环没有工具，能按类型统计的是审查、重写、汇总这三种步骤
    step_map = {}
    for step in steps:
        item = step_map.setdefault(step.step_type, {"step_type": step.step_type, "call_count": 0, "succeeded_count": 0, "failed_count": 0, "elapsed_total_ms": 0, "elapsed_samples": 0, "elapsed_values": []})
        item["call_count"] += 1
        # RUNNING 的步骤只计入总数，不计成功也不计失败
        if step.status == "SUCCEEDED":
            item["succeeded_count"] += 1
        elif step.status == "FAILED":
            item["failed_count"] += 1
        # 耗时为 0 的步骤不参与平均耗时
        if step.elapsed_ms:
            item["elapsed_total_ms"] += step.elapsed_ms
            item["elapsed_samples"] += 1
            item["elapsed_values"].append(int(step.elapsed_ms))
    step_ranking = []
    for item in step_map.values():
        item["success_rate"] = round(item["succeeded_count"] * 100 / item["call_count"], 2)
        # 累计值和样本数只用来算平均，算完就从返回结构里去掉
        elapsed_total_ms = item.pop("elapsed_total_ms")
        elapsed_samples = item.pop("elapsed_samples")
        values = item.pop("elapsed_values")
        item["average_elapsed_ms"] = round(elapsed_total_ms / elapsed_samples, 2) if elapsed_samples else 0
        item["p50_elapsed_ms"] = percentile(values, 50)
        item["p95_elapsed_ms"] = percentile(values, 95)
        step_ranking.append(item)
    # 步骤多的排前面，条数相同时按类型名排
    step_ranking.sort(key=lambda item: (-item["call_count"], item["step_type"]))

    all_elapsed = [int(log.elapsed_ms or 0) for log in logs]
    all_ttft = [int(log.ttft_ms or 0) for log in logs if int(log.ttft_ms or 0) > 0]
    failed_count = sum(log.status == "FAILED" for log in logs)
    succeeded_runs = sum(run.status == "SUCCEEDED" for run in runs)
    failed_steps = sum(step.status == "FAILED" for step in steps)
    # 产生了修订稿的运行才需要人工确认，被放弃的就是人工没采用的那些
    revision_runs = [run for run in runs if run.revised_json is not None]
    rejected_runs = sum(run.status == "REJECTED" for run in runs)
    # 平均轮次只统计至少完成过一轮审查的运行
    rounds = [run.current_round for run in runs if run.current_round]
    # 只有写了 finished_at 的运行才能算耗时
    finished_run_durations = [max(0, int((run.finished_at - run.started_at).total_seconds() * 1000)) for run in runs if run.finished_at]

    # 失败的 AI 调用，从最新的往前取十条
    recent_failures = [
        {"source": "AI_CALL", "type": log.call_type, "model_name": log.model_name, "error_message": log.error_message, "create_time": format_datetime(log.create_time)}
        for log in reversed(logs) if log.status == "FAILED"
    ][:10]
    # 失败的自检运行，同样取最新十条
    recent_failures += [
        {"source": "AGENT_RUN", "type": run.run_no, "model_name": None, "error_message": run.error_message, "create_time": format_datetime(run.started_at)}
        for run in reversed(runs) if run.status == "FAILED"
    ][:10]
    # 两类合在一起按时间倒序，最终保留十条
    recent_failures = sorted(recent_failures, key=lambda item: item["create_time"] or "", reverse=True)[:10]
    agent_status = Counter(run.status for run in runs)

    return {
        # 页面顶部标签显示的观测范围
        "scope": "企业全局观测" if current_user.role == "ADMIN" else "个人AI与Agent观测",
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        # 八张卡片的数据，没有样本时比率和平均值返回 0
        "summary": {
            "call_count": len(logs),
            "failed_call_count": failed_count,
            "call_success_rate": round((len(logs) - failed_count) * 100 / len(logs), 2) if logs else 0,
            "average_elapsed_ms": round(sum(all_elapsed) / len(all_elapsed), 2) if all_elapsed else 0,
            "p50_elapsed_ms": percentile(all_elapsed, 50),
            "p95_elapsed_ms": percentile(all_elapsed, 95),
            "average_ttft_ms": round(sum(all_ttft) / len(all_ttft), 2) if all_ttft else 0,
            "p50_ttft_ms": percentile(all_ttft, 50),
            "p95_ttft_ms": percentile(all_ttft, 95),
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            # 成本单位是微元（1e-6 元）；未定价调用数一并给出，避免把 0 读成免费
            "cost_micro": total_cost_micro,
            "cost_text": pricing.format_micro(total_cost_micro),
            "unpriced_call_count": unpriced_count,
            "unpriced_ratio": round(unpriced_count * 100 / len(logs), 2) if logs else 0,
            "agent_run_count": len(runs),
            "agent_success_rate": round(succeeded_runs * 100 / len(runs), 2) if runs else 0,
            "average_agent_elapsed_ms": round(sum(finished_run_durations) / len(finished_run_durations), 2) if finished_run_durations else 0,
            "step_count": len(steps),
            "step_failure_rate": round(failed_steps * 100 / len(steps), 2) if steps else 0,
            "average_rounds": round(sum(rounds) / len(rounds), 2) if rounds else 0,
            "revision_count": len(revision_runs),
            "rejection_count": rejected_runs,
        },
        # 趋势图的横轴数据
        "daily_trend": [{"date": day, **value} for day, value in daily.items()],
        "call_types": call_types,
        "model_ranking": model_ranking,
        # 六种状态固定按这个顺序返回，没有记录的状态数量为 0
        "agent_status": [{"status": status, "count": agent_status[status]} for status in ["RUNNING", "WAITING_CONFIRMATION", "SUCCEEDED", "REJECTED", "FAILED", "INTERRUPTED"]],
        "step_ranking": step_ranking,
        "recent_failures": recent_failures,
    }


def _cache_tokens(usage: dict | None) -> tuple[int, int]:
    """从 usage_json 里取（输入 token, 命中缓存 token）。

    供应商字段不统一：LangChain 归一后常见 input_token_details.cache_read，
    OpenAI 兼容层可能给 prompt_tokens_details.cached_tokens 或
    cache_read_input_tokens。没有上报时返回 0，页面显示"未上报"，不估算。
    """
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    cached = 0
    details = usage.get("input_token_details") or {}
    if isinstance(details, dict):
        cached = int(details.get("cache_read") or details.get("cached_tokens") or 0)
    prompt_details = usage.get("prompt_tokens_details") or {}
    if isinstance(prompt_details, dict):
        cached = max(cached, int(prompt_details.get("cached_tokens") or 0))
    cached = max(cached, int(usage.get("cache_read_input_tokens") or 0))
    return input_tokens, max(0, min(cached, input_tokens))


async def build_cost_report(date_from: str | None, date_to: str | None) -> dict:
    """成本与缓存看板（管理员）：按会议、按问答轮、命中缓存分别归因。

    口径：成本来自 `ai_call_log.cost_micro`（未定价配置按 0 并单独计数）；
    单场纪要成本 = MINUTES 调用 + 该纪要关联的 Agent 自检调用，按 minutes_id
    映射到 meeting_id；问答轮成本 = CHAT 调用按助手消息 id 聚合。
    """
    _, _, start_time, end_time = parse_date_range(date_from, date_to)
    logs = await AiCallLog.filter(
        create_time__gte=start_time, create_time__lt=end_time).order_by("create_time", "id")

    minutes_ids: set[int] = set()
    run_ids: set[int] = set()
    for log in logs:
        call = log.call_type or ""
        if call.startswith("MINUTES"):
            minutes_ids.add(int(log.biz_id))
        elif call.startswith("AGENT_"):
            run_ids.add(int(log.biz_id))
    run_to_minutes = {
        int(run_id): int(minutes_id)
        for run_id, minutes_id in await AgentRun.filter(id__in=list(run_ids) or [-1])
        .values_list("id", "minutes_id")
    }
    minutes_to_meeting = {
        int(minutes_id): int(meeting_id)
        for minutes_id, meeting_id in await MeetingMinutes.filter(
            id__in=list(minutes_ids | set(run_to_minutes.values())) or [-1])
        .values_list("id", "meeting_id")
    }
    meeting_titles = {
        int(mid): title for mid, title in await Meeting.filter(
            id__in=list(set(minutes_to_meeting.values())) or [-1]).values_list("id", "title")
    }
    by_meeting: dict[int, dict] = {}
    for log in logs:
        call = log.call_type or ""
        # 只在对应 ID 空间里映射：CHAT 的 biz_id 是助手消息 id，数值上可能与
        # run_id 撞车；混用会把问答成本算到会议头上（同 employee_ai_log_ids 的教训）。
        if call.startswith("MINUTES"):
            minutes_id = int(log.biz_id)
        elif call.startswith("AGENT_"):
            minutes_id = run_to_minutes.get(int(log.biz_id))
        else:
            continue
        meeting_id = minutes_to_meeting.get(int(minutes_id)) if minutes_id is not None else None
        if meeting_id is None:
            continue
        bucket = by_meeting.setdefault(int(meeting_id), {
            "meeting_id": int(meeting_id),
            "meeting_title": meeting_titles.get(int(meeting_id)) or f"会议 {meeting_id}",
            "cost_micro": 0, "call_count": 0,
        })
        bucket["cost_micro"] += int(log.cost_micro or 0)
        bucket["call_count"] += 1

    chat_cost = 0
    chat_calls = 0
    chat_turns: set[int] = set()
    cache_hit = 0
    cache_input = 0
    priced_config_ids = {
        row["id"]: (int(row["input_price_micro_per_1k"] or 0),
                    int(row["output_price_micro_per_1k"] or 0))
        for row in await AiModelConfig.filter(
            id__in=list({int(log.model_config_id) for log in logs}) or [-1]).values(
            "id", "input_price_micro_per_1k", "output_price_micro_per_1k")
    }
    unpriced = 0
    for log in logs:
        if not any(priced_config_ids.get(int(log.model_config_id), (0, 0))):
            unpriced += 1
        input_tokens, cached = _cache_tokens(log.usage_json)
        cache_input += input_tokens
        cache_hit += cached
        if (log.call_type or "") == "CHAT":
            chat_cost += int(log.cost_micro or 0)
            chat_calls += 1
            chat_turns.add(int(log.biz_id))

    return {
        "by_meeting": sorted(by_meeting.values(), key=lambda item: item["cost_micro"],
                             reverse=True)[:20],
        "by_turn": {
            "turn_count": len(chat_turns), "call_count": chat_calls,
            "avg_cost_micro": round(chat_cost / len(chat_turns), 2) if chat_turns else None,
            "avg_calls": round(chat_calls / len(chat_turns), 2) if chat_turns else None,
        },
        "cache": {
            "hit_tokens": cache_hit, "input_tokens": cache_input,
            "rate": round(cache_hit / cache_input, 4) if cache_input else None,
            "reported": cache_input > 0,
        },
        "unpriced_count": unpriced,
    }
