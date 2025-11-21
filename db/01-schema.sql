-- MeetingBot 表结构（空库初始化用）
--
-- 来源：原 Navicat 导出文件按语句类型拆分（表结构 / 演示数据）。
-- 职责：**只建表结构，不含任何数据**——演示数据在 02-seed.sql。
--
-- 与 ORM 的关系：`meetingbot/fastapi-app/models.py` 是模型的真源，本文件必须与它一致；
-- 不一致会当场被 tests/test_schema_sync.py 拦下（它逐表逐列比对，不需要数据库）。
-- 后续改表请走迁移：`cd meetingbot/fastapi-app && aerich migrate --name xxx && aerich upgrade`，
-- 并把结果同步回本文件，保证"空库一次初始化 = 最新结构"。

/*
 Navicat Premium Dump SQL

 Source Server         : localhost
 Source Server Type    : MySQL
 Source Server Version : 80040 (8.0.40)
 Source Host           : localhost:3306
 Source Schema         : meetingbot

 Target Server Type    : MySQL
 Target Server Version : 80040 (8.0.40)
 File Encoding         : 65001

 Date: 16/09/2026 17:26:26
*/

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ----------------------------
-- Table structure for agent_run
-- ----------------------------
DROP TABLE IF EXISTS `agent_run`;
CREATE TABLE `agent_run`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自检运行ID',
  `run_no` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '自检运行唯一编号',
  `minutes_id` int NOT NULL COMMENT '被自检的会议纪要ID',
  `meeting_id` int NOT NULL COMMENT '所属会议ID',
  `user_id` int NOT NULL COMMENT '发起人用户ID',
  `model_config_id` int NOT NULL COMMENT '自检模型配置ID',
  `status` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING、WAITING_CONFIRMATION、SUCCEEDED、REJECTED、FAILED、INTERRUPTED',
  `stage` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '等待执行' COMMENT '当前执行阶段',
  `progress` int NOT NULL DEFAULT 0 COMMENT '自检进度百分比',
  `current_round` int NOT NULL DEFAULT 0 COMMENT '已完成的反思轮次',
  `max_rounds` int NOT NULL DEFAULT 2 COMMENT '反思轮次上限，一轮等于审查一次加改一版',
  `passed` tinyint(1) NOT NULL DEFAULT 0 COMMENT '最后一轮模型是否判定通过',
  `score` int NOT NULL DEFAULT 0 COMMENT '最后一轮的自检得分',
  `issue_count` int NOT NULL DEFAULT 0 COMMENT '最后一轮剩余的问题条数',
  `review_json` json NOT NULL COMMENT '最后一轮的问题清单',
  `revised_json` json NULL COMMENT '待人工确认的修订稿，为空表示纪要一个字都没改',
  `applied_by` int NULL DEFAULT NULL COMMENT '确认人用户ID',
  `applied_time` datetime(6) NULL DEFAULT NULL COMMENT '确认时间',
  `reject_reason` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '人工放弃修订的原因',
  `final_result` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '自检结论',
  `error_message` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '运行失败原因',
  `started_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '开始时间',
  `finished_at` datetime(6) NULL DEFAULT NULL COMMENT '结束时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_agent_run_no`(`run_no` ASC) USING BTREE,
  INDEX `idx_agent_run_user_time`(`user_id` ASC, `started_at` ASC) USING BTREE,
  INDEX `idx_agent_run_status`(`status` ASC) USING BTREE,
  INDEX `idx_agent_run_minutes_status`(`minutes_id` ASC, `status` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 5 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '会议纪要自检运行表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for agent_step
-- ----------------------------
DROP TABLE IF EXISTS `agent_step`;
CREATE TABLE `agent_step`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自检步骤ID',
  `run_id` int NOT NULL COMMENT '自检运行ID',
  `step_no` int NOT NULL COMMENT '运行内步骤号',
  `round_no` int NOT NULL DEFAULT 0 COMMENT '所属反思轮次',
  `step_type` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'REVIEW、REFINE、FINAL',
  `decision_summary` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '这一步要做什么',
  `observation_summary` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '这一步的结论，审查步里是问题清单',
  `payload_json` json NULL COMMENT '这一步的结构化产物，审查步是打分和问题，重写步是新纪要',
  `status` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING、SUCCEEDED、FAILED',
  `elapsed_ms` bigint NOT NULL DEFAULT 0 COMMENT '步骤耗时毫秒',
  `error_message` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '步骤失败原因',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `finish_time` datetime(6) NULL DEFAULT NULL COMMENT '完成时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_agent_run_step`(`run_id` ASC, `step_no` ASC) USING BTREE,
  INDEX `idx_agent_step_run_status`(`run_id` ASC, `status` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 21 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '会议纪要自检步骤表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for ai_call_log
-- ----------------------------
DROP TABLE IF EXISTS `ai_call_log`;
CREATE TABLE `ai_call_log`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT 'AI调用日志ID',
  `request_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '单次模型请求唯一编号',
  `call_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '转写、纪要、纪要自检或说话人匹配的调用类型',
  `biz_id` int NOT NULL COMMENT '关联转写、纪要、索引、问答、Agent运行或发起用户ID',
  `model_config_id` int NOT NULL COMMENT '模型配置ID',
  `model_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '调用的模型名称',
  `request_size` bigint NOT NULL DEFAULT 0 COMMENT '本次请求内容字节数',
  `response_size` bigint NOT NULL DEFAULT 0 COMMENT '模型返回JSON字节数',
  `status` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '调用状态：SUCCEEDED、FAILED',
  `elapsed_ms` bigint NOT NULL DEFAULT 0 COMMENT '调用耗时毫秒',
  `usage_json` json NULL COMMENT '模型返回的usage统计',
  `cost_micro` bigint NOT NULL DEFAULT 0 COMMENT '本次调用成本（微元，1e-6 元），按调用时的单价与真实用量算出',
  `trace_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '链路追踪ID（同一次业务操作的多次调用共享）',
  `error_message` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '调用失败原因',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '调用时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_ai_call_request_id`(`request_id` ASC) USING BTREE,
  INDEX `idx_ai_call_type_time`(`call_type` ASC, `create_time` ASC) USING BTREE,
  INDEX `idx_ai_call_biz_id`(`biz_id` ASC) USING BTREE,
  INDEX `idx_ai_call_model_time_status`(`model_config_id` ASC, `create_time` ASC, `status` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 33 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = 'AI模型调用日志表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for ai_model_config
-- ----------------------------
DROP TABLE IF EXISTS `ai_model_config`;
CREATE TABLE `ai_model_config`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT 'AI模型配置ID',
  `name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '配置名称',
  `model_type` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'TRANSCRIPTION' COMMENT 'TRANSCRIPTION、MINUTES、AGENT、SPEAKER',
  `provider` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'OPENAI_COMPATIBLE' COMMENT '服务商类型：OPENAI_COMPATIBLE',
  `base_url` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '兼容OpenAI协议的API基础地址',
  `api_key` varchar(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '模型服务API Key',
  `model_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '模型服务中的模型名称',
  `timeout_seconds` int NOT NULL DEFAULT 600 COMMENT '单个分段请求超时秒数',
  `input_price_micro_per_1k` int NOT NULL DEFAULT 0 COMMENT '输入单价：每1000 token 的微元数（1e-6 元），0 表示未定价',
  `output_price_micro_per_1k` int NOT NULL DEFAULT 0 COMMENT '输出单价：每1000 token 的微元数（1e-6 元），0 表示未定价',
  `enabled` tinyint(1) NOT NULL DEFAULT 0 COMMENT '是否启用：0否、1是',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_ai_model_type_enabled`(`model_type` ASC, `enabled` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 7 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = 'AI模型配置表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for department
-- ----------------------------
DROP TABLE IF EXISTS `department`;
CREATE TABLE `department`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '部门ID',
  `name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '部门名称',
  `description` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '部门说明',
  `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'NORMAL' COMMENT '状态：NORMAL正常、DISABLED停用',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_department_status`(`status` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 3 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '部门表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for meeting
-- ----------------------------
DROP TABLE IF EXISTS `meeting`;
CREATE TABLE `meeting`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '会议ID',
  `meeting_no` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '会议编号',
  `title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '会议主题',
  `start_time` datetime(6) NOT NULL COMMENT '开始时间',
  `end_time` datetime(6) NOT NULL COMMENT '结束时间',
  `location` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '会议地点或线上会议地址',
  `agenda` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '会议议程',
  `creator_id` int NOT NULL COMMENT '创建人用户ID',
  `host_id` int NOT NULL COMMENT '主持人用户ID',
  `status` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'SCHEDULED' COMMENT '状态：SCHEDULED待开始、IN_PROGRESS进行中、FINISHED已结束、CANCELLED已取消',
  `cancel_reason` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '取消原因',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_meeting_no`(`meeting_no` ASC) USING BTREE,
  INDEX `idx_meeting_creator_id`(`creator_id` ASC) USING BTREE,
  INDEX `idx_meeting_host_id`(`host_id` ASC) USING BTREE,
  INDEX `idx_meeting_status_start_time`(`status` ASC, `start_time` ASC) USING BTREE,
  INDEX `idx_meeting_start_creator_status`(`start_time` ASC, `creator_id` ASC, `status` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 3 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '会议表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for meeting_material
-- ----------------------------
DROP TABLE IF EXISTS `meeting_material`;
CREATE TABLE `meeting_material`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '会议资料ID',
  `meeting_id` int NOT NULL COMMENT '所属会议ID',
  `file_name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '用户上传时的原始文件名',
  `storage_name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '服务端唯一存储文件名',
  `file_type` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '资料类型：AUDIO录音、VIDEO视频、ATTACHMENT附件',
  `content_type` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '文件MIME类型',
  `file_ext` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '小写文件扩展名',
  `file_size` bigint NOT NULL DEFAULT 0 COMMENT '文件字节数',
  `file_hash` char(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '文件SHA-256摘要，用于会议内重复上传判断',
  `uploader_id` int NOT NULL COMMENT '上传人用户ID',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '上传时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_meeting_material_storage_name`(`storage_name` ASC) USING BTREE,
  UNIQUE INDEX `uk_meeting_material_hash`(`meeting_id` ASC, `file_hash` ASC) USING BTREE,
  INDEX `idx_meeting_material_type_time`(`meeting_id` ASC, `file_type` ASC, `create_time` ASC) USING BTREE,
  INDEX `idx_meeting_material_uploader_id`(`uploader_id` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 8 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '会议录音、视频和附件表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for meeting_minutes
-- ----------------------------
DROP TABLE IF EXISTS `meeting_minutes`;
CREATE TABLE `meeting_minutes`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '会议纪要ID',
  `meeting_id` int NOT NULL COMMENT '会议ID',
  `source_task_id` int NOT NULL COMMENT '来源转写任务ID',
  `model_config_id` int NOT NULL COMMENT '纪要生成模型配置ID',
  `status` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'GENERATING' COMMENT '状态：GENERATING、DRAFT、CONFIRMED、FAILED',
  `stage` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '等待生成' COMMENT '当前生成阶段',
  `progress` int NOT NULL DEFAULT 0 COMMENT '生成进度0至100',
  `summary` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '会议摘要',
  `topics_json` json NOT NULL COMMENT '议题总结数组',
  `viewpoints_json` json NOT NULL COMMENT '发言观点数组',
  `decisions_json` json NOT NULL COMMENT '会议决策数组',
  `pending_items_json` json NOT NULL COMMENT '待确认事项数组',
  `risks_json` json NOT NULL COMMENT '风险与争议点数组',
  `created_by` int NOT NULL COMMENT '生成发起人ID',
  `confirmed_by` int NULL DEFAULT NULL COMMENT '确认人ID',
  `confirmed_time` datetime(6) NULL DEFAULT NULL COMMENT '确认时间',
  `error_message` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '生成失败原因',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_minutes_source_task`(`source_task_id` ASC) USING BTREE,
  INDEX `idx_minutes_meeting_status`(`meeting_id` ASC, `status` ASC) USING BTREE,
  INDEX `idx_minutes_source_task_id`(`source_task_id` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 5 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = 'AI会议纪要版本表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for meeting_participant
-- ----------------------------
DROP TABLE IF EXISTS `meeting_participant`;
CREATE TABLE `meeting_participant`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '参会记录ID',
  `meeting_id` int NOT NULL COMMENT '会议ID',
  `user_id` int NOT NULL COMMENT '参会员工用户ID',
  `participant_role` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PARTICIPANT' COMMENT '会议角色：CREATOR创建人、HOST主持人、PARTICIPANT参会人',
  `response_status` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PENDING' COMMENT '参会状态：PENDING待确认、ACCEPTED参加、DECLINED不参加',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_meeting_participant`(`meeting_id` ASC, `user_id` ASC) USING BTREE,
  INDEX `idx_meeting_participant_user_id`(`user_id` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 15 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '会议参会人员表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for prompt_template
-- ----------------------------
DROP TABLE IF EXISTS `prompt_template`;
CREATE TABLE `prompt_template`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT 'Prompt模板ID',
  `code` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '模板唯一编码',
  `name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '模板名称',
  `scene_type` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MEETING_MINUTES' COMMENT '使用场景',
  `system_prompt` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '系统指令',
  `user_prompt` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '包含变量占位符的用户模板',
  `enabled` tinyint(1) NOT NULL DEFAULT 1 COMMENT '是否启用',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_prompt_code`(`code` ASC) USING BTREE,
  INDEX `idx_prompt_scene_enabled`(`scene_type` ASC, `enabled` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 15 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = 'Prompt模板表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for transcript_segment
-- ----------------------------
DROP TABLE IF EXISTS `transcript_segment`;
CREATE TABLE `transcript_segment`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '转写片段ID',
  `task_id` int NOT NULL COMMENT '转写任务ID',
  `meeting_id` int NOT NULL COMMENT '会议ID',
  `segment_no` int NOT NULL COMMENT '时间轴顺序号',
  `start_ms` bigint NOT NULL DEFAULT 0 COMMENT '片段开始毫秒',
  `end_ms` bigint NOT NULL DEFAULT 0 COMMENT '片段结束毫秒',
  `speaker_label` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '模型识别的说话人标签',
  `speaker_user_id` int NULL DEFAULT NULL COMMENT '人工匹配的参会用户ID',
  `original_text` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '模型原始转写文本',
  `content` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '页面当前使用的可修订文本',
  `revised_by` int NULL DEFAULT NULL COMMENT '最后修订人用户ID',
  `revised_time` datetime(6) NULL DEFAULT NULL COMMENT '最后修订时间',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_transcript_task_segment`(`task_id` ASC, `segment_no` ASC) USING BTREE,
  INDEX `idx_transcript_meeting_time`(`meeting_id` ASC, `start_ms` ASC) USING BTREE,
  INDEX `idx_transcript_speaker_user_id`(`speaker_user_id` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 436 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '带时间轴和说话人的转写片段表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for transcription_task
-- ----------------------------
DROP TABLE IF EXISTS `transcription_task`;
CREATE TABLE `transcription_task`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '转写任务ID',
  `meeting_id` int NOT NULL COMMENT '会议ID',
  `material_id` int NOT NULL COMMENT '录音或视频资料ID',
  `model_config_id` int NOT NULL COMMENT '本次使用的AI模型配置ID',
  `initiator_id` int NOT NULL COMMENT '发起人用户ID',
  `language` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'zh' COMMENT '音频主要语言ISO-639-1代码',
  `status` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PENDING' COMMENT '状态：PENDING、PROCESSING、SUCCEEDED、FAILED',
  `stage` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '等待执行' COMMENT '当前执行阶段',
  `progress` int NOT NULL DEFAULT 0 COMMENT '转写进度0至100',
  `retry_count` int NOT NULL DEFAULT 0 COMMENT '已重试次数',
  `max_retry` int NOT NULL DEFAULT 3 COMMENT '允许的最大重试次数',
  `total_duration_ms` bigint NOT NULL DEFAULT 0 COMMENT '音频总时长毫秒',
  `full_text` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '全部转写文本',
  `error_message` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '失败原因',
  `started_at` datetime(6) NULL DEFAULT NULL COMMENT '开始执行时间',
  `finished_at` datetime(6) NULL DEFAULT NULL COMMENT '完成或失败时间',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_transcription_material_id`(`material_id` ASC) USING BTREE,
  INDEX `idx_transcription_meeting_status`(`meeting_id` ASC, `status` ASC) USING BTREE,
  INDEX `idx_transcription_initiator_id`(`initiator_id` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 8 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '音视频异步转写任务表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for user
-- ----------------------------
DROP TABLE IF EXISTS `user`;
CREATE TABLE `user`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '用户ID',
  `username` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '登录账号',
  `password` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '登录密码',
  `name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '姓名',
  `avatar` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '头像地址',
  `role` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'EMPLOYEE' COMMENT '角色：ADMIN管理员、EMPLOYEE员工',
  `phone` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '联系电话',
  `email` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '电子邮箱',
  `department_id` int NULL DEFAULT NULL COMMENT '所属部门ID',
  `position` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '员工职位',
  `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'NORMAL' COMMENT '账号状态：NORMAL正常、DISABLED停用',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_user_username`(`username` ASC) USING BTREE,
  INDEX `idx_user_role`(`role` ASC) USING BTREE,
  INDEX `idx_user_department_id`(`department_id` ASC) USING BTREE,
  INDEX `idx_user_status`(`status` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 13 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '统一用户表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for kb_index_task
-- ----------------------------
DROP TABLE IF EXISTS `kb_index_task`;
CREATE TABLE `kb_index_task`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '索引任务ID',
  `meeting_id` int NOT NULL COMMENT '所属会议ID',
  `source_type` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '来源类型：TRANSCRIPT转写、MINUTES纪要',
  `source_id` int NOT NULL COMMENT '来源ID：转写任务是transcription_task.id，纪要是meeting_minutes.id',
  `model_config_id` int NOT NULL COMMENT '嵌入模型配置ID',
  `initiator_id` int NOT NULL COMMENT '发起人用户ID',
  `status` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PENDING' COMMENT '任务状态：PENDING、PROCESSING、SUCCEEDED、FAILED',
  `stage` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '等待执行' COMMENT '当前阶段文案（给页面看）',
  `progress` int NOT NULL DEFAULT 0 COMMENT '进度百分比',
  `retry_count` int NOT NULL DEFAULT 0 COMMENT '已重试次数',
  `max_retry` int NOT NULL DEFAULT 3 COMMENT '最大重试次数',
  `document_id` int NULL DEFAULT NULL COMMENT '本次产出的知识库文档ID',
  `chunk_count` int NOT NULL DEFAULT 0 COMMENT '切片总数',
  `embedded_count` int NOT NULL DEFAULT 0 COMMENT '已写入向量的切片数（重试时只补没嵌入的）',
  `error_message` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '失败原因',
  `started_at` datetime(6) NULL DEFAULT NULL COMMENT '开始时间',
  `finished_at` datetime(6) NULL DEFAULT NULL COMMENT '结束时间',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_kb_index_source`(`source_type` ASC, `source_id` ASC) USING BTREE,
  INDEX `idx_kb_index_meeting`(`meeting_id` ASC) USING BTREE,
  INDEX `idx_kb_index_status`(`status` ASC) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '知识库索引任务表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for kb_document
-- ----------------------------
DROP TABLE IF EXISTS `kb_document`;
CREATE TABLE `kb_document`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '知识库文档ID',
  `meeting_id` int NOT NULL COMMENT '所属会议ID（检索按会议可见性过滤的依据）',
  `source_type` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '来源类型：TRANSCRIPT、MINUTES',
  `source_id` int NOT NULL COMMENT '来源ID',
  `task_id` int NOT NULL COMMENT '产出这份文档的索引任务ID',
  `authority` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'RAW' COMMENT '权威级别：RAW转写原话、CONFIRMED人已确认的纪要',
  `content_hash` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '来源内容指纹，用于判断是否需要重建',
  `chunk_count` int NOT NULL DEFAULT 0 COMMENT '切片数',
  `embed_model` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '嵌入模型名',
  `embed_dim` int NOT NULL DEFAULT 0 COMMENT '向量维度',
  `status` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'SUCCEEDED' COMMENT '状态：SUCCEEDED、FAILED',
  `error_message` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '失败原因',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_kb_document_meeting`(`meeting_id` ASC) USING BTREE,
  INDEX `idx_kb_document_source`(`source_type` ASC, `source_id` ASC) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '知识库文档版本表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for kb_chunk
-- ----------------------------
DROP TABLE IF EXISTS `kb_chunk`;
CREATE TABLE `kb_chunk`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '切片ID（同时是 Qdrant 里的 point id）',
  `document_id` int NOT NULL COMMENT '所属知识库文档ID',
  `meeting_id` int NOT NULL COMMENT '所属会议ID',
  `chunk_no` int NOT NULL COMMENT '文档内顺序号',
  `chunk_type` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '切片类型：TRANSCRIPT_WINDOW转写窗口、MINUTES_ITEM纪要条目',
  `text` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '切片正文',
  `embed_text` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '用于嵌入的文本（上下文+原文）；为空表示直接用 text',
  `speaker` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '说话人标签（转写窗口）',
  `start_ms` bigint NULL DEFAULT NULL COMMENT '起始毫秒（转写窗口）',
  `end_ms` bigint NULL DEFAULT NULL COMMENT '结束毫秒（转写窗口）',
  `field` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '所属纪要字段（纪要条目）',
  `item_index` int NULL DEFAULT NULL COMMENT '该字段内的条目序号（纪要条目）',
  `origin` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '条目来源标注：stated、derived、model_inferred',
  `evidence_json` json NULL COMMENT '条目自带的证据（引用校验用）',
  `token_count` int NOT NULL DEFAULT 0 COMMENT '字符数粗估（预算与截断用，不是精确 token）',
  `embedded` tinyint(1) NOT NULL DEFAULT 0 COMMENT '向量是否已写入：0否、1是',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_kb_chunk_no`(`document_id` ASC, `chunk_no` ASC) USING BTREE,
  INDEX `idx_kb_chunk_meeting`(`meeting_id` ASC) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '知识库切片表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for chat_session
-- ----------------------------
DROP TABLE IF EXISTS `chat_session`;
CREATE TABLE `chat_session`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '会话ID',
  `user_id` int NOT NULL COMMENT '会话归属用户ID',
  `title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '新对话' COMMENT '会话标题（首次提问前若干字）',
  `scope_meeting_ids` json NOT NULL COMMENT '本次对话限定的会议ID数组，空数组表示不限（权限仍按用户实时判定）',
  `summary` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '滑窗之外历史的滚动摘要',
  `summary_upto_message_id` int NOT NULL DEFAULT 0 COMMENT '摘要已覆盖到的消息ID（水位线）',
  `status` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'ACTIVE' COMMENT '状态：ACTIVE、DELETED',
  `message_count` int NOT NULL DEFAULT 0 COMMENT '消息条数（会话列表直接读，不做聚合）',
  `last_active_time` datetime(6) NULL DEFAULT NULL COMMENT '最后活跃时间',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_chat_session_user`(`user_id` ASC) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '知识问答会话表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for chat_message
-- ----------------------------
DROP TABLE IF EXISTS `chat_message`;
CREATE TABLE `chat_message`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '消息ID',
  `session_id` int NOT NULL COMMENT '所属会话ID',
  `role` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '角色：USER用户、ASSISTANT助手',
  `content` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '消息正文',
  `citations_json` json NULL COMMENT '这一轮用到的引用快照（切片会被重建删除，引用必须留得下来）',
  `tools_json` json NULL COMMENT '工具调用轨迹（页面折叠展示）',
  `moderation_json` json NULL COMMENT '内容审核留痕（命中词与处置动作，不含上下文）',
  `plan_json` json NULL COMMENT '查询规划（改写/意图/建议工具/触发词/开关状态）',
  `stop_reason` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '结束原因：finished、max_rounds、budget_tokens、budget_cost、budget_tools、stalled',
  `error_message` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '本轮失败原因',
  `token_count` int NOT NULL DEFAULT 0 COMMENT '本轮 token 用量',
  `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_chat_message_session`(`session_id` ASC) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '知识问答消息表' ROW_FORMAT = Dynamic;

SET FOREIGN_KEY_CHECKS = 1;
