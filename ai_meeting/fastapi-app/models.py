from tortoise import fields
from tortoise.models import Model


class Department(Model):
    # 对应 department.id
    id = fields.IntField(pk=True)
    # 对应 department.name，弹窗部门名称输入框
    name = fields.CharField(max_length=100)
    # 对应 department.description
    description = fields.CharField(max_length=500, null=True)
    # 对应 department.status，不传时默认 NORMAL
    status = fields.CharField(max_length=20, default="NORMAL")
    # 对应 department.create_time
    create_time = fields.DatetimeField(auto_now_add=True)
    # 对应 department.update_time
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        # 指定这个模型映射到数据库的 department 表
        table = "department"


class User(Model):
    # 对应 user.id，主键自增
    id = fields.IntField(pk=True)
    # 对应 user.username，unique=True 让 ORM 建表时生成唯一索引
    username = fields.CharField(max_length=100, unique=True)
    # 对应 user.password，只落 bcrypt 哈希；登录与改密经 security 模块校验
    password = fields.CharField(max_length=255)
    # 对应 user.name，注册时用账号填充
    name = fields.CharField(max_length=100)
    # 对应 user.avatar，未上传头像时为空
    avatar = fields.CharField(max_length=500, null=True)
    # 对应 user.role，不传时默认 EMPLOYEE
    role = fields.CharField(max_length=30, default="EMPLOYEE")
    # 对应 user.phone
    phone = fields.CharField(max_length=30, null=True)
    # 对应 user.email
    email = fields.CharField(max_length=100, null=True)
    # 对应 user.department_id，注册时不填，由管理员后续分配
    department_id = fields.IntField(null=True)
    # 对应 user.position
    position = fields.CharField(max_length=100, null=True)
    # 对应 user.status，不传时默认 NORMAL
    status = fields.CharField(max_length=20, default="NORMAL")
    # 对应 user.create_time，插入时由 ORM 自动写入当前时间
    create_time = fields.DatetimeField(auto_now_add=True)
    # 对应 user.update_time，每次保存时由 ORM 自动刷新
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        # 指定这个模型映射到数据库的 user 表
        table = "user"


class Meeting(Model):
    # 对应 meeting.id
    id = fields.IntField(pk=True)
    # 对应 meeting.meeting_no，unique=True 对应唯一索引
    meeting_no = fields.CharField(max_length=50, unique=True)
    # 对应 meeting.title
    title = fields.CharField(max_length=255)
    # 对应 meeting.start_time
    start_time = fields.DatetimeField()
    # 对应 meeting.end_time
    end_time = fields.DatetimeField()
    # 对应 meeting.location
    location = fields.CharField(max_length=255, null=True)
    # 对应 meeting.agenda，长文本
    agenda = fields.TextField(null=True)
    # 对应 meeting.creator_id
    creator_id = fields.IntField()
    # 对应 meeting.host_id
    host_id = fields.IntField()
    # 对应 meeting.status，新建会议默认待开始
    status = fields.CharField(max_length=30, default="SCHEDULED")
    # 对应 meeting.cancel_reason
    cancel_reason = fields.CharField(max_length=500, null=True)
    # 对应 meeting.create_time
    create_time = fields.DatetimeField(auto_now_add=True)
    # 对应 meeting.update_time
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        # 指定这个模型映射到数据库的 meeting 表
        table = "meeting"
        # 对应 scripts/indexes.sql 的二级索引（列表页可见性收窄）
        indexes = [("creator_id",), ("host_id",)]


class MeetingParticipant(Model):
    # 对应 meeting_participant.id
    id = fields.IntField(pk=True)
    # 对应 meeting_participant.meeting_id
    meeting_id = fields.IntField()
    # 对应 meeting_participant.user_id
    user_id = fields.IntField()
    # 对应 meeting_participant.participant_role
    participant_role = fields.CharField(max_length=30, default="PARTICIPANT")
    # 对应 meeting_participant.response_status
    response_status = fields.CharField(max_length=30, default="PENDING")
    # 对应 meeting_participant.create_time
    create_time = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "meeting_participant"
        # 对应数据库的 uk_meeting_participant 唯一索引
        unique_together = (("meeting_id", "user_id"),)
        # 对应 scripts/indexes.sql 的二级索引（列表页过滤列）
        indexes = [("user_id",)]


class MeetingMaterial(Model):
    # 对应 meeting_material.id
    id = fields.IntField(pk=True)
    # 对应 meeting_material.meeting_id
    meeting_id = fields.IntField()
    # 对应 meeting_material.file_name
    file_name = fields.CharField(max_length=255)
    # 对应 meeting_material.storage_name，unique=True 对应唯一索引
    storage_name = fields.CharField(max_length=255, unique=True)
    # 对应 meeting_material.file_type
    file_type = fields.CharField(max_length=30)
    # 对应 meeting_material.content_type
    content_type = fields.CharField(max_length=150, null=True)
    # 对应 meeting_material.file_ext
    file_ext = fields.CharField(max_length=30, null=True)
    # 对应 meeting_material.file_size，用 BigInt 承载 500MB 级别的字节数
    file_size = fields.BigIntField(default=0)
    # 对应 meeting_material.file_hash
    file_hash = fields.CharField(max_length=64)
    # 对应 meeting_material.uploader_id
    uploader_id = fields.IntField()
    # 对应 meeting_material.create_time
    create_time = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "meeting_material"
        # 对应数据库的 uk_meeting_material_hash 唯一索引
        unique_together = (("meeting_id", "file_hash"),)
        # 对应 scripts/indexes.sql 的二级索引（列表页过滤列）
        indexes = [("meeting_id",)]


class AiModelConfig(Model):
    # 对应 ai_model_config.id
    id = fields.IntField(pk=True)
    # 对应 ai_model_config.name
    name = fields.CharField(max_length=100)
    # 对应 ai_model_config.model_type，不传时默认转写用途
    model_type = fields.CharField(max_length=30, default="TRANSCRIPTION")
    # 对应 ai_model_config.provider
    provider = fields.CharField(max_length=50, default="OPENAI_COMPATIBLE")
    # 对应 ai_model_config.base_url
    base_url = fields.CharField(max_length=500)
    # 对应 ai_model_config.api_key
    api_key = fields.CharField(max_length=1000)
    # 对应 ai_model_config.model_name
    model_name = fields.CharField(max_length=100)
    # 对应 ai_model_config.timeout_seconds
    timeout_seconds = fields.IntField(default=600)
    # 对应 ai_model_config.enabled，ORM 里是布尔值，数据库里是 0 或 1
    enabled = fields.BooleanField(default=False)
    # 对应 ai_model_config.create_time
    create_time = fields.DatetimeField(auto_now_add=True)
    # 对应 ai_model_config.update_time
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "ai_model_config"


class TranscriptionTask(Model):
    # 对应 transcription_task.id
    id = fields.IntField(pk=True)
    # 对应 transcription_task.meeting_id
    meeting_id = fields.IntField()
    # 对应 transcription_task.material_id，unique=True 对应唯一索引
    material_id = fields.IntField(unique=True)
    # 对应 transcription_task.model_config_id
    model_config_id = fields.IntField()
    # 对应 transcription_task.initiator_id
    initiator_id = fields.IntField()
    # 对应 transcription_task.language
    language = fields.CharField(max_length=20, default="zh")
    # 对应 transcription_task.status，新任务默认等待执行
    status = fields.CharField(max_length=30, default="PENDING")
    # 对应 transcription_task.stage
    stage = fields.CharField(max_length=100, default="等待执行")
    # 对应 transcription_task.progress
    progress = fields.IntField(default=0)
    # 对应 transcription_task.retry_count
    retry_count = fields.IntField(default=0)
    # 对应 transcription_task.max_retry
    max_retry = fields.IntField(default=3)
    # 对应 transcription_task.total_duration_ms
    total_duration_ms = fields.BigIntField(default=0)
    # 对应 transcription_task.full_text
    full_text = fields.TextField(null=True)
    # 对应 transcription_task.error_message
    error_message = fields.TextField(null=True)
    # 对应 transcription_task.started_at
    started_at = fields.DatetimeField(null=True)
    # 对应 transcription_task.finished_at
    finished_at = fields.DatetimeField(null=True)
    # 对应 transcription_task.create_time
    create_time = fields.DatetimeField(auto_now_add=True)
    # 对应 transcription_task.update_time
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "transcription_task"
        # 对应 scripts/indexes.sql 的二级索引（列表页过滤列）
        indexes = [("initiator_id",), ("meeting_id",)]


class TranscriptSegment(Model):
    # 对应 transcript_segment.id
    id = fields.IntField(pk=True)
    # 对应 transcript_segment.task_id
    task_id = fields.IntField()
    # 对应 transcript_segment.meeting_id
    meeting_id = fields.IntField()
    # 对应 transcript_segment.segment_no
    segment_no = fields.IntField()
    # 对应 transcript_segment.start_ms
    start_ms = fields.BigIntField(default=0)
    # 对应 transcript_segment.end_ms
    end_ms = fields.BigIntField(default=0)
    # 对应 transcript_segment.speaker_label
    speaker_label = fields.CharField(max_length=50, null=True)
    # 对应 transcript_segment.speaker_user_id
    speaker_user_id = fields.IntField(null=True)
    # 对应 transcript_segment.original_text
    original_text = fields.TextField()
    # 对应 transcript_segment.content
    content = fields.TextField()
    # 对应 transcript_segment.revised_by
    revised_by = fields.IntField(null=True)
    # 对应 transcript_segment.revised_time
    revised_time = fields.DatetimeField(null=True)
    # 对应 transcript_segment.create_time
    create_time = fields.DatetimeField(auto_now_add=True)
    # 对应 transcript_segment.update_time
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "transcript_segment"
        # 对应 scripts/indexes.sql 的二级索引（列表页/观测页过滤列）
        indexes = [("meeting_id",)]
        # 对应数据库的 uk_transcript_task_segment 唯一索引
        unique_together = (("task_id", "segment_no"),)


class AiCallLog(Model):
    # 对应 ai_call_log.id
    id = fields.IntField(pk=True)
    # 对应 ai_call_log.request_id，AIC 前缀加随机串
    request_id = fields.CharField(max_length=50, unique=True)
    # 对应 ai_call_log.call_type，例如 MINUTES、AGENT_REVIEW
    call_type = fields.CharField(max_length=50)
    # 对应 ai_call_log.biz_id，按 call_type 指向转写任务、纪要或自检运行
    biz_id = fields.IntField()
    # 对应 ai_call_log.model_config_id
    model_config_id = fields.IntField()
    # 对应 ai_call_log.model_name，调用时从模型配置复制过来
    model_name = fields.CharField(max_length=100)
    # 对应 ai_call_log.request_size 和 response_size
    request_size = fields.BigIntField(default=0)
    response_size = fields.BigIntField(default=0)
    # 对应 ai_call_log.status，SUCCEEDED 或 FAILED
    status = fields.CharField(max_length=30)
    # 对应 ai_call_log.elapsed_ms
    elapsed_ms = fields.BigIntField(default=0)
    # 对应 ai_call_log.usage_json
    usage_json = fields.JSONField(null=True)
    # 对应 ai_call_log.error_message
    error_message = fields.TextField(null=True)
    # 对应 ai_call_log.create_time，写日志时自动填入
    create_time = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "ai_call_log"
        # 对应 scripts/indexes.sql 的二级索引（列表页/观测页过滤列）
        indexes = [("create_time", "model_config_id"), ("biz_id", "call_type")]


class PromptTemplate(Model):
    # 对应 prompt_template.id
    id = fields.IntField(pk=True)
    # 对应 prompt_template.code，unique=True 对应唯一索引 uk_prompt_code
    code = fields.CharField(max_length=100, unique=True)
    # 对应 prompt_template.name
    name = fields.CharField(max_length=100)
    # 对应 prompt_template.scene_type，不传时默认 MEETING_MINUTES
    scene_type = fields.CharField(max_length=50, default="MEETING_MINUTES")
    # 对应 prompt_template.system_prompt，TextField 映射 longtext
    system_prompt = fields.TextField()
    # 对应 prompt_template.user_prompt
    user_prompt = fields.TextField()
    # 对应 prompt_template.enabled，ORM 里是布尔值，数据库里是 0 或 1
    enabled = fields.BooleanField(default=True)
    # 对应 prompt_template.create_time
    create_time = fields.DatetimeField(auto_now_add=True)
    # 对应 prompt_template.update_time，每次保存时自动刷新
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        # 指定这个模型映射到数据库的 prompt_template 表
        table = "prompt_template"


class MeetingMinutes(Model):
    # 对应 meeting_minutes.id
    id = fields.IntField(pk=True)
    # 对应 meeting_minutes.meeting_id
    meeting_id = fields.IntField()
    # 对应 meeting_minutes.source_task_id，唯一约束写在下面的 Meta 里
    source_task_id = fields.IntField()
    # 对应 meeting_minutes.model_config_id
    model_config_id = fields.IntField()
    # 对应 meeting_minutes.status，新建时默认生成中
    status = fields.CharField(max_length=30, default="GENERATING")
    # 对应 meeting_minutes.stage
    stage = fields.CharField(max_length=100, default="等待生成")
    # 对应 meeting_minutes.progress
    progress = fields.IntField(default=0)
    # 对应 meeting_minutes.summary，生成完成前为空
    summary = fields.TextField(null=True)
    # 下面五个 JSONField 对应五个 json 列，default=list 让新建记录时写入空数组
    topics_json = fields.JSONField(default=list)
    viewpoints_json = fields.JSONField(default=list)
    decisions_json = fields.JSONField(default=list)
    pending_items_json = fields.JSONField(default=list)
    risks_json = fields.JSONField(default=list)
    # 对应 meeting_minutes.created_by
    created_by = fields.IntField()
    # 对应 meeting_minutes.confirmed_by 和 confirmed_time，确认前为空
    confirmed_by = fields.IntField(null=True)
    confirmed_time = fields.DatetimeField(null=True)
    # 对应 meeting_minutes.error_message
    error_message = fields.TextField(null=True)
    # 对应 meeting_minutes.create_time 和 update_time
    create_time = fields.DatetimeField(auto_now_add=True)
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "meeting_minutes"
        # 对应 scripts/indexes.sql 的二级索引（列表页/观测页过滤列）
        indexes = [("meeting_id",), ("created_by",)]
        # 一个转写任务只保留一份纪要，重新生成时覆盖原来那条
        unique_together = (("source_task_id",),)


class AgentRun(Model):
    """一次会议纪要自检运行。

    自检采用反思环：一轮等于「审查一次 + 按问题改一版」。模型判定通过就当场结束不再重写，
    判定不通过就改一版，还没到轮数上限就回到审查复核新的这一版。默认跑两轮，
    也就是第一轮改完的稿子一定会被再审一遍。
    转几轮由模型的判定结果决定，代码只负责兜住上限，所以这里记录的是轮次而不是固定步数。
    """

    # 对应 agent_run.id
    id = fields.IntField(pk=True)
    # 对应 agent_run.run_no
    run_no = fields.CharField(max_length=50, unique=True)
    # 对应 agent_run.minutes_id、meeting_id、user_id、model_config_id
    minutes_id = fields.IntField()
    meeting_id = fields.IntField()
    user_id = fields.IntField()
    model_config_id = fields.IntField()
    # 对应 agent_run.status，新建时默认 RUNNING
    status = fields.CharField(max_length=30, default="RUNNING")
    # 当前执行到哪一步的中文描述，和 0 到 100 的进度，页面上的进度条读它们
    stage = fields.CharField(max_length=100, default="等待执行")
    progress = fields.IntField(default=0)
    # 对应 agent_run.current_round 和 max_rounds
    current_round = fields.IntField(default=0)
    max_rounds = fields.IntField(default=2)
    # 对应 agent_run.passed、score、issue_count，每轮审查后覆盖
    passed = fields.BooleanField(default=False)
    score = fields.IntField(default=0)
    issue_count = fields.IntField(default=0)
    # 对应 agent_run.review_json，默认空数组
    review_json = fields.JSONField(default=list)
    # 对应 agent_run.revised_json，没改过稿时为 None
    revised_json = fields.JSONField(null=True)
    # 对应 agent_run.applied_by、applied_time、reject_reason，人工确认或放弃时写入
    applied_by = fields.IntField(null=True)
    applied_time = fields.DatetimeField(null=True)
    reject_reason = fields.CharField(max_length=500, null=True)
    # 对应 agent_run.final_result 和 error_message
    final_result = fields.TextField(null=True)
    error_message = fields.TextField(null=True)
    # 对应 agent_run.started_at，建记录时自动填入
    started_at = fields.DatetimeField(auto_now_add=True)
    # 对应 agent_run.finished_at
    finished_at = fields.DatetimeField(null=True)
    # 对应 agent_run.update_time，每次保存自动刷新
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "agent_run"
        # 对应 scripts/indexes.sql 的二级索引（列表页/观测页过滤列）
        indexes = [("minutes_id",), ("meeting_id",), ("user_id",)]


class AgentStep(Model):
    # 对应 agent_step.id
    id = fields.IntField(pk=True)
    # 对应 agent_step.run_id、step_no、round_no
    run_id = fields.IntField()
    step_no = fields.IntField()
    round_no = fields.IntField(default=0)
    # 对应 agent_step.step_type：REVIEW、REFINE、FINAL
    step_type = fields.CharField(max_length=30)
    # 对应 agent_step.decision_summary 和 observation_summary
    decision_summary = fields.TextField(null=True)
    observation_summary = fields.TextField(null=True)
    # 对应 agent_step.payload_json
    payload_json = fields.JSONField(null=True)
    # 对应 agent_step.status，开步骤时默认 RUNNING
    status = fields.CharField(max_length=30, default="RUNNING")
    # 对应 agent_step.elapsed_ms 和 error_message
    elapsed_ms = fields.BigIntField(default=0)
    error_message = fields.TextField(null=True)
    # 对应 agent_step.create_time，开步骤时自动填入
    create_time = fields.DatetimeField(auto_now_add=True)
    # 对应 agent_step.finish_time
    finish_time = fields.DatetimeField(null=True)

    class Meta:
        table = "agent_step"
        # 对应唯一索引 uk_agent_run_step
        unique_together = (("run_id", "step_no"),)



