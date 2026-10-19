"""会议系列与长期记忆：系列、记忆、待办、事件、会议归属与会话系列范围。

新增表：meeting_series、meeting_memory、action_item、action_item_event。
增量列：meeting.series_id、chat_session.scope_series_ids。
空库由 db/01-schema.sql 直接建出终态；本迁移只服务存量库（aerich 版本表保证只跑一次）。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS `meeting_series` (
            `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
            `name` VARCHAR(255) NOT NULL,
            `description` LONGTEXT NULL,
            `cadence` VARCHAR(30) NOT NULL DEFAULT 'IRREGULAR',
            `owner_id` INT NOT NULL,
            `status` VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
            `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
            KEY `idx_series_owner` (`owner_id`),
            KEY `idx_series_status` (`status`)
        ) ENGINE=InnoDB CHARACTER SET=utf8mb4 COMMENT='会议系列';
        CREATE TABLE IF NOT EXISTS `meeting_memory` (
            `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
            `scope_type` VARCHAR(20) NOT NULL,
            `scope_id` INT NOT NULL,
            `kind` VARCHAR(20) NOT NULL,
            `content` LONGTEXT NOT NULL,
            `normalized` LONGTEXT NULL,
            `source_type` VARCHAR(20) NOT NULL DEFAULT 'MANUAL',
            `source_id` INT NULL,
            `source_meeting_id` INT NULL,
            `evidence_json` JSON NULL,
            `status` VARCHAR(20) NOT NULL DEFAULT 'PROPOSED',
            `supersedes_id` INT NULL,
            `valid_from` DATETIME(6) NULL,
            `valid_to` DATETIME(6) NULL,
            `created_by` INT NOT NULL,
            `confirmed_by` INT NULL,
            `confirmed_time` DATETIME(6) NULL,
            `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
            KEY `idx_memory_scope_status` (`scope_type`, `scope_id`, `status`),
            KEY `idx_memory_kind` (`kind`),
            KEY `idx_memory_source` (`source_type`, `source_id`),
            KEY `idx_memory_supersedes` (`supersedes_id`)
        ) ENGINE=InnoDB CHARACTER SET=utf8mb4 COMMENT='长期记忆';
        CREATE TABLE IF NOT EXISTS `action_item` (
            `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
            `series_id` INT NULL,
            `meeting_id` INT NOT NULL,
            `minutes_id` INT NULL,
            `source_field` VARCHAR(30) NULL,
            `source_index` INT NULL,
            `content` LONGTEXT NOT NULL,
            `owner_user_id` INT NULL,
            `owner_name` VARCHAR(100) NULL,
            `deadline` DATETIME(6) NULL,
            `deadline_text` VARCHAR(200) NULL,
            `status` VARCHAR(20) NOT NULL DEFAULT 'OPEN',
            `evidence_json` JSON NULL,
            `dedupe_key` VARCHAR(64) NULL,
            `last_seen_meeting_id` INT NULL,
            `created_by` INT NOT NULL,
            `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
            KEY `idx_action_series_status` (`series_id`, `status`),
            KEY `idx_action_meeting` (`meeting_id`),
            KEY `idx_action_owner` (`owner_user_id`),
            KEY `idx_action_dedupe` (`dedupe_key`)
        ) ENGINE=InnoDB CHARACTER SET=utf8mb4 COMMENT='会议待办';
        CREATE TABLE IF NOT EXISTS `action_item_event` (
            `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
            `action_item_id` INT NOT NULL,
            `event_type` VARCHAR(30) NOT NULL,
            `from_value` VARCHAR(200) NULL,
            `to_value` VARCHAR(200) NULL,
            `note` LONGTEXT NULL,
            `actor_id` INT NOT NULL,
            `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            KEY `idx_action_event_item_time` (`action_item_id`, `create_time`)
        ) ENGINE=InnoDB CHARACTER SET=utf8mb4 COMMENT='待办变更事件';
        ALTER TABLE `meeting`
            ADD COLUMN `series_id` INT NULL DEFAULT NULL COMMENT '所属会议系列ID',
            ADD KEY `idx_meeting_series` (`series_id`);
        ALTER TABLE `chat_session`
            ADD COLUMN `scope_series_ids` JSON NULL COMMENT '按系列限定对话范围';
        UPDATE `chat_session` SET `scope_series_ids` = JSON_ARRAY() WHERE `scope_series_ids` IS NULL;
        ALTER TABLE `chat_session`
            MODIFY COLUMN `scope_series_ids` JSON NOT NULL COMMENT '按系列限定对话范围';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `chat_session` DROP COLUMN `scope_series_ids`;
        ALTER TABLE `meeting` DROP COLUMN `series_id`;
        DROP TABLE IF EXISTS `action_item_event`;
        DROP TABLE IF EXISTS `action_item`;
        DROP TABLE IF EXISTS `meeting_memory`;
        DROP TABLE IF EXISTS `meeting_series`;
    """
