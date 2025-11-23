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
    # 对应 meeting.series_id：会议所属系列（周会/项目例会）。为空表示尚未归类。
    # 系列只用于记忆归属与检索范围收窄，不改变会议可见性（可见性仍由 access 判定）。
    series_id = fields.IntField(null=True)
    # 对应 meeting.create_time
    create_time = fields.DatetimeField(auto_now_add=True)
    # 对应 meeting.update_time
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        # 指定这个模型映射到数据库的 meeting 表
        table = "meeting"
        # 对应 scripts/indexes.sql 的二级索引（列表页可见性收窄、系列筛选）
        indexes = [("creator_id",), ("host_id",), ("series_id",)]


class MeetingSeries(Model):
    """会议系列：周会、项目例会等重复性会议的容器。

    一个会议最多属于一个系列（`meeting.series_id`）。系列不授予任何权限：
    系列可见 = 该系列下至少一场会议对用户可见；系列编辑 = 创建人或管理员。
    """

    # 对应 meeting_series.id
    id = fields.IntField(pk=True)
    # 对应 meeting_series.name：系列名称（例如「产品发布周会」）
    name = fields.CharField(max_length=255)
    # 对应 meeting_series.description
    description = fields.TextField(null=True)
    # 对应 meeting_series.cadence：WEEKLY / BIWEEKLY / MONTHLY / IRREGULAR
    cadence = fields.CharField(max_length=30, default="IRREGULAR")
    # 对应 meeting_series.owner_id：系列负责人（默认创建人）
    owner_id = fields.IntField()
    # 对应 meeting_series.status：ACTIVE / ARCHIVED
    status = fields.CharField(max_length=20, default="ACTIVE")
    # 对应 meeting_series.create_time / update_time
    create_time = fields.DatetimeField(auto_now_add=True)
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "meeting_series"
        indexes = [("owner_id",), ("status",)]


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
    # 对应 ai_model_config.input_price_micro_per_1k / output_price_micro_per_1k：
    # 每 1000 token 的单价，单位是"微元"（1e-6 元）。用整数存是为了让成本求和
    # 与比较不引入浮点误差（金额用浮点相加迟早对不上账）；0 表示未配置单价，
    # 此时成本按 0 记并**在页面上标注为未定价**，而不是拿 0 冒充"免费"。
    input_price_micro_per_1k = fields.IntField(default=0)
    output_price_micro_per_1k = fields.IntField(default=0)
    # 对应 ai_model_config.enabled，ORM 里是布尔值，数据库里是 0 或 1
    enabled = fields.BooleanField(default=False)
    # 对应 ai_model_config.create_time
    create_time = fields.DatetimeField(auto_now_add=True)
    # 对应 ai_model_config.update_time
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "ai_model_config"


class ModelRoute(Model):
    """调用类型/用途 → 模型配置的路由。

    路由为空或目标配置不可用时回退到原用途（例如 CHAT → AGENT）。默认不建路由，
    所有流量走原用途；成本实验达标后才建议开启。
    """

    # 对应 model_route.id
    id = fields.IntField(pk=True)
    # 对应 model_route.route_key：MINUTES / AGENT / SPEAKER / CHAT / CHAT_SUMMARY /
    # KB_QUERY_PLAN / KB_CONTEXTUALIZE
    route_key = fields.CharField(max_length=50, unique=True)
    # 对应 model_route.model_config_id
    model_config_id = fields.IntField()
    # 对应 model_route.enabled
    enabled = fields.BooleanField(default=True)
    # 对应 model_route.note
    note = fields.CharField(max_length=500, null=True)
    # 对应 model_route.create_time / update_time
    create_time = fields.DatetimeField(auto_now_add=True)
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "model_route"


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
    # 对应 transcription_task.recorded_at：录音**实际开始时间**（墙钟）。
    # 与 create_time（上传时间）和 meeting.start_time（会议名义时间）都不是一回事：
    # 一场会可能有多段录音（上午一场、下午一场），"下午那场"只能靠它判断。
    # 为空表示未标注；不拿 create_time 冒充（上传可能发生在几天后）。
    recorded_at = fields.DatetimeField(null=True)
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
    # 对应 ai_call_log.cost_micro：这次调用的成本（微元），由调用时所在配置的单价
    # 与本次真实用量算出，写入这一行——成本是"这次调用"的属性，不是事后重算的
    # 派生值（事后重算会把单价变更前的历史调用按新价重估，那是另一种数据）
    cost_micro = fields.BigIntField(default=0)
    # 对应 ai_call_log.trace_id：跨阶段串联用（同一次业务操作里的多次调用共享它），
    # 为空表示该调用未参与链路追踪
    trace_id = fields.CharField(max_length=64, null=True)
    # 对应 ai_call_log.span_id：这一次调用在 trace 里的 span（没有 OTel 导出时
    # 也由应用生成并落库，保证"调用能对上 span"）
    span_id = fields.CharField(max_length=32, null=True)
    # 对应 ai_call_log.error_message
    error_message = fields.TextField(null=True)
    # 对应 ai_call_log.create_time，写日志时自动填入
    create_time = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "ai_call_log"
        # 对应 scripts/indexes.sql 的二级索引（列表页/观测页过滤列）
        indexes = [("create_time", "model_config_id"), ("biz_id", "call_type"),
                   ("trace_id",)]


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


class MeetingMemory(Model):
    """已确认的长期记忆：系列级决策/结论 + 用户级偏好。

    只有 CONFIRMED 状态对外可见；PROPOSED 是待人工确认的候选。
    待办不在本表重复存：待办统一由 `action_item` 承载，工具层合并返回。
    """

    # 对应 meeting_memory.id
    id = fields.IntField(pk=True)
    # 对应 meeting_memory.scope_type：SERIES（系列级）/ USER（用户级）
    scope_type = fields.CharField(max_length=20)
    # 对应 meeting_memory.scope_id：series_id 或 user_id
    scope_id = fields.IntField()
    # 对应 meeting_memory.kind：DECISION / CONCLUSION / PREFERENCE
    kind = fields.CharField(max_length=20)
    # 对应 meeting_memory.content
    content = fields.TextField()
    # 对应 meeting_memory.normalized：归一化文本，用于去重与冲突检测
    normalized = fields.TextField(null=True)
    # 对应 meeting_memory.source_type：MINUTES / MANUAL
    source_type = fields.CharField(max_length=20, default="MANUAL")
    # 对应 meeting_memory.source_id：纪要ID或手工记录ID
    source_id = fields.IntField(null=True)
    # 对应 meeting_memory.source_meeting_id
    source_meeting_id = fields.IntField(null=True)
    # 对应 meeting_memory.evidence_json：证据片段（quote/start_ms/end_ms/speaker）
    evidence_json = fields.JSONField(null=True)
    # 对应 meeting_memory.status：PROPOSED / CONFIRMED / REJECTED / EXPIRED / SUPERSEDED
    status = fields.CharField(max_length=20, default="PROPOSED")
    # 对应 meeting_memory.supersedes_id：本条替代的旧记忆
    supersedes_id = fields.IntField(null=True)
    # 对应 meeting_memory.valid_from / valid_to：生效与失效时间
    valid_from = fields.DatetimeField(null=True)
    valid_to = fields.DatetimeField(null=True)
    # 对应 meeting_memory.created_by / confirmed_by / confirmed_time
    created_by = fields.IntField()
    confirmed_by = fields.IntField(null=True)
    confirmed_time = fields.DatetimeField(null=True)
    # 对应 meeting_memory.create_time / update_time
    create_time = fields.DatetimeField(auto_now_add=True)
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "meeting_memory"
        indexes = [("scope_type", "scope_id", "status"), ("kind",),
                   ("source_type", "source_id"), ("supersedes_id",)]


class ActionItem(Model):
    """会议待办：从已确认纪要的 pending_items 生成，可跨会议结转。"""

    # 对应 action_item.id
    id = fields.IntField(pk=True)
    # 对应 action_item.series_id / meeting_id / minutes_id
    series_id = fields.IntField(null=True)
    meeting_id = fields.IntField()
    minutes_id = fields.IntField(null=True)
    # 对应 action_item.source_field / source_index：来源纪要字段与条目序号
    source_field = fields.CharField(max_length=30, null=True)
    source_index = fields.IntField(null=True)
    # 对应 action_item.content
    content = fields.TextField()
    # 对应 action_item.owner_user_id / owner_name：匹配到员工时落用户ID，否则保留文本
    owner_user_id = fields.IntField(null=True)
    owner_name = fields.CharField(max_length=100, null=True)
    # 对应 action_item.deadline / deadline_text：解析出的时间与原文期限
    deadline = fields.DatetimeField(null=True)
    deadline_text = fields.CharField(max_length=200, null=True)
    # 对应 action_item.status：OPEN / DONE / BLOCKED / CANCELLED
    status = fields.CharField(max_length=20, default="OPEN")
    # 对应 action_item.evidence_json
    evidence_json = fields.JSONField(null=True)
    # 对应 action_item.dedupe_key：同一系列内去重用的稳定键
    dedupe_key = fields.CharField(max_length=64, null=True)
    # 对应 action_item.last_seen_meeting_id：最后一次在哪个会议被提及（结转）
    last_seen_meeting_id = fields.IntField(null=True)
    # 对应 action_item.created_by
    created_by = fields.IntField()
    # 对应 action_item.create_time / update_time
    create_time = fields.DatetimeField(auto_now_add=True)
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "action_item"
        indexes = [("series_id", "status"), ("meeting_id",),
                   ("owner_user_id",), ("dedupe_key",)]


class ActionItemEvent(Model):
    """待办变更留痕：状态/负责人/期限/结转各写一行。"""

    # 对应 action_item_event.id
    id = fields.IntField(pk=True)
    # 对应 action_item_event.action_item_id
    action_item_id = fields.IntField()
    # 对应 action_item_event.event_type：CREATE / STATUS / OWNER / DEADLINE / CARRY_OVER
    event_type = fields.CharField(max_length=30)
    # 对应 action_item_event.from_value / to_value
    from_value = fields.CharField(max_length=200, null=True)
    to_value = fields.CharField(max_length=200, null=True)
    # 对应 action_item_event.note
    note = fields.TextField(null=True)
    # 对应 action_item_event.actor_id
    actor_id = fields.IntField()
    # 对应 action_item_event.create_time
    create_time = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "action_item_event"
        indexes = [("action_item_id", "create_time")]


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


class KbIndexTask(Model):
    """知识库索引任务：一类来源（一段转写的全文 / 一份已确认纪要）一行任务。

    与转写任务同构：status 用条件更新抢占，重复调度不会重复建索引；
    一个来源只留一行任务，重新索引是覆盖同一行（内容变了就重建文档版本）。
    """

    # 对应 kb_index_task.id
    id = fields.IntField(pk=True)
    # 对应 kb_index_task.meeting_id
    meeting_id = fields.IntField()
    # 对应 kb_index_task.source_type：TRANSCRIPT（转写）/ MINUTES（纪要）
    source_type = fields.CharField(max_length=30)
    # 对应 kb_index_task.source_id：source_type 为 TRANSCRIPT 时是 transcription_task.id，
    # 为 MINUTES 时是 meeting_minutes.id
    source_id = fields.IntField()
    # 对应 kb_index_task.model_config_id：嵌入模型配置
    model_config_id = fields.IntField()
    # 对应 kb_index_task.initiator_id
    initiator_id = fields.IntField()
    # 对应 kb_index_task.status：PENDING、PROCESSING、SUCCEEDED、FAILED
    status = fields.CharField(max_length=30, default="PENDING")
    # 对应 kb_index_task.stage 与 progress（给页面看的进度，不是状态机的一部分）
    stage = fields.CharField(max_length=100, default="等待执行")
    progress = fields.IntField(default=0)
    # 对应 kb_index_task.retry_count 与 max_retry
    retry_count = fields.IntField(default=0)
    max_retry = fields.IntField(default=3)
    # 对应 kb_index_task.document_id：本次产出的文档版本
    document_id = fields.IntField(null=True)
    # 对应 kb_index_task.chunk_count 与 embedded_count（后者用于"只补没嵌入的"，重试不重复付费）
    chunk_count = fields.IntField(default=0)
    embedded_count = fields.IntField(default=0)
    # 对应 kb_index_task.error_message
    error_message = fields.TextField(null=True)
    # 对应 kb_index_task.started_at 与 finished_at
    started_at = fields.DatetimeField(null=True)
    finished_at = fields.DatetimeField(null=True)
    # 对应 kb_index_task.create_time 与 update_time
    create_time = fields.DatetimeField(auto_now_add=True)
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "kb_index_task"
        # 对应唯一索引 uk_kb_index_source：一类来源只留一行任务
        unique_together = (("source_type", "source_id"),)


class KbDocument(Model):
    """一次索引产出的文档版本：一个来源 + 一次内容指纹 = 一份文档。

    同来源重建时复用同一行并覆盖切片；失败/半成品版本保留到下一次成功索引为止，
    读侧只认 `status=SUCCEEDED`，所以不会把半成品交给模型。
    """

    # 对应 kb_document.id
    id = fields.IntField(pk=True)
    # 对应 kb_document.meeting_id（检索时按会议可见性过滤就靠它）
    meeting_id = fields.IntField()
    # 对应 kb_document.source_type 与 source_id，含义同 KbIndexTask
    source_type = fields.CharField(max_length=30)
    source_id = fields.IntField()
    # 对应 kb_document.task_id：产出这份文档的索引任务
    task_id = fields.IntField()
    # 对应 kb_document.authority：RAW（转写原话）/ CONFIRMED（人确认过的纪要）
    # 这是"人拍板才算数"在知识库里的落地：答案更该引权威条目，而不是模型草稿
    authority = fields.CharField(max_length=30, default="RAW")
    # 对应 kb_document.content_hash：来源内容的指纹，用来判断要不要重建
    content_hash = fields.CharField(max_length=64)
    # 对应 kb_document.chunk_count
    chunk_count = fields.IntField(default=0)
    # 对应 kb_document.embed_model 与 embed_dim：向量维度和模型名一起存，
    # 换嵌入模型时能一眼看出哪些文档需要重嵌
    embed_model = fields.CharField(max_length=100)
    embed_dim = fields.IntField(default=0)
    # 对应 kb_document.status：BUILDING、SUCCEEDED、FAILED（BUILDING 对检索不可见）
    status = fields.CharField(max_length=30, default="SUCCEEDED")
    error_message = fields.TextField(null=True)
    # 对应 kb_document.create_time
    create_time = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "kb_document"


class KbChunk(Model):
    """知识库切片：检索的最小单位，也是引用能落到的地方。

    切片的 id 同时就是 PG 向量行的主键——不做第二套编号，
    否则"库里这条切片"和"PG 向量行"迟早对不上。
    """

    # 对应 kb_chunk.id（同时是 PG 向量行的主键）
    id = fields.IntField(pk=True)
    # 对应 kb_chunk.document_id 与 meeting_id
    document_id = fields.IntField()
    meeting_id = fields.IntField()
    # 对应 kb_chunk.chunk_no：同一文档内的顺序号
    chunk_no = fields.IntField()
    # 对应 kb_chunk.chunk_type：TRANSCRIPT_WINDOW（转写窗口）/ MINUTES_ITEM（纪要条目）
    chunk_type = fields.CharField(max_length=30)
    # 对应 kb_chunk.text：切片正文（引用回显与人工核对都靠它）
    text = fields.TextField()
    # 对应 kb_chunk.embed_text：真正送去嵌入的文本（Contextual Retrieval 的
    # 会议/时间/主题上下文 + 原文）。**与 text 分开存**：引用要回显原文并逐字定位，
    # 把上下文句写进 text 会让引用在转写里定位不到。为空表示直接用 text（未开启）。
    # 存库而不是每次重算：嵌入失败重试时不必重新付一次上下文生成的调用。
    embed_text = fields.TextField(null=True)
    # 对应 kb_chunk.speaker、start_ms、end_ms：转写窗口的定位信息
    speaker = fields.CharField(max_length=50, null=True)
    start_ms = fields.BigIntField(null=True)
    end_ms = fields.BigIntField(null=True)
    # 对应 kb_chunk.field 与 item_index：纪要条目所属字段与序号
    field = fields.CharField(max_length=30, null=True)
    item_index = fields.IntField(null=True)
    # 对应 kb_chunk.origin：纪要条目的来源标注（stated/derived/model_inferred）
    origin = fields.CharField(max_length=30, null=True)
    # 对应 kb_chunk.evidence_json：纪要条目自带的证据（引用校验要用）
    evidence_json = fields.JSONField(null=True)
    # 对应 kb_chunk.token_count：粗估字符数，用于预算与截断，不是精确 token 数
    token_count = fields.IntField(default=0)
    # 对应 kb_chunk.embedded：向量是否已写入 PG/pgvector。
    # 重试时只补 embedded=0 的切片——不重复为已成功的切片付费（沿用纪要缓存同款理由）
    embedded = fields.BooleanField(default=False)
    # 对应 kb_chunk.create_time
    create_time = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "kb_chunk"
        # 对应唯一索引 uk_kb_chunk_no
        unique_together = (("document_id", "chunk_no"),)
        # 对应 scripts/indexes.sql 的二级索引（按会议收窄检索范围）
        indexes = [("meeting_id",)]


class KbIndexOutbox(Model):
    """知识库索引的期望状态（outbox）。

    为什么不能只靠 KbIndexTask：它按“来源”唯一、且会被重新调度覆盖，无法表达
    “这一份来源现在应该被删除”。取消确认、删除纪要、删除会议这些状态变化发生时，
    来源行可能已经不在（无法再推导 source_id），必须有一行**与来源解耦**的期望状态
    留下来，后台 worker 才能把 PG 里的派生数据清干净。

    语义：每个 (source_type, source_id) 一行，desired_state 是最终目标；
    同一来源反复变化只覆盖这一行（version 递增），不堆事件。
    """

    id = fields.IntField(pk=True)
    meeting_id = fields.IntField()
    source_type = fields.CharField(max_length=30)
    source_id = fields.IntField()
    # INDEX = 来源可索引，应存在；DELETE = 来源不可索引/已删除，应清理
    desired_state = fields.CharField(max_length=20, default="INDEX")
    # PENDING / PROCESSING / DONE / FAILED
    status = fields.CharField(max_length=20, default="PENDING")
    # 同一来源每变一次就 +1；worker 处理完只认最后版本，避免旧事件覆盖新状态
    version = fields.IntField(default=1)
    attempts = fields.IntField(default=0)
    last_error = fields.TextField(null=True)
    available_at = fields.DatetimeField(null=True)
    started_at = fields.DatetimeField(null=True)
    finished_at = fields.DatetimeField(null=True)
    create_time = fields.DatetimeField(auto_now_add=True)
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "kb_index_outbox"
        unique_together = (("source_type", "source_id"),)
        indexes = [("status", "available_at"), ("meeting_id",)]


class ChatSession(Model):
    """一次连续对话（问答 Agent 的会话）。

    会话是**用户的**，不是会议的：一次对话可以跨多场会（"把这周的会都总结一下"），
    所以范围单独存 `scope_meeting_ids`（空数组 = 当前用户可见的全部会议），
    权限仍在工具层按用户实时判定——这里存的范围只是"这次对话限定看哪几场"，
    不是权限授予（用户权限被回收后，旧会话自然看不到那场会）。
    """

    # 对应 chat_session.id
    id = fields.IntField(pk=True)
    # 对应 chat_session.user_id：会话归属，列表页按它过滤
    user_id = fields.IntField()
    # 对应 chat_session.title：首次提问的前若干字，页面列表显示
    title = fields.CharField(max_length=255, default="新对话")
    # 对应 chat_session.scope_meeting_ids：本次对话限定的会议集合，空数组表示不限定
    scope_meeting_ids = fields.JSONField(default=list)
    # 对应 chat_session.scope_series_ids：按系列限定。每轮按当前可见会议展开，
    # 保留"权限收回后旧会话自然看不到"的实时性；与 scope_meeting_ids 取并集。
    scope_series_ids = fields.JSONField(default=list)
    # 对应 chat_session.summary 与 summary_upto_message_id：
    # 滑窗之外的历史压成一段摘要（会话记忆的第二层），水位线记它覆盖到哪条消息为止，
    # 于是"哪些消息已经进过摘要"不必再猜——没有水位线就只能整段重算或重复计入
    summary = fields.TextField(null=True)
    summary_upto_message_id = fields.IntField(default=0)
    # 对应 chat_session.status：ACTIVE / DELETED（删除是软删，保留消息供审计）
    status = fields.CharField(max_length=30, default="ACTIVE")
    # 对应 chat_session.message_count：会话列表直接读它，不为此对消息表做聚合
    message_count = fields.IntField(default=0)
    # 对应 chat_session.last_active_time：列表页按它倒序
    last_active_time = fields.DatetimeField(null=True)
    # 对应 chat_session.create_time 与 update_time
    create_time = fields.DatetimeField(auto_now_add=True)
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "chat_session"
        # 对应 scripts/indexes.sql 的二级索引（会话列表按用户过滤）
        indexes = [("user_id",)]


class ChatMessage(Model):
    """一条问答消息。用户与助手各占一行，工具调用与引用作为附件存在助手行上。

    助手行上的 `citations_json` 是**当时那一轮的引用快照**，不是指向切片的外键：
    切片会随文档重建被删掉（`kb_service` 重建文档时物理删除旧切片），
    而"这条回答当时引用了什么"必须留得下来——这正是知识库要能问责的前提。
    """

    # 对应 chat_message.id
    id = fields.IntField(pk=True)
    # 对应 chat_message.session_id
    session_id = fields.IntField()
    # 对应 chat_message.role：USER / ASSISTANT
    role = fields.CharField(max_length=20)
    # 对应 chat_message.content：正文（助手行是逐 token 拼起来的完整回答）
    content = fields.TextField()
    # 对应 chat_message.citations_json：这一轮答案用到的引用（含编号与出处定位）
    citations_json = fields.JSONField(null=True)
    # 对应 chat_message.tools_json：工具调用轨迹（页面折叠展示"查了什么"）
    tools_json = fields.JSONField(null=True)
    # 对应 chat_message.moderation_json：这一轮的审核留痕（命中词与处置动作）。
    # 只存词与位置，不存命中处的上下文——留痕不该复制一份违规原文
    moderation_json = fields.JSONField(null=True)
    # 对应 chat_message.plan_json：这一轮的**查询规划**（相对时间/指代改写、意图、
    # 建议工具、触发词、开关状态）。存它的理由与引用快照同款：结论要能归因——
    # 出错的到底是"改写错了"还是"检索没找到"，没有这一份就只能猜
    plan_json = fields.JSONField(null=True)
    # 对应 chat_message.stop_reason：finished / max_rounds / budget_* / stalled，
    # 用户看到的是它对应的中文提示，观测看到的是这个值
    stop_reason = fields.CharField(max_length=30, null=True)
    # 对应 chat_message.error_message：这一轮失败时的原因（失败消息也留在会话里）
    error_message = fields.TextField(null=True)
    # 对应 chat_message.token_count：本轮 token 用量（来自 ai_call_log 的用量汇总）
    token_count = fields.IntField(default=0)
    # 这一轮的生命周期：RUNNING / FINISHED / FAILED / INTERRUPTED。
    # 老数据默认 FINISHED；新助手行显式写 RUNNING，恢复/续传据此判断。
    run_status = fields.CharField(max_length=20, default="FINISHED")
    # LangGraph checkpoint 的 thread 键（chat-turn-<assistant_id>），以及最后一次
    # checkpoint。它们是恢复入口，不是业务状态。
    thread_id = fields.CharField(max_length=128, null=True)
    checkpoint_id = fields.CharField(max_length=128, null=True)
    # 这一轮共享的 trace id；SSE 恢复后仍能对上同一批模型/工具调用
    trace_id = fields.CharField(max_length=64, null=True)
    # 已记入配额的 token 数（幂等计费：恢复时不重复扣）
    charged_tokens = fields.IntField(default=0)
    # 对应 chat_message.create_time
    create_time = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "chat_message"
        # 对应 scripts/indexes.sql 的二级索引（按会话取历史消息）
        indexes = [("session_id",), ("run_status",), ("trace_id",)]


class OnlineEvalRun(Model):
    """一次在线评测运行（管理员手动触发，单次 ≤50 条）。"""

    # 对应 online_eval_run.id
    id = fields.IntField(pk=True)
    # 对应 online_eval_run.triggered_by / sample_size / model_config_id
    triggered_by = fields.IntField()
    sample_size = fields.IntField(default=0)
    model_config_id = fields.IntField()
    # 对应 online_eval_run.status：RUNNING / DONE / FAILED
    status = fields.CharField(max_length=20, default="RUNNING")
    # 对应 online_eval_run.started_at / finished_at
    started_at = fields.DatetimeField(auto_now_add=True)
    finished_at = fields.DatetimeField(null=True)
    # 对应 online_eval_run.summary_json：聚合指标与失败样例摘要
    summary_json = fields.JSONField(null=True)
    # 对应 online_eval_run.error_message
    error_message = fields.TextField(null=True)
    # 对应 online_eval_run.update_time
    update_time = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "online_eval_run"
        indexes = [("status", "started_at")]


class OnlineEvalCase(Model):
    """在线评测的单条结果：一轮问答的忠实度/相关性判定。"""

    # 对应 online_eval_case.id
    id = fields.IntField(pk=True)
    # 对应 online_eval_case.run_id / chat_message_id
    run_id = fields.IntField()
    chat_message_id = fields.IntField()
    # 对应 online_eval_case.question / answer
    question = fields.TextField()
    answer = fields.TextField()
    # 对应 online_eval_case.citations_json / tools_json
    citations_json = fields.JSONField(null=True)
    tools_json = fields.JSONField(null=True)
    # 对应 online_eval_case.scores_json：faithfulness / answer_relevancy 等
    scores_json = fields.JSONField(null=True)
    # 对应 online_eval_case.judge_model / error_message
    judge_model = fields.CharField(max_length=100, null=True)
    error_message = fields.TextField(null=True)
    # 对应 online_eval_case.create_time
    create_time = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "online_eval_case"
        indexes = [("run_id",), ("chat_message_id",)]
