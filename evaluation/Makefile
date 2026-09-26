# AIMeeting 评测入口（四层，见 README.md 的评测宪法）
PY ?= ../ai_meeting/fastapi-app/.venv/bin/python

.PHONY: smoke deep gate gold test

# L0 零调用回归：契约不变量、证据可定位率、可靠性复算。每次改动必跑。
smoke:
	$(PY) regression/run.py --set all --output-dir results

# L1 深度评测（需授权与凭据，先读脚本顶部预注册说明再跑）：
#   judges/meeting_level.py    会议级忠实度（HHEM 同单位）
#   judges/item_judge.py       条目级四分类
#   judges/citations.py        引用级（ALCE 式）
#   judges/completeness.py     关键点完整性
# asr 轴零调用：asr/cer_tool.py（重放已有转写文本）；cpCER 三件套：
#   .venv/bin/python asr/cpcor.py --all-first-test   # 主条件 3 场
#   .venv/bin/python asr/cpcor.py --task <task.json> # 单场/变体
deep:
	@echo "L1 逐仪器运行（各脚本支持 --authorized-llm-calls 预算闸）："
	@echo "  $(PY) judges/meeting_level.py --authorized-llm-calls N"
	@echo "  $(PY) judges/completeness.py --authorized-llm-calls N"
	@echo "  $(PY) judges/citations.py --build && $(PY) judges/citations.py --judge --authorized-llm-calls N"

# L2 变更门禁：留出集双臂 A/B + 内容盲判（先在 LEDGER.md 预注册再跑）
gate:
	@echo "先在 LEDGER.md 写预注册（假设/主指标/阈值/样本量/何为失败），然后："
	@echo "  （机制臂 holdout_batch/holdout_arm 已随默认关机制删除——2026-10-06；L2 门禁重启时按新机制重写）"
	@echo "  $(PY) gate/content_judgment.py --authorized-llm-calls N"

# 金标与裁判校准（防"无锚点裁判"的唯一手段）
gold:
	@echo "  $(PY) gold/annotation.py   # 构建 120 条人工标注包（CSV+指南，可直接发众包）"
	@echo "  $(PY) gold/judge_calibration.py --authorized-llm-calls N  # 裁判跨族校准"

# 评测代码自身单测（零调用）
test:
	cd . && $(PY) -m unittest discover -s tests -p 'test_*.py'
