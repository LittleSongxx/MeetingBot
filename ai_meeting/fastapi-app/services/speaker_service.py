from models import AiModelConfig, Meeting, MeetingParticipant, PromptTemplate, TranscriptSegment, User
from common.prompts import render_prompt
from services.model_factory import invoke_json

# 说话人证据采样：按标签分组节选，而不是截取整段转写的前 N 条。
# 旧实现（前 120 条）在长会议上有结构性缺口——后部分段的说话人标签
#（如 05-0）在名单里存在、却从不出现在模型可见的文本里，永远得不到建议。
# 现在每个标签节选头 5 条 + 尾 3 条（点名句多在头，收尾常有点名回指），
# 模型对**全部**标签都有证据可看；总行数封顶防 token 失控。
SAMPLE_HEAD = 5
SAMPLE_TAIL = 3
MAX_TOTAL_LINES = 420

SPEAKER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    # 对应页面表格里的说话人标签，例如 01-0
                    "speaker_label": {"type": "string"},
                    # 从参会人员名单里挑的 user_id，拿不准时模型填 0
                    "user_id": {"type": "integer"},
                    # 把握程度，页面上翻成中文显示在姓名标签旁边
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    # 判断依据，页面上显示在建议下面，供人核对
                    "reason": {"type": "string"},
                },
                "required": ["speaker_label", "user_id", "confidence", "reason"],
            },
        }
    },
    "required": ["suggestions"],
}

CONFIDENCE_TEXT = {"HIGH": "把握较大", "MEDIUM": "有一定把握", "LOW": "把握不大"}


async def build_participants_text(meeting_id: int) -> tuple[str, dict[int, User]]:
    """把参会人员拼成一段名单，模型只能从这份名单里选人。"""
    rows = await MeetingParticipant.filter(meeting_id=meeting_id).order_by("id")
    # 一次查出全部参会人的用户记录，避免在循环里逐条查库
    users = {user.id: user for user in await User.filter(id__in=[row.user_id for row in rows])}
    lines = []
    for row in rows:
        user = users.get(row.user_id)
        # 查不到对应用户记录时跳过这一行
        if user is None:
            continue
        position = user.position or "未填写职位"
        # user_id 写进名单里，模型要按它返回，代码才好把建议对回具体的人
        lines.append(f"user_id={user.id}｜{user.name}｜{position}｜会议角色：{row.participant_role or '参会人'}")
    return "\n".join(lines), users


async def build_transcript_text(task_id: int) -> str:
    """按标签分组节选转写：保证每一个出现的说话人标签都有证据片段进入模型视野。"""
    segments = await TranscriptSegment.filter(task_id=task_id).order_by("segment_no")
    by_label: dict[str, list] = {}
    for segment in segments:
        by_label.setdefault(segment.speaker_label or "未知", []).append(segment)
    lines: list[str] = []
    for label, grouped in by_label.items():
        if len(grouped) <= SAMPLE_HEAD + SAMPLE_TAIL:
            sample = grouped
        else:
            sample = grouped[:SAMPLE_HEAD] + grouped[-SAMPLE_TAIL:]
        lines.append(f"— 说话人 {label}（共 {len(grouped)} 条发言，节选 {len(sample)} 条）—")
        lines.extend(f"[{label}] {seg.content}" for seg in sample)
        if len(lines) >= MAX_TOTAL_LINES:
            lines = lines[:MAX_TOTAL_LINES]
            lines.append("（其余说话人的节选因篇幅截断）")
            break
    return "\n".join(lines)


async def suggest_speakers(task_id: int, meeting_id: int, user_id: int) -> list[dict]:
    """调一次模型，给每个说话人标签推荐一位参会人员。结果只是建议，写不写库由人决定。"""
    # 说话人匹配有自己的模型用途，管理员可以给它单独挑一个模型
    config = await AiModelConfig.filter(model_type="SPEAKER", enabled=True).order_by("-id").first()
    if config is None or not config.api_key:
        raise RuntimeError("请先由管理员启用说话人匹配模型配置")
    # 第 10 章维护的 SPEAKER_MATCH 模板，被停用时直接提示
    prompt = await PromptTemplate.get_or_none(code="SPEAKER_MATCH", enabled=True)
    if prompt is None:
        raise RuntimeError("说话人建议匹配Prompt未启用")
    meeting = await Meeting.get(id=meeting_id)
    # users 是 user_id 到用户记录的字典，下面过滤建议时用它判断推荐的人在不在名单里
    participants_text, users = await build_participants_text(meeting_id)
    # 没有参会人员就没有可选范围，直接拒绝，不浪费一次模型调用
    if not participants_text:
        raise RuntimeError("该会议还没有参会人员，先去会议管理里添加")
    transcript_text = await build_transcript_text(task_id)
    if not transcript_text:
        raise RuntimeError("该转写任务没有可用文本")

    system_prompt = prompt.system_prompt
    # 把 SPEAKER_MATCH 模板里的三个花括号变量替换成真实内容
    user_prompt = render_prompt(
        prompt.user_prompt,
        meeting_title=meeting.title,
        participants=participants_text,
        transcript=transcript_text,
    )
    # 调一次说话人匹配模型，成功失败都写一条 SPEAKER_MATCH 调用日志，biz_id 是转写任务ID
    result = await invoke_json(
        config, system_prompt, user_prompt, SPEAKER_SCHEMA, "SPEAKER_MATCH", task_id, "说话人匹配模型"
    )

    # 模型可能给出名单以外的 user_id，这里按参会人员名单过滤一遍，只留下确实存在的人
    labels = set(
        await TranscriptSegment.filter(task_id=task_id).distinct().values_list("speaker_label", flat=True)
    )
    suggestions = []
    for item in result.get("suggestions") or []:
        label = item.get("speaker_label")
        # 模型拿不准时按提示词要求填 0，缺失时同样按 0 处理
        suggested_id = int(item.get("user_id") or 0)
        # 标签不在这个任务里，或者推荐的人不在参会名单里，这条建议直接丢掉；0 不在 users 里，同样被丢掉
        if label not in labels or suggested_id not in users:
            continue
        suggestions.append({
            # 前端按它把建议放进对应的说话人行
            "speaker_label": label,
            # 点“采纳”时写进这一行下拉框的值
            "user_id": suggested_id,
            # SPEAKER_SCHEMA 里没有姓名字段，姓名按 user_id 从参会名单里取，页面绿色标签显示它
            "user_name": users[suggested_id].name,
            "confidence": item.get("confidence"),
            # HIGH、MEDIUM、LOW 翻译成“把握较大”等中文
            "confidence_text": CONFIDENCE_TEXT.get(item.get("confidence"), item.get("confidence")),
            # 模型给出的判断依据，页面显示在建议下方
            "reason": item.get("reason"),
        })
    return suggestions
