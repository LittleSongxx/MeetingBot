"""知识问答的会话与消息两张表。

与基线迁移同款用 `CREATE TABLE IF NOT EXISTS`：空库由 initdb 的 `db/01-schema.sql`
建表，存量库跑本迁移补齐——两种环境走同一条升级路径，重复执行不报错。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS `chat_session` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `user_id` INT NOT NULL,
    `title` VARCHAR(255) NOT NULL DEFAULT '新对话',
    `scope_meeting_ids` JSON NOT NULL,
    `summary` LONGTEXT,
    `summary_upto_message_id` INT NOT NULL DEFAULT 0,
    `status` VARCHAR(30) NOT NULL DEFAULT 'ACTIVE',
    `message_count` INT NOT NULL DEFAULT 0,
    `last_active_time` DATETIME(6),
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    KEY `idx_chat_session_user` (`user_id`)
) CHARACTER SET utf8mb4 COMMENT='知识问答会话表';
CREATE TABLE IF NOT EXISTS `chat_message` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `session_id` INT NOT NULL,
    `role` VARCHAR(20) NOT NULL,
    `content` LONGTEXT NOT NULL,
    `citations_json` JSON,
    `tools_json` JSON,
    `moderation_json` JSON,
    `stop_reason` VARCHAR(30),
    `error_message` LONGTEXT,
    `token_count` INT NOT NULL DEFAULT 0,
    `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    KEY `idx_chat_message_session` (`session_id`)
) CHARACTER SET utf8mb4 COMMENT='知识问答消息表';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    # 回退只删这两张表：它们没有任何外键引用其他表，也不被其他表引用。
    # 消息先删——会话表是它存在的前提。
    return """
        DROP TABLE IF EXISTS `chat_message`;
        DROP TABLE IF EXISTS `chat_session`;
    """
