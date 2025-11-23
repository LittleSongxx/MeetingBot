"""在线评测与模型路由：online_eval_run、online_eval_case、model_route。

空库由 db/01-schema.sql 直接建出终态；本迁移只服务存量库。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS `model_route` (
            `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
            `route_key` VARCHAR(50) NOT NULL,
            `model_config_id` INT NOT NULL,
            `enabled` TINYINT(1) NOT NULL DEFAULT 1,
            `note` VARCHAR(500) NULL,
            `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
            UNIQUE KEY `uk_model_route_key` (`route_key`)
        ) ENGINE=InnoDB CHARACTER SET=utf8mb4 COMMENT='模型路由';
        CREATE TABLE IF NOT EXISTS `online_eval_run` (
            `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
            `triggered_by` INT NOT NULL,
            `sample_size` INT NOT NULL DEFAULT 0,
            `model_config_id` INT NOT NULL,
            `status` VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
            `started_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            `finished_at` DATETIME(6) NULL,
            `summary_json` JSON NULL,
            `error_message` LONGTEXT NULL,
            `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
            KEY `idx_online_eval_status_time` (`status`, `started_at`)
        ) ENGINE=InnoDB CHARACTER SET=utf8mb4 COMMENT='在线评测运行';
        CREATE TABLE IF NOT EXISTS `online_eval_case` (
            `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
            `run_id` INT NOT NULL,
            `chat_message_id` INT NOT NULL,
            `question` LONGTEXT NOT NULL,
            `answer` LONGTEXT NOT NULL,
            `citations_json` JSON NULL,
            `tools_json` JSON NULL,
            `scores_json` JSON NULL,
            `judge_model` VARCHAR(100) NULL,
            `error_message` LONGTEXT NULL,
            `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            KEY `idx_online_case_run` (`run_id`),
            KEY `idx_online_case_message` (`chat_message_id`)
        ) ENGINE=InnoDB CHARACTER SET=utf8mb4 COMMENT='在线评测单条结果';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS `online_eval_case`;
        DROP TABLE IF EXISTS `online_eval_run`;
        DROP TABLE IF EXISTS `model_route`;
    """
