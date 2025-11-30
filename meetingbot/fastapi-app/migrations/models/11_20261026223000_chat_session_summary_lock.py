"""跨进程摘要租约：chat_session 增加 summary_lock_until。

后台滚动摘要需要避免多副本同时压同一批消息；租约到期后其他进程可抢占，
进程崩溃不会永久锁死。空库由 db/01-schema.sql 建终态，本迁移只服务存量库。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `chat_session`
            ADD COLUMN `summary_lock_until` datetime(6) NULL DEFAULT NULL
                COMMENT '跨进程摘要租约到期时间；空/已过期可抢占';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `chat_session`
            DROP COLUMN `summary_lock_until`;
    """
