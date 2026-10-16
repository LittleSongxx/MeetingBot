"""成本核算：单价列 + 每次调用的成本。

`ai_model_config` 加两个单价列（每 1000 token 的微元数），`ai_call_log` 加
`cost_micro`（这次调用花了多少）与 `trace_id`（同一次业务操作的多次调用共享）。

一次 ALTER 同时加两列而不是两条语句：MySQL 8 对"加列"是 INSTANT 操作，
合并成一条能少一次表重建的窗口。迁移只由 aerich 执行一次（版本表保证），
因此这里不写 IF NOT EXISTS（MySQL 的 ADD COLUMN 也不支持它）；
空库由 `db/01-schema.sql` 直接建出带新列的结构，两条路径殊途同归。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `ai_model_config`
            ADD COLUMN `input_price_micro_per_1k` INT NOT NULL DEFAULT 0
                COMMENT '输入单价：每1000 token 的微元数（1e-6 元），0 表示未定价',
            ADD COLUMN `output_price_micro_per_1k` INT NOT NULL DEFAULT 0
                COMMENT '输出单价：每1000 token 的微元数（1e-6 元），0 表示未定价';
        ALTER TABLE `ai_call_log`
            ADD COLUMN `cost_micro` BIGINT NOT NULL DEFAULT 0
                COMMENT '本次调用成本（微元，1e-6 元），按调用时的单价与真实用量算出',
            ADD COLUMN `trace_id` VARCHAR(64) NULL DEFAULT NULL
                COMMENT '链路追踪ID（同一次业务操作的多次调用共享）';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    # 回退丢数据（成本与单价都是历史事实，重建不出来），所以只在明确要清空时执行
    return """
        ALTER TABLE `ai_model_config`
            DROP COLUMN `input_price_micro_per_1k`,
            DROP COLUMN `output_price_micro_per_1k`;
        ALTER TABLE `ai_call_log`
            DROP COLUMN `cost_micro`,
            DROP COLUMN `trace_id`;
    """
