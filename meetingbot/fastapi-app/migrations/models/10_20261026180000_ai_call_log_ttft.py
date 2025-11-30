"""首 token 延迟：ai_call_log 增加 ttft_ms。

按单次调用记录首 token 延迟（毫秒），把"总耗时"与"首字快慢"分开：
非流式调用、或流式但未测到首 token 时写 0（默认值即 0，存量行无需回填）。
空库由 db/01-schema.sql 直接建出终态；本迁移只服务存量库（aerich 版本表保证只跑一次）。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `ai_call_log`
            ADD COLUMN `ttft_ms` BIGINT NOT NULL DEFAULT 0
                COMMENT '首 token 延迟毫秒；非流式或未测到为 0';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `ai_call_log`
            DROP COLUMN `ttft_ms`;
    """
