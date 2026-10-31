-- 第 24/26 轮：QA 引用按问题复杂度动态限制（单点 1 条、多点 2~3 条）
-- 已有库执行一次；全新库已包含在 02-seed.sql
UPDATE prompt_template
SET user_prompt = REPLACE(
    REPLACE(
        REPLACE(
            user_prompt,
            'citations 最多给 2 条，只选最直接支持答案的片段。',
            'citations：单点问题给 1 条、多点问题给 2~3 条，只选最直接支持答案的片段。'
        ),
        'citations 最多给 1 条，只选最直接支持答案的片段。',
        'citations：单点问题给 1 条、多点问题给 2~3 条，只选最直接支持答案的片段。'
    ),
    'citations 最多给 3 条；单点问题只给 1 条，多点问题给对应 2~3 条。',
    'citations：单点问题给 1 条、多点问题给 2~3 条，只选最直接支持答案的片段。'
)
WHERE code = 'MINUTES_QA';

-- 回滚（如需）：恢复为“最多 1 条”或直接使用 02-seed.sql 的模板。
