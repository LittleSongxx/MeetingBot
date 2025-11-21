"""录音墙钟时间：`transcription_task` 加 `recorded_at`。

一场会可能有多段录音（上午一场、下午一场），而 `create_time` 是上传时间、
`meeting.start_time` 是会议名义时间，都不能回答"下午那场"。这一列是检索侧
判断"上午/下午"的唯一依据；为空表示未标注，不拿上传时间冒充。
列只由 aerich 执行一次（版本表保证），空库由 `db/01-schema.sql` 直接建出。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `transcription_task`
            ADD COLUMN `recorded_at` DATETIME(6) NULL
                COMMENT '录音实际开始时间（墙钟）；用于判断上午/下午；空=未标注';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `transcription_task` DROP COLUMN `recorded_at`;
    """
