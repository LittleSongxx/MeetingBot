-- 第 24 轮：QA 引用限制为 1 条最直接支持答案的片段（当前 QA 评测集均为单证据题）
-- 已有库执行一次；全新库已包含在 02-seed.sql
UPDATE prompt_template
SET user_prompt = REPLACE(
    user_prompt,
    'citations 最多给 2 条，只选最直接支持答案的片段。',
    'citations 最多给 1 条，只选最直接支持答案的片段。'
)
WHERE code = 'MINUTES_QA'
  AND user_prompt LIKE '%citations 最多给 2 条%';

UPDATE prompt_template
SET user_prompt = CONCAT(
    user_prompt,
    '\n\ncitations 最多给 1 条，只选最直接支持答案的片段。'
)
WHERE code = 'MINUTES_QA'
  AND user_prompt NOT LIKE '%citations 最多给 1 条%';

-- 回滚（如需）：
-- UPDATE prompt_template SET user_prompt = REPLACE(user_prompt, '\n\ncitations 最多给 1 条，只选最直接支持答案的片段。', '') WHERE code='MINUTES_QA';
