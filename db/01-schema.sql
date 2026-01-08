-- AI 会议纪要助手（MeetingBot）MySQL 8.4 表结构
--
-- 职责：**只建表结构，不含任何数据、Key 或口令**。演示种子在 02-seed.sql。
-- 用法：由 db/Dockerfile 打进镜像，MySQL 数据卷为空时由
--       docker-entrypoint-initdb.d 自动执行一次（先 01-schema 再 02-seed）。
--
-- 表结构真源是 `meetingbot/fastapi-app/models.py`（Tortoise ORM 模型），
-- 本文件必须与它保持一致；改表时两边同时改。13 张表与模型类的对应关系：
--
--   department          -> Department
--   user                -> User
--   meeting             -> Meeting
--   meeting_participant -> MeetingParticipant
--   meeting_material    -> MeetingMaterial
--   ai_model_config     -> AiModelConfig
--   transcription_task  -> TranscriptionTask
--   transcript_segment  -> TranscriptSegment
--   ai_call_log         -> AiCallLog
--   prompt_template     -> PromptTemplate
--   meeting_minutes     -> MeetingMinutes
--   agent_run           -> AgentRun
--   agent_step          -> AgentStep
--
-- 设计约定：零外键，表间关系由应用层维护；业务幂等约束放在唯一索引里
-- （meeting_material(meeting_id, file_hash)、transcription_task(material_id)、
--   meeting_minutes(source_task_id)、agent_step(run_id, step_no) 等）。
--
-- 客户端字符集必须显式声明：mysql 8.4 官方镜像的客户端默认 latin1，
-- 不声明会把 UTF-8 中文按 latin1 读入再编码，库里出现双重编码乱码。

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE `department` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '部门ID',
  `name` varchar(100) NOT NULL COMMENT '部门名称',
  `description` varchar(500) DEFAULT NULL COMMENT '部门说明',
  `status` varchar(20) NOT NULL DEFAULT 'NORMAL' COMMENT '状态：NORMAL正常、DISABLED停用',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_department_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='部门表';

CREATE TABLE `user` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '用户ID',
  `username` varchar(100) NOT NULL COMMENT '登录账号',
  `password` varchar(255) NOT NULL COMMENT '登录密码',
  `name` varchar(100) NOT NULL COMMENT '姓名',
  `avatar` varchar(500) DEFAULT NULL COMMENT '头像地址',
  `role` varchar(30) NOT NULL DEFAULT 'EMPLOYEE' COMMENT '角色：ADMIN管理员、EMPLOYEE员工',
  `phone` varchar(30) DEFAULT NULL COMMENT '联系电话',
  `email` varchar(100) DEFAULT NULL COMMENT '电子邮箱',
  `department_id` int DEFAULT NULL COMMENT '所属部门ID',
  `position` varchar(100) DEFAULT NULL COMMENT '员工职位',
  `status` varchar(20) NOT NULL DEFAULT 'NORMAL' COMMENT '账号状态：NORMAL正常、DISABLED停用',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_username` (`username`),
  KEY `idx_user_role` (`role`),
  KEY `idx_user_department_id` (`department_id`),
  KEY `idx_user_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='统一用户表';

CREATE TABLE `meeting` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '会议ID',
  `meeting_no` varchar(50) NOT NULL COMMENT '会议编号',
  `title` varchar(255) NOT NULL COMMENT '会议主题',
  `start_time` datetime NOT NULL COMMENT '开始时间',
  `end_time` datetime NOT NULL COMMENT '结束时间',
  `location` varchar(255) DEFAULT NULL COMMENT '会议地点或线上会议地址',
  `agenda` text COMMENT '会议议程',
  `creator_id` int NOT NULL COMMENT '创建人用户ID',
  `host_id` int NOT NULL COMMENT '主持人用户ID',
  `status` varchar(30) NOT NULL DEFAULT 'SCHEDULED' COMMENT '状态：SCHEDULED待开始、IN_PROGRESS进行中、FINISHED已结束、CANCELLED已取消',
  `cancel_reason` varchar(500) DEFAULT NULL COMMENT '取消原因',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_meeting_no` (`meeting_no`),
  KEY `idx_meeting_creator_id` (`creator_id`),
  KEY `idx_meeting_host_id` (`host_id`),
  KEY `idx_meeting_status_start_time` (`status`, `start_time`),
  KEY `idx_meeting_start_creator_status` (`start_time`, `creator_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会议表';

CREATE TABLE `meeting_participant` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '参会记录ID',
  `meeting_id` int NOT NULL COMMENT '会议ID',
  `user_id` int NOT NULL COMMENT '参会员工用户ID',
  `participant_role` varchar(30) NOT NULL DEFAULT 'PARTICIPANT' COMMENT '会议角色：CREATOR创建人、HOST主持人、PARTICIPANT参会人',
  `response_status` varchar(30) NOT NULL DEFAULT 'PENDING' COMMENT '参会状态：PENDING待确认、ACCEPTED参加、DECLINED不参加',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_meeting_participant` (`meeting_id`, `user_id`),
  KEY `idx_meeting_participant_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会议参会人员表';

CREATE TABLE `meeting_material` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '会议资料ID',
  `meeting_id` int NOT NULL COMMENT '所属会议ID',
  `file_name` varchar(255) NOT NULL COMMENT '用户上传时的原始文件名',
  `storage_name` varchar(255) NOT NULL COMMENT '服务端唯一存储文件名',
  `file_type` varchar(30) NOT NULL COMMENT '资料类型：AUDIO录音、VIDEO视频、ATTACHMENT附件',
  `content_type` varchar(150) DEFAULT NULL COMMENT '文件MIME类型',
  `file_ext` varchar(30) DEFAULT NULL COMMENT '小写文件扩展名',
  `file_size` bigint NOT NULL DEFAULT 0 COMMENT '文件字节数',
  `file_hash` char(64) NOT NULL COMMENT '文件SHA-256摘要，用于会议内重复上传判断',
  `uploader_id` int NOT NULL COMMENT '上传人用户ID',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_meeting_material_storage_name` (`storage_name`),
  UNIQUE KEY `uk_meeting_material_hash` (`meeting_id`, `file_hash`),
  KEY `idx_meeting_material_type_time` (`meeting_id`, `file_type`, `create_time`),
  KEY `idx_meeting_material_uploader_id` (`uploader_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会议录音、视频和附件表';

CREATE TABLE `ai_model_config` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT 'AI模型配置ID',
  `name` varchar(100) NOT NULL COMMENT '配置名称',
  `model_type` varchar(30) NOT NULL DEFAULT 'TRANSCRIPTION' COMMENT 'TRANSCRIPTION、MINUTES、AGENT、SPEAKER',
  `provider` varchar(50) NOT NULL DEFAULT 'OPENAI_COMPATIBLE' COMMENT '服务商类型：OPENAI_COMPATIBLE、DASHSCOPE',
  `base_url` varchar(500) NOT NULL COMMENT '模型服务API基础地址',
  `api_key` varchar(1000) NOT NULL COMMENT '模型服务API Key',
  `model_name` varchar(100) NOT NULL COMMENT '模型服务中的模型名称',
  `timeout_seconds` int NOT NULL DEFAULT 600 COMMENT '单个分段请求超时秒数',
  `enabled` tinyint(1) NOT NULL DEFAULT 0 COMMENT '是否启用：0否、1是',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_ai_model_type_enabled` (`model_type`, `enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI模型配置表';

CREATE TABLE `transcription_task` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '转写任务ID',
  `meeting_id` int NOT NULL COMMENT '会议ID',
  `material_id` int NOT NULL COMMENT '录音或视频资料ID',
  `model_config_id` int NOT NULL COMMENT '本次使用的AI模型配置ID',
  `initiator_id` int NOT NULL COMMENT '发起人用户ID',
  `language` varchar(20) NOT NULL DEFAULT 'zh' COMMENT '音频主要语言ISO-639-1代码',
  `status` varchar(30) NOT NULL DEFAULT 'PENDING' COMMENT '状态：PENDING、PROCESSING、SUCCEEDED、FAILED',
  `stage` varchar(100) NOT NULL DEFAULT '等待执行' COMMENT '当前执行阶段',
  `progress` int NOT NULL DEFAULT 0 COMMENT '转写进度0至100',
  `retry_count` int NOT NULL DEFAULT 0 COMMENT '已重试次数',
  `max_retry` int NOT NULL DEFAULT 3 COMMENT '允许的最大重试次数',
  `total_duration_ms` bigint NOT NULL DEFAULT 0 COMMENT '音频总时长毫秒',
  `full_text` longtext COMMENT '全部转写文本',
  `error_message` text COMMENT '失败原因',
  `started_at` datetime DEFAULT NULL COMMENT '开始执行时间',
  `finished_at` datetime DEFAULT NULL COMMENT '完成或失败时间',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_transcription_material_id` (`material_id`),
  KEY `idx_transcription_meeting_status` (`meeting_id`, `status`),
  KEY `idx_transcription_initiator_id` (`initiator_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='音视频异步转写任务表';

CREATE TABLE `transcript_segment` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '转写片段ID',
  `task_id` int NOT NULL COMMENT '转写任务ID',
  `meeting_id` int NOT NULL COMMENT '会议ID',
  `segment_no` int NOT NULL COMMENT '时间轴顺序号',
  `start_ms` bigint NOT NULL DEFAULT 0 COMMENT '片段开始毫秒',
  `end_ms` bigint NOT NULL DEFAULT 0 COMMENT '片段结束毫秒',
  `speaker_label` varchar(50) DEFAULT NULL COMMENT '模型识别的说话人标签',
  `speaker_user_id` int DEFAULT NULL COMMENT '人工匹配的参会用户ID',
  `original_text` text NOT NULL COMMENT '模型原始转写文本',
  `content` text NOT NULL COMMENT '页面当前使用的可修订文本',
  `revised_by` int DEFAULT NULL COMMENT '最后修订人用户ID',
  `revised_time` datetime DEFAULT NULL COMMENT '最后修订时间',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_transcript_task_segment` (`task_id`, `segment_no`),
  KEY `idx_transcript_meeting_time` (`meeting_id`, `start_ms`),
  KEY `idx_transcript_speaker_user_id` (`speaker_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='带时间轴和说话人的转写片段表';

CREATE TABLE `ai_call_log` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT 'AI调用日志ID',
  `request_id` varchar(50) NOT NULL COMMENT '单次模型请求唯一编号',
  `call_type` varchar(50) NOT NULL COMMENT '转写、纪要、纪要自检或说话人匹配的调用类型',
  `biz_id` int NOT NULL COMMENT '关联转写任务、会议纪要、自检运行或说话人匹配任务ID',
  `model_config_id` int NOT NULL COMMENT '模型配置ID',
  `model_name` varchar(100) NOT NULL COMMENT '调用的模型名称',
  `request_size` bigint NOT NULL DEFAULT 0 COMMENT '本次请求内容字节数',
  `response_size` bigint NOT NULL DEFAULT 0 COMMENT '模型返回JSON字节数',
  `status` varchar(30) NOT NULL COMMENT '调用状态：SUCCEEDED、FAILED',
  `elapsed_ms` bigint NOT NULL DEFAULT 0 COMMENT '调用耗时毫秒',
  `usage_json` json DEFAULT NULL COMMENT '模型返回的usage统计',
  `error_message` text COMMENT '调用失败原因',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '调用时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ai_call_request_id` (`request_id`),
  KEY `idx_ai_call_type_time` (`call_type`, `create_time`),
  KEY `idx_ai_call_biz_id` (`biz_id`),
  KEY `idx_ai_call_model_time_status` (`model_config_id`, `create_time`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI模型调用日志表';

CREATE TABLE `prompt_template` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT 'Prompt模板ID',
  `code` varchar(100) NOT NULL COMMENT '模板唯一编码',
  `name` varchar(100) NOT NULL COMMENT '模板名称',
  `scene_type` varchar(50) NOT NULL DEFAULT 'MEETING_MINUTES' COMMENT '使用场景',
  `system_prompt` longtext NOT NULL COMMENT '系统指令',
  `user_prompt` longtext NOT NULL COMMENT '包含变量占位符的用户模板',
  `enabled` tinyint(1) NOT NULL DEFAULT 1 COMMENT '是否启用',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_prompt_code` (`code`),
  KEY `idx_prompt_scene_enabled` (`scene_type`, `enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Prompt模板表';

CREATE TABLE `meeting_minutes` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '会议纪要ID',
  `meeting_id` int NOT NULL COMMENT '会议ID',
  `source_task_id` int NOT NULL COMMENT '来源转写任务ID',
  `model_config_id` int NOT NULL COMMENT '纪要生成模型配置ID',
  `status` varchar(30) NOT NULL DEFAULT 'GENERATING' COMMENT '状态：GENERATING、DRAFT、CONFIRMED、FAILED',
  `stage` varchar(100) NOT NULL DEFAULT '等待生成' COMMENT '当前生成阶段',
  `progress` int NOT NULL DEFAULT 0 COMMENT '生成进度0至100',
  `summary` longtext COMMENT '会议摘要',
  `topics_json` json NOT NULL COMMENT '议题总结数组',
  `viewpoints_json` json NOT NULL COMMENT '发言观点数组',
  `decisions_json` json NOT NULL COMMENT '会议决策数组',
  `pending_items_json` json NOT NULL COMMENT '待确认事项数组',
  `risks_json` json NOT NULL COMMENT '风险与争议点数组',
  `created_by` int NOT NULL COMMENT '生成发起人ID',
  `confirmed_by` int DEFAULT NULL COMMENT '确认人ID',
  `confirmed_time` datetime DEFAULT NULL COMMENT '确认时间',
  `error_message` text COMMENT '生成失败原因',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_minutes_source_task` (`source_task_id`),
  KEY `idx_minutes_meeting_status` (`meeting_id`, `status`),
  KEY `idx_minutes_source_task_id` (`source_task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI会议纪要表';

CREATE TABLE `agent_run` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自检运行ID',
  `run_no` varchar(50) NOT NULL COMMENT '自检运行唯一编号',
  `minutes_id` int NOT NULL COMMENT '被自检的会议纪要ID',
  `meeting_id` int NOT NULL COMMENT '所属会议ID',
  `user_id` int NOT NULL COMMENT '发起人用户ID',
  `model_config_id` int NOT NULL COMMENT '自检模型配置ID',
  `status` varchar(30) NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING、WAITING_CONFIRMATION、SUCCEEDED、REJECTED、FAILED、INTERRUPTED',
  `stage` varchar(100) NOT NULL DEFAULT '等待执行' COMMENT '当前执行阶段',
  `progress` int NOT NULL DEFAULT 0 COMMENT '自检进度百分比',
  `current_round` int NOT NULL DEFAULT 0 COMMENT '已完成的反思轮次',
  `max_rounds` int NOT NULL DEFAULT 2 COMMENT '反思轮次上限，一轮等于审查一次加改一版',
  `passed` tinyint(1) NOT NULL DEFAULT 0 COMMENT '最后一轮模型是否判定通过',
  `score` int NOT NULL DEFAULT 0 COMMENT '最后一轮的自检得分',
  `issue_count` int NOT NULL DEFAULT 0 COMMENT '最后一轮剩余的问题条数',
  `review_json` json NOT NULL COMMENT '最后一轮的问题清单',
  `revised_json` json DEFAULT NULL COMMENT '待人工确认的修订稿，为空表示纪要一个字都没改',
  `applied_by` int DEFAULT NULL COMMENT '确认人用户ID',
  `applied_time` datetime DEFAULT NULL COMMENT '确认时间',
  `reject_reason` varchar(500) DEFAULT NULL COMMENT '人工放弃修订的原因',
  `final_result` longtext COMMENT '自检结论',
  `error_message` text COMMENT '运行失败原因',
  `started_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '开始时间',
  `finished_at` datetime DEFAULT NULL COMMENT '结束时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_agent_run_no` (`run_no`),
  KEY `idx_agent_run_minutes_status` (`minutes_id`, `status`),
  KEY `idx_agent_run_user_time` (`user_id`, `started_at`),
  KEY `idx_agent_run_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会议纪要自检运行表';

CREATE TABLE `agent_step` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自检步骤ID',
  `run_id` int NOT NULL COMMENT '自检运行ID',
  `step_no` int NOT NULL COMMENT '运行内步骤号',
  `round_no` int NOT NULL DEFAULT 0 COMMENT '所属反思轮次',
  `step_type` varchar(30) NOT NULL COMMENT 'REVIEW、REFINE、FINAL',
  `decision_summary` text COMMENT '这一步要做什么',
  `observation_summary` text COMMENT '这一步的结论，审查步里是问题清单',
  `payload_json` json DEFAULT NULL COMMENT '这一步的结构化产物，审查步是打分和问题，重写步是新纪要',
  `status` varchar(30) NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING、SUCCEEDED、FAILED',
  `elapsed_ms` bigint NOT NULL DEFAULT 0 COMMENT '步骤耗时毫秒',
  `error_message` text COMMENT '步骤失败原因',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `finish_time` datetime DEFAULT NULL COMMENT '完成时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_agent_run_step` (`run_id`, `step_no`),
  KEY `idx_agent_step_run_status` (`run_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会议纪要自检步骤表';

SET FOREIGN_KEY_CHECKS = 1;
