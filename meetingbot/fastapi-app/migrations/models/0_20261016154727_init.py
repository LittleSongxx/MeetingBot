from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS `agent_run` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `run_no` VARCHAR(50) NOT NULL UNIQUE,
    `minutes_id` INT NOT NULL,
    `meeting_id` INT NOT NULL,
    `user_id` INT NOT NULL,
    `model_config_id` INT NOT NULL,
    `status` VARCHAR(30) NOT NULL DEFAULT 'RUNNING',
    `stage` VARCHAR(100) NOT NULL DEFAULT '等待执行',
    `progress` INT NOT NULL DEFAULT 0,
    `current_round` INT NOT NULL DEFAULT 0,
    `max_rounds` INT NOT NULL DEFAULT 2,
    `passed` BOOL NOT NULL DEFAULT 0,
    `score` INT NOT NULL DEFAULT 0,
    `issue_count` INT NOT NULL DEFAULT 0,
    `review_json` JSON NOT NULL,
    `revised_json` JSON,
    `applied_by` INT,
    `applied_time` DATETIME(6),
    `reject_reason` VARCHAR(500),
    `final_result` LONGTEXT,
    `error_message` LONGTEXT,
    `started_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `finished_at` DATETIME(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    KEY `idx_agent_run_minutes_b0a0f4` (`minutes_id`),
    KEY `idx_agent_run_meeting_2be6d8` (`meeting_id`),
    KEY `idx_agent_run_user_id_811a87` (`user_id`)
) CHARACTER SET utf8mb4 COMMENT='一次会议纪要自检运行。';
CREATE TABLE IF NOT EXISTS `agent_step` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `run_id` INT NOT NULL,
    `step_no` INT NOT NULL,
    `round_no` INT NOT NULL DEFAULT 0,
    `step_type` VARCHAR(30) NOT NULL,
    `decision_summary` LONGTEXT,
    `observation_summary` LONGTEXT,
    `payload_json` JSON,
    `status` VARCHAR(30) NOT NULL DEFAULT 'RUNNING',
    `elapsed_ms` BIGINT NOT NULL DEFAULT 0,
    `error_message` LONGTEXT,
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `finish_time` DATETIME(6),
    UNIQUE KEY `uid_agent_step_run_id_8a6679` (`run_id`, `step_no`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `ai_call_log` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `request_id` VARCHAR(50) NOT NULL UNIQUE,
    `call_type` VARCHAR(50) NOT NULL,
    `biz_id` INT NOT NULL,
    `model_config_id` INT NOT NULL,
    `model_name` VARCHAR(100) NOT NULL,
    `request_size` BIGINT NOT NULL DEFAULT 0,
    `response_size` BIGINT NOT NULL DEFAULT 0,
    `status` VARCHAR(30) NOT NULL,
    `elapsed_ms` BIGINT NOT NULL DEFAULT 0,
    `usage_json` JSON,
    `error_message` LONGTEXT,
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    KEY `idx_ai_call_log_create__01784a` (`create_time`, `model_config_id`),
    KEY `idx_ai_call_log_biz_id_76bb48` (`biz_id`, `call_type`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `ai_model_config` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `name` VARCHAR(100) NOT NULL,
    `model_type` VARCHAR(30) NOT NULL DEFAULT 'TRANSCRIPTION',
    `provider` VARCHAR(50) NOT NULL DEFAULT 'OPENAI_COMPATIBLE',
    `base_url` VARCHAR(500) NOT NULL,
    `api_key` VARCHAR(1000) NOT NULL,
    `model_name` VARCHAR(100) NOT NULL,
    `timeout_seconds` INT NOT NULL DEFAULT 600,
    `enabled` BOOL NOT NULL DEFAULT 0,
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `department` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `name` VARCHAR(100) NOT NULL,
    `description` VARCHAR(500),
    `status` VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `meeting` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `meeting_no` VARCHAR(50) NOT NULL UNIQUE,
    `title` VARCHAR(255) NOT NULL,
    `start_time` DATETIME(6) NOT NULL,
    `end_time` DATETIME(6) NOT NULL,
    `location` VARCHAR(255),
    `agenda` LONGTEXT,
    `creator_id` INT NOT NULL,
    `host_id` INT NOT NULL,
    `status` VARCHAR(30) NOT NULL DEFAULT 'SCHEDULED',
    `cancel_reason` VARCHAR(500),
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    KEY `idx_meeting_creator_f5cc35` (`creator_id`),
    KEY `idx_meeting_host_id_b8eb6f` (`host_id`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `meeting_material` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `meeting_id` INT NOT NULL,
    `file_name` VARCHAR(255) NOT NULL,
    `storage_name` VARCHAR(255) NOT NULL UNIQUE,
    `file_type` VARCHAR(30) NOT NULL,
    `content_type` VARCHAR(150),
    `file_ext` VARCHAR(30),
    `file_size` BIGINT NOT NULL DEFAULT 0,
    `file_hash` VARCHAR(64) NOT NULL,
    `uploader_id` INT NOT NULL,
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY `uid_meeting_mat_meeting_eee702` (`meeting_id`, `file_hash`),
    KEY `idx_meeting_mat_meeting_ec5284` (`meeting_id`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `meeting_minutes` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `meeting_id` INT NOT NULL,
    `source_task_id` INT NOT NULL,
    `model_config_id` INT NOT NULL,
    `status` VARCHAR(30) NOT NULL DEFAULT 'GENERATING',
    `stage` VARCHAR(100) NOT NULL DEFAULT '等待生成',
    `progress` INT NOT NULL DEFAULT 0,
    `summary` LONGTEXT,
    `topics_json` JSON NOT NULL,
    `viewpoints_json` JSON NOT NULL,
    `decisions_json` JSON NOT NULL,
    `pending_items_json` JSON NOT NULL,
    `risks_json` JSON NOT NULL,
    `created_by` INT NOT NULL,
    `confirmed_by` INT,
    `confirmed_time` DATETIME(6),
    `error_message` LONGTEXT,
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY `uid_meeting_min_source__2d3a08` (`source_task_id`),
    KEY `idx_meeting_min_meeting_b6ef38` (`meeting_id`),
    KEY `idx_meeting_min_created_3b08dd` (`created_by`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `meeting_participant` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `meeting_id` INT NOT NULL,
    `user_id` INT NOT NULL,
    `participant_role` VARCHAR(30) NOT NULL DEFAULT 'PARTICIPANT',
    `response_status` VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY `uid_meeting_par_meeting_22a205` (`meeting_id`, `user_id`),
    KEY `idx_meeting_par_user_id_76aad7` (`user_id`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `prompt_template` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `code` VARCHAR(100) NOT NULL UNIQUE,
    `name` VARCHAR(100) NOT NULL,
    `scene_type` VARCHAR(50) NOT NULL DEFAULT 'MEETING_MINUTES',
    `system_prompt` LONGTEXT NOT NULL,
    `user_prompt` LONGTEXT NOT NULL,
    `enabled` BOOL NOT NULL DEFAULT 1,
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `transcript_segment` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `task_id` INT NOT NULL,
    `meeting_id` INT NOT NULL,
    `segment_no` INT NOT NULL,
    `start_ms` BIGINT NOT NULL DEFAULT 0,
    `end_ms` BIGINT NOT NULL DEFAULT 0,
    `speaker_label` VARCHAR(50),
    `speaker_user_id` INT,
    `original_text` LONGTEXT NOT NULL,
    `content` LONGTEXT NOT NULL,
    `revised_by` INT,
    `revised_time` DATETIME(6),
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY `uid_transcript__task_id_434d40` (`task_id`, `segment_no`),
    KEY `idx_transcript__meeting_45e784` (`meeting_id`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `transcription_task` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `meeting_id` INT NOT NULL,
    `material_id` INT NOT NULL UNIQUE,
    `model_config_id` INT NOT NULL,
    `initiator_id` INT NOT NULL,
    `language` VARCHAR(20) NOT NULL DEFAULT 'zh',
    `status` VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    `stage` VARCHAR(100) NOT NULL DEFAULT '等待执行',
    `progress` INT NOT NULL DEFAULT 0,
    `retry_count` INT NOT NULL DEFAULT 0,
    `max_retry` INT NOT NULL DEFAULT 3,
    `total_duration_ms` BIGINT NOT NULL DEFAULT 0,
    `full_text` LONGTEXT,
    `error_message` LONGTEXT,
    `started_at` DATETIME(6),
    `finished_at` DATETIME(6),
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    KEY `idx_transcripti_initiat_a1934c` (`initiator_id`),
    KEY `idx_transcripti_meeting_53bb1f` (`meeting_id`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `user` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `username` VARCHAR(100) NOT NULL UNIQUE,
    `password` VARCHAR(255) NOT NULL,
    `name` VARCHAR(100) NOT NULL,
    `avatar` VARCHAR(500),
    `role` VARCHAR(30) NOT NULL DEFAULT 'EMPLOYEE',
    `phone` VARCHAR(30),
    `email` VARCHAR(100),
    `department_id` INT,
    `position` VARCHAR(100),
    `status` VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `kb_index_task` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `meeting_id` INT NOT NULL,
    `source_type` VARCHAR(30) NOT NULL,
    `source_id` INT NOT NULL,
    `model_config_id` INT NOT NULL,
    `initiator_id` INT NOT NULL,
    `status` VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    `stage` VARCHAR(100) NOT NULL DEFAULT '等待执行',
    `progress` INT NOT NULL DEFAULT 0,
    `retry_count` INT NOT NULL DEFAULT 0,
    `max_retry` INT NOT NULL DEFAULT 3,
    `document_id` INT,
    `chunk_count` INT NOT NULL DEFAULT 0,
    `embedded_count` INT NOT NULL DEFAULT 0,
    `error_message` LONGTEXT,
    `started_at` DATETIME(6),
    `finished_at` DATETIME(6),
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY `uk_kb_index_source` (`source_type`, `source_id`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `kb_document` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `meeting_id` INT NOT NULL,
    `source_type` VARCHAR(30) NOT NULL,
    `source_id` INT NOT NULL,
    `task_id` INT NOT NULL,
    `authority` VARCHAR(30) NOT NULL DEFAULT 'RAW',
    `content_hash` VARCHAR(64) NOT NULL,
    `chunk_count` INT NOT NULL DEFAULT 0,
    `embed_model` VARCHAR(100) NOT NULL,
    `embed_dim` INT NOT NULL DEFAULT 0,
    `status` VARCHAR(30) NOT NULL DEFAULT 'SUCCEEDED',
    `error_message` LONGTEXT,
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `kb_chunk` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `document_id` INT NOT NULL,
    `meeting_id` INT NOT NULL,
    `chunk_no` INT NOT NULL,
    `chunk_type` VARCHAR(30) NOT NULL,
    `text` LONGTEXT NOT NULL,
    `speaker` VARCHAR(50),
    `start_ms` BIGINT,
    `end_ms` BIGINT,
    `field` VARCHAR(30),
    `item_index` INT,
    `origin` VARCHAR(30),
    `evidence_json` JSON,
    `token_count` INT NOT NULL DEFAULT 0,
    `embedded` BOOL NOT NULL DEFAULT 0,
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY `uk_kb_chunk_no` (`document_id`, `chunk_no`),
    KEY `idx_kb_chunk_meeting` (`meeting_id`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `aerich` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `version` VARCHAR(255) NOT NULL,
    `app` VARCHAR(100) NOT NULL,
    `content` JSON NOT NULL
) CHARACTER SET utf8mb4;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        """


MODELS_STATE = (
    "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"
)
