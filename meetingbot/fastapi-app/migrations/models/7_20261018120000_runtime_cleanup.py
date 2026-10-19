"""运行时清理：移除 write-only 的 `chat_message.last_event_id`。

恢复入口只有一条：Redis Stream 回放 + LangGraph checkpoint。这个列在库里
从来没有被读取过，留着会让读者以为还存在第二条回放路径。

旧库可能没有这一列（新库由迁移链直接建出终态结构），所以用
information_schema 判断后再 ALTER；直接 DROP COLUMN 会在新库上报 1091。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        SET @has_last_event_id := (
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_message'
              AND COLUMN_NAME = 'last_event_id'
        );
        SET @drop_last_event_id := IF(
            @has_last_event_id > 0,
            'ALTER TABLE `chat_message` DROP COLUMN `last_event_id`',
            'SELECT 1'
        );
        PREPARE stmt FROM @drop_last_event_id;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    # 回退不恢复 write-only 列；需要时按 6 号迁移的列定义手工加回
    return "SELECT 1"
