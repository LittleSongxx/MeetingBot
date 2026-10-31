-- 第 24/26 轮：QA 引用按问题复杂度动态限制（单点 1 条、多点 2~3 条），并要求每句 claim 有引用支持
-- 已有库执行一次；全新库已包含在 02-seed.sql
UPDATE prompt_template
SET user_prompt = CONCAT(
    user_prompt,
    '\n\n回答中每一句 claim 都必须能被 citations 直接支持；无法支持的内容不要写，宁可拒答。'
)
WHERE code = 'MINUTES_QA'
  AND user_prompt NOT LIKE '%每一句 claim%';

-- 回滚（如需）：去掉追加的 claim 约束。
