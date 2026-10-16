"""问答的查询规划留痕：`chat_message` 加 `plan_json`。

存这一份的理由与引用快照同款——**结论要能归因**：一轮答错时，是"改写把问题带偏了"
还是"检索没找到"，只看最终答案分不出来。列只由 aerich 执行一次（版本表保证），
因此不写 IF NOT EXISTS；空库由 `db/01-schema.sql` 直接建出带新列的结构。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `chat_message`
            ADD COLUMN `plan_json` JSON NULL
                COMMENT '查询规划（改写/意图/建议工具/触发词/开关状态）';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    # 回退丢数据：规划留痕是历史事实，重建不出来——只在明确要清空时执行
    return """
        ALTER TABLE `chat_message` DROP COLUMN `plan_json`;
    """
