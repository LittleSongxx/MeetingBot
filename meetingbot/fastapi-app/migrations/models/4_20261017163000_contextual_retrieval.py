"""Contextual Retrieval：`kb_chunk` 加 `embed_text`。

存"真正送去嵌入的文本"（会议/时间/主题上下文 + 原文），与 `text` 分开：
`text` 是引用回显与逐字定位的正文，上下文句写进去会让引用在转写里找不到。
列只由 aerich 执行一次（版本表保证），空库由 `db/01-schema.sql` 直接建出带新列的结构。
"""

from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `kb_chunk`
            ADD COLUMN `embed_text` LONGTEXT NULL
                COMMENT '用于嵌入的文本（上下文+原文）；为空表示直接用 text';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    # 回退丢数据：上下文句是索引时生成的历史产物，重建不出来——只在明确要清空时执行
    return """
        ALTER TABLE `kb_chunk` DROP COLUMN `embed_text`;
    """
