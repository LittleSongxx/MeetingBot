"""Agent runtime：索引期望状态、对话 checkpoint/追踪字段、模型调用 span。

`kb_index_outbox` 是新增表：来源状态变化与 PG 派生索引之间的 outbox。
`chat_message` 增加本轮生命周期与 checkpoint/续传字段。
`ai_call_log` 增加 span_id，并给 trace_id 建索引（按 trace 回放一次问答的调用链）。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS `kb_index_outbox` (
            `id` int NOT NULL AUTO_INCREMENT COMMENT '期望状态ID',
            `meeting_id` int NOT NULL COMMENT '来源所属会议ID',
            `source_type` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'TRANSCRIPT转写 / MINUTES纪要',
            `source_id` int NOT NULL COMMENT '来源ID（转写任务或纪要ID）',
            `desired_state` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'INDEX' COMMENT '期望状态：INDEX可索引 / DELETE应清理',
            `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/PROCESSING/DONE/FAILED',
            `version` int NOT NULL DEFAULT 1 COMMENT '同一来源每次状态变化递增；worker只处理最新版本',
            `attempts` int NOT NULL DEFAULT 0 COMMENT '已尝试次数',
            `last_error` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '最近一次失败原因',
            `available_at` datetime(6) NULL DEFAULT NULL COMMENT '下次可处理时间（退避）',
            `started_at` datetime(6) NULL DEFAULT NULL COMMENT '本次处理开始时间',
            `finished_at` datetime(6) NULL DEFAULT NULL COMMENT '本次处理结束时间',
            `create_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
            `update_time` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
            PRIMARY KEY (`id`) USING BTREE,
            UNIQUE INDEX `uk_kb_outbox_source`(`source_type` ASC, `source_id` ASC) USING BTREE,
            INDEX `idx_kb_outbox_status_available`(`status` ASC, `available_at` ASC) USING BTREE,
            INDEX `idx_kb_outbox_meeting`(`meeting_id` ASC) USING BTREE
        ) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '知识库索引期望状态表（outbox）' ROW_FORMAT = Dynamic;
        ALTER TABLE `ai_call_log`
            ADD COLUMN `span_id` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL
                COMMENT '本次调用在 trace 中的 span ID',
            ADD INDEX `idx_ai_call_trace`(`trace_id` ASC) USING BTREE;
        ALTER TABLE `chat_message`
            ADD COLUMN `run_status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'FINISHED'
                COMMENT '本轮生命周期：RUNNING/FINISHED/FAILED/INTERRUPTED',
            ADD COLUMN `thread_id` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL
                COMMENT 'LangGraph checkpoint thread ID',
            ADD COLUMN `checkpoint_id` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL
                COMMENT '最后一次 LangGraph checkpoint ID',
            ADD COLUMN `trace_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL
                COMMENT '本轮共享的链路追踪ID',
            ADD COLUMN `charged_tokens` int NOT NULL DEFAULT 0
                COMMENT '已记入配额的token数（恢复时幂等）',
            ADD INDEX `idx_chat_message_run_status`(`run_status` ASC) USING BTREE,
            ADD INDEX `idx_chat_message_trace`(`trace_id` ASC) USING BTREE;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `chat_message`
            DROP INDEX `idx_chat_message_trace`,
            DROP INDEX `idx_chat_message_run_status`,
            DROP COLUMN `charged_tokens`,
            DROP COLUMN `trace_id`,
            DROP COLUMN `checkpoint_id`,
            DROP COLUMN `thread_id`,
            DROP COLUMN `run_status`;
        ALTER TABLE `ai_call_log`
            DROP INDEX `idx_ai_call_trace`,
            DROP COLUMN `span_id`;
        DROP TABLE IF EXISTS `kb_index_outbox`;
    """
