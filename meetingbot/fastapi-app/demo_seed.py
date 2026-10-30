"""SEED_DEMO=1 时补充一套 AI 链路演示数据。

只写数据库、不调用模型，用于本地演示和 README 截图：
- 一份已完成的转写任务与 14 条时间轴片段（说话人标签已匹配到演示账号）；
- 一份 DRAFT 纪要（六块结构化内容齐全）；
- 一次 WAITING_CONFIRMATION 的纪要自检运行（4 个步骤 + 修订稿）；
- 若干 AI 调用日志，让观测页的趋势、模型排行和失败记录都有真实数据可看。

生产部署不要开启 SEED_DEMO。
"""

import logging
from datetime import timedelta

from common.times import now
from models import (
    AgentRun,
    AgentStep,
    AiCallLog,
    AiModelConfig,
    Meeting,
    MeetingMaterial,
    MeetingMinutes,
    TranscriptionTask,
    TranscriptSegment,
    User,
)

logger = logging.getLogger(__name__)

DEMO_RUN_NO = "AGR-DEMO-001"

# 说话人标签 → 演示账号：01-0/02-0 是主持人张晨，01-1/02-1 是运营李悦，
# 02-A 故意不做匹配，演示页面的说话人匹配功能
SPEAKER_MAP = {"01-0": "employee", "01-1": "operator", "02-0": "employee", "02-1": "operator"}

# (说话人标签, 文本, start_ms, end_ms)
SEGMENTS = [
    ("01-0", "各位下午好，今天的产品发布评审会现在开始。今天要出三个结论：V2.0 能不能按期发布、灰度怎么放、对外通稿什么时候定稿。", 0, 9200),
    ("01-0", "先请王敏说一下功能验收的情况。", 9200, 12400),
    ("01-1", "V2.0 计划内的 17 个需求已经验收通过 15 个，剩下两个是报表导出和消息推送的免打扰设置。", 12400, 21800),
    ("01-1", "消息推送已经修完，昨天回归通过；报表导出还有问题，让张涛详细说一下。", 21800, 30200),
    ("02-0", "报表导出的问题在压测里暴露出来了：单个用户导出三个月数据要跑四十多秒，超过网关 30 秒超时，压 500 并发时失败率到 12%。", 30200, 43600),
    ("02-1", "四十多秒确实不能接受，有没有办法在发布前解决？", 43600, 49200),
    ("02-0", "两个方案：一是加缓存，把常用时间范围预生成；二是改成异步导出，先落任务再下载文件。", 49200, 58600),
    ("01-0", "缓存解决不了任意时间范围，我倾向异步导出。", 58600, 63800),
    ("02-1", "异步导出对用户体验有影响，前端要加一个导出任务列表。", 63800, 70200),
    ("01-0", "这个可以接受。决定：报表导出改为异步导出，张涛负责后端，前端任务列表由李悦跟进，9 月 10 日前联调完。", 70200, 82600),
    ("01-1", "灰度节奏建议按 10%、30%、100% 三阶段放量，每阶段观察一天。", 82600, 90200),
    ("01-0", "同意。9 月 1 日先放 10%，核心指标稳定再扩大。对外通稿 9 月 12 日定稿，李悦负责。", 90200, 100400),
    ("02-A", "补充一个风险：如果异步导出上线后任务堆积，需要有限流和失败重试，否则会影响其他接口。", 100400, 110600),
    ("01-0", "这个风险记下来，张涛评估一下队列和限流方案，下次评审同步。散会。", 110600, 119200),
]

MINUTES = {
    "summary": (
        "本次会议评审 V2.0 发布准备情况：功能验收 17 项通过 15 项，报表导出接口在压测中出现超时与失败率问题。"
        "会议决定报表导出改为异步导出，灰度按 10%、30%、100% 三阶段放量，对外通稿 9 月 12 日定稿。"
    ),
    "topics": [
        {"title": "功能验收取舍", "summary": "17 项需求通过 15 项，消息推送已修复，报表导出存在性能问题。"},
        {"title": "报表导出改造", "summary": "压测下导出耗时超过网关超时，决定改为异步导出并增加前端任务列表。"},
        {"title": "灰度与发布节奏", "summary": "按 10%、30%、100% 三阶段放量，每阶段观察一天，9 月 1 日启动。"},
    ],
    "viewpoints": [
        {"speaker": "王敏", "viewpoint": "功能验收整体符合预期，剩余问题集中在报表导出和消息推送。"},
        {"speaker": "张涛", "viewpoint": "导出超时由缓存无法覆盖任意时间范围，建议改成异步导出。"},
        {"speaker": "李悦", "viewpoint": "异步导出需要前端任务列表配套，否则用户不知道导出进度。"},
    ],
    "decisions": [
        {
            "content": "报表导出改为异步导出，前端增加导出任务列表",
            "basis": "并发压测失败率 12%，耗时超过网关 30 秒超时",
            "owner_suggestion": "张涛、李悦",
            "deadline_suggestion": "2026-09-10",
        },
        {
            "content": "V2.0 按 10%、30%、100% 三阶段灰度放量",
            "basis": "控制发布风险，核心指标稳定后再扩大范围",
            "owner_suggestion": "张涛",
            "deadline_suggestion": "2026-09-01",
        },
        {
            "content": "对外通稿 9 月 12 日定稿",
            "basis": "与灰度观察窗口对齐，避免发布节奏与宣传口径不一致",
            "owner_suggestion": "李悦",
            "deadline_suggestion": "2026-09-12",
        },
    ],
    "pending_items": [
        {"content": "评估异步导出的队列与限流方案", "owner_suggestion": "张涛", "deadline_suggestion": "下次评审同步"},
        {"content": "前端导出任务列表联调", "owner_suggestion": "李悦", "deadline_suggestion": "2026-09-10"},
    ],
    "risks": [
        {"content": "异步导出上线后任务堆积，可能影响其他接口", "level": "HIGH", "suggestion": "增加队列容量监控与失败重试、限流保护"},
        {"content": "灰度阶段指标抖动，缺少回滚预案", "level": "MEDIUM", "suggestion": "发布前演练一次回滚流程"},
    ],
}

# 自检第 1 轮问题清单（重写前）与第 2 轮（重写后的复核结果）
ISSUES_ROUND_1 = [
    {"field": "decisions", "index": 0, "issue_type": "MISSING_OWNER",
     "detail": "报表导出决策没有写负责人", "suggestion": "补上张涛、李悦"},
    {"field": "pending_items", "index": 1, "issue_type": "MISSING_DEADLINE",
     "detail": "前端任务列表联调没有完成时间", "suggestion": "补上 2026-09-10"},
    {"field": "risks", "index": 0, "issue_type": "NOT_ACTIONABLE",
     "detail": "任务堆积风险没有可执行的应对动作", "suggestion": "补充队列监控、失败重试与限流保护"},
]
ISSUES_ROUND_2 = [
    {"field": "risks", "index": 1, "issue_type": "VAGUE",
     "detail": "回滚预案只写了演练，没有说明回滚判定条件", "suggestion": "补充失败率与耗时阈值"},
]

REVISED = {
    "summary": MINUTES["summary"],
    "topics": MINUTES["topics"],
    "viewpoints": MINUTES["viewpoints"],
    "decisions": [
        {
            "content": "报表导出改为异步导出，前端增加导出任务列表",
            "basis": "并发压测失败率 12%，耗时超过网关 30 秒超时",
            "owner_suggestion": "张涛（后端）、李悦（前端）",
            "deadline_suggestion": "2026-09-10",
        },
        MINUTES["decisions"][1],
        MINUTES["decisions"][2],
    ],
    "pending_items": [
        {"content": "评估异步导出的队列与限流方案", "owner_suggestion": "张涛", "deadline_suggestion": "2026-09-05"},
        {"content": "前端导出任务列表联调", "owner_suggestion": "李悦", "deadline_suggestion": "2026-09-10"},
    ],
    "risks": [
        {"content": "异步导出上线后任务堆积，可能影响其他接口", "level": "HIGH",
         "suggestion": "为导出队列配置容量监控与限流，失败任务自动重试并告警"},
        {"content": "灰度阶段指标抖动，缺少回滚预案", "level": "MEDIUM",
         "suggestion": "发布前演练回滚流程：失败率超过 1% 或 P95 耗时超过 3 秒即回滚"},
    ],
}


async def _create_call_logs(task: TranscriptionTask, minutes: MeetingMinutes, run: AgentRun) -> None:
    """观测页演示日志：三种链路、成功与失败、跨多天分布。"""
    specs = [
        # call_type, biz_id, model_type, status, elapsed_ms, request_size, response_size, usage, days_ago
        ("TRANSCRIPTION", task.id, "TRANSCRIPTION", "SUCCEEDED", 42100, 2_060_000, 38_400,
         {"duration": 2_712_000}, 6),
        ("MINUTES_CHUNK", minutes.id, "MINUTES", "SUCCEEDED", 8_900, 12_000, 2_400,
         {"input_tokens": 4_200, "output_tokens": 860, "total_tokens": 5_060}, 6),
        ("MINUTES_CHUNK", minutes.id, "MINUTES", "SUCCEEDED", 9_600, 11_800, 2_300,
         {"input_tokens": 4_050, "output_tokens": 820, "total_tokens": 4_870}, 6),
        ("MINUTES_CHUNK", minutes.id, "MINUTES", "FAILED", 30_000, 9_400, 0,
         None, 6),
        ("MINUTES_CHUNK", minutes.id, "MINUTES", "SUCCEEDED", 8_100, 8_900, 1_900,
         {"input_tokens": 3_100, "output_tokens": 700, "total_tokens": 3_800}, 6),
        ("MINUTES_MERGE", minutes.id, "MINUTES", "SUCCEEDED", 10_800, 7_600, 2_800,
         {"input_tokens": 2_600, "output_tokens": 950, "total_tokens": 3_550}, 6),
        ("SPEAKER_MATCH", task.id, "SPEAKER", "SUCCEEDED", 6_400, 5_200, 640,
         {"input_tokens": 1_800, "output_tokens": 210, "total_tokens": 2_010}, 5),
        ("AGENT_REVIEW", run.id, "AGENT", "SUCCEEDED", 7_300, 13_600, 1_500,
         {"input_tokens": 4_800, "output_tokens": 520, "total_tokens": 5_320}, 5),
        ("AGENT_REFINE", run.id, "AGENT", "SUCCEEDED", 9_900, 14_200, 2_600,
         {"input_tokens": 5_100, "output_tokens": 900, "total_tokens": 6_000}, 5),
        ("AGENT_REVIEW", run.id, "AGENT", "SUCCEEDED", 7_800, 13_900, 1_400,
         {"input_tokens": 4_900, "output_tokens": 480, "total_tokens": 5_380}, 4),
    ]
    configs = {config.model_type: config async for config in AiModelConfig.all()}
    for index, (call_type, biz_id, model_type, status, elapsed, req, resp, usage, days_ago) in enumerate(specs, 1):
        config = configs.get(model_type)
        if config is None:
            continue
        log = await AiCallLog.create(
            request_id=f"AICDEMO{index:04d}",
            call_type=call_type,
            biz_id=biz_id,
            model_config_id=config.id,
            model_name=config.model_name,
            request_size=req,
            response_size=resp,
            status=status,
            elapsed_ms=elapsed,
            usage_json=usage,
            error_message="模型返回内容为空，已触发一次重试" if status == "FAILED" else None,
        )
        # create_time 是 auto_now_add，建完之后按天挪开，让观测页趋势图有分布
        await AiCallLog.filter(id=log.id).update(create_time=now() - timedelta(days=days_ago))


async def seed_ai_demo(meeting: Meeting, host: User, operator: User) -> None:
    """幂等补充转写 / 纪要 / 自检 / 调用日志演示数据（按固定业务编号判断）。"""
    material = await MeetingMaterial.filter(meeting_id=meeting.id, file_type="AUDIO").order_by("id").first()
    if material is not None and await TranscriptionTask.filter(material_id=material.id).exists():
        logger.info("AI 演示数据已存在，跳过")
        return
    asr_config = await AiModelConfig.filter(model_type="TRANSCRIPTION").order_by("id").first()
    minutes_config = await AiModelConfig.filter(model_type="MINUTES").order_by("id").first()
    agent_config = await AiModelConfig.filter(model_type="AGENT").order_by("id").first()
    if not all((material, asr_config, minutes_config, agent_config)):
        logger.warning("缺少演示素材或模型配置，跳过 AI 演示数据")
        return

    started = now() - timedelta(days=6, hours=2)
    finished = started + timedelta(minutes=46)
    task = await TranscriptionTask.create(
        meeting_id=meeting.id,
        material_id=material.id,
        model_config_id=asr_config.id,
        initiator_id=host.id,
        language="zh",
        status="SUCCEEDED",
        stage="转写完成",
        progress=100,
        total_duration_ms=SEGMENTS[-1][3],
        full_text="\n".join(text for _, text, _, _ in SEGMENTS),
        started_at=started,
        finished_at=finished,
    )

    users = {"employee": host, "operator": operator}
    for index, (label, text, start_ms, end_ms) in enumerate(SEGMENTS, 1):
        speaker = users.get(SPEAKER_MAP.get(label, ""))
        await TranscriptSegment.create(
            task_id=task.id,
            meeting_id=meeting.id,
            segment_no=index,
            start_ms=start_ms,
            end_ms=end_ms,
            speaker_label=label,
            speaker_user_id=speaker.id if speaker else None,
            original_text=text,
            content=text,
        )

    minutes = await MeetingMinutes.create(
        meeting_id=meeting.id,
        source_task_id=task.id,
        model_config_id=minutes_config.id,
        status="DRAFT",
        stage="纪要已生成",
        progress=100,
        summary=MINUTES["summary"],
        topics_json=MINUTES["topics"],
        viewpoints_json=MINUTES["viewpoints"],
        decisions_json=MINUTES["decisions"],
        pending_items_json=MINUTES["pending_items"],
        risks_json=MINUTES["risks"],
        created_by=host.id,
    )

    run_started = finished + timedelta(minutes=3)
    run = await AgentRun.create(
        run_no=DEMO_RUN_NO,
        minutes_id=minutes.id,
        meeting_id=meeting.id,
        user_id=host.id,
        model_config_id=agent_config.id,
        status="WAITING_CONFIRMATION",
        stage="等待人工确认",
        progress=100,
        current_round=2,
        max_rounds=2,
        passed=False,
        score=86,
        issue_count=len(ISSUES_ROUND_2),
        review_json=ISSUES_ROUND_2,
        revised_json=REVISED,
        final_result="已按审查发现的 3 条问题重写纪要，得分 86，达到 2 轮上限后没有再复审，请人工判断是否采用修订稿。",
        started_at=run_started,
        finished_at=run_started + timedelta(minutes=2),
    )
    steps = [
        # step_no, round_no, step_type, decision, observation, payload, elapsed_ms
        (1, 1, "REVIEW", "第1轮：审查是否可落实",
         "可落实性不足：报表导出决策没有明确负责人；前端任务列表联调没有完成时间；任务堆积风险没有可执行的应对动作。",
         {"passed": False, "score": 62, "issues": ISSUES_ROUND_1}, 7_300),
        (2, 1, "REFINE", "第1轮：按 3 条问题重写",
         "已按 3 条问题重写，补齐负责人、完成时间和风险应对动作。",
         REVISED, 9_900),
        (3, 2, "REVIEW", "第2轮：审查是否可落实",
         "负责人和完成时间已补齐；风险条目的回滚判定仍然偏含糊，建议补充失败率与耗时阈值。",
         {"passed": False, "score": 86, "issues": ISSUES_ROUND_2}, 7_800),
        (4, 2, "FINAL", "汇总自检结论", run.final_result, None, 60),
    ]
    for step_no, round_no, step_type, decision, observation, payload, elapsed in steps:
        await AgentStep.create(
            run_id=run.id,
            step_no=step_no,
            round_no=round_no,
            step_type=step_type,
            decision_summary=decision,
            observation_summary=observation,
            payload_json=payload,
            status="SUCCEEDED",
            elapsed_ms=elapsed,
            finish_time=run_started + timedelta(seconds=step_no * 30),
        )

    await _create_call_logs(task, minutes, run)
    logger.info("AI 演示数据已就绪", extra={"task_id": task.id, "minutes_id": minutes.id, "run_id": run.id})
