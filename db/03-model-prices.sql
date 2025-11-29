-- 模型单价（估算，2026-10-23 快照）
--
-- 目的：让新产生的 ai_call_log 能算出成本，而不是全部 unpriced。
-- **这是估算单价，不是账单**：
--   * qwen3.8-max-0902 取阿里云百炼官方价（华北2，2026-10-22 页面）：
--     $1.650 / 百万 input、$4.951 / 百万 output；按 1 USD = 7.1 CNY 折算为
--     11715 / 35152 微元每 1k token（1 微元 = 1e-6 元）。
--   * text-embedding 取 500 微元/1k input（量级参考，输出不计）。
--   * ASR、deepseek（评测裁判）不在这里定价：前者按 GPU/音频时长计，
--     后者只用于离线评测，产品账本里出现也不该混进单轮问答成本。
-- 历史 ai_call_log 不会按新价重估（成本是事实，不是派生值）。
UPDATE ai_model_config
SET input_price_micro_per_1k = 11715,
    output_price_micro_per_1k = 35152
WHERE model_name = 'qwen3.8-max-0902';

UPDATE ai_model_config
SET input_price_micro_per_1k = 500,
    output_price_micro_per_1k = 0
WHERE model_name = 'qwen3.7-text-embedding';
