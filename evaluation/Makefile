# MeetingBot 评测入口（四层，见 README.md 的评测宪法）
PY ?= ../meetingbot/fastapi-app/.venv/bin/python
PY_RAGAS ?= .venv-ragas/bin/python
SMOKE_ARGS ?=

.PHONY: smoke deep gate gold rag test trajectory recall quality-gate ragas ragas-pilot ragas-aggregate

# L0 零调用回归：契约不变量、证据可定位率、可靠性复算。每次改动必跑。
smoke:
	$(PY) regression/run.py --set all --output-dir results $(SMOKE_ARGS)

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

# 轴5 检索与回答（四个指标逐项报；--ask/--judge 需授权，--build 零调用）
rag:
	$(PY) judges/rag.py --build
	@echo "判定（需授权）: $(PY) judges/rag.py --judge --authorized-llm-calls N"
	@echo "抓取真实问答（需授权 + JWT）: $(PY) judges/rag.py --ask --token $$TOKEN --authorized-llm-calls N"
	@echo "零调用不变量（引用可定位 / 不越权）随 make smoke 一起跑"

# 金标与裁判校准（防"无锚点裁判"的唯一手段）
gold:
	@echo "  $(PY) gold/annotation.py   # 构建 120 条人工标注包（CSV+指南，可直接发众包）"
	@echo "  $(PY) gold/judge_calibration.py --authorized-llm-calls N  # 裁判跨族校准"

# Agent 轨迹评测（零模型调用）：工具集合/首轮 strict/必引/pass^k。
# 无产物时只做金标校验并如实报"无样本"（安全，可随时跑）。
# 有产物时（pass^k 需同一配置重复抓 k 次，按 trial 次序传 k 份产物）：
#   $(PY) regression/trajectory.py --artifact results/t1.json --artifact results/t2.json \
#       --artifact results/t3.json --k 3 --output results/trajectory_report.json
# 裁判抽检回填表（零调用）：
#   $(PY) gold/judge_spotcheck.py --artifact results/chat_answers.json --sample 20 \
#       --output results/judge_spotcheck_20.csv
# 回填后算 Cohen's kappa：
#   $(PY) gold/judge_spotcheck.py --score results/judge_spotcheck_20.csv \
#       --output results/judge_spotcheck_20.score.json
trajectory:
	$(PY) regression/trajectory.py --gold gold/trajectory_27.json
	@echo "有产物时（pass^k 需同一配置重复抓 k 次，按 trial 次序传 k 份产物）："
	@echo "  $(PY) regression/trajectory.py --artifact results/t1.json --artifact results/t2.json --artifact results/t3.json --k 3 --output results/trajectory_report.json"
	@echo "裁判抽检回填表（零调用）: $(PY) gold/judge_spotcheck.py --artifact results/chat_answers.json --sample 20 --output results/judge_spotcheck_20.csv"
	@echo "回填后算 Cohen's kappa: $(PY) gold/judge_spotcheck.py --score results/judge_spotcheck_20.csv --output results/judge_spotcheck_20.score.json"

# 可检索事实召回 recall-v2 报告（零模型调用；仪器口径见 reports/2026-10-22_recall_instrument_fix.md）
recall:
	$(PY) analysis/recall_report.py

# RAG 全链路质量闸（零模型调用；旧口径 + recall-v2 双行 + CI + 产物校验和）
quality-gate:
	$(PY) quality_gate.py

# 评测代码自身单测（零调用）
test:
	cd . && $(PY) -m unittest discover -s tests -p 'test_*.py'

# RAGAS 官方口径重测（r1；独立 venv，预注册见 LEDGER.md §RAGAS-1）
ragas-pilot:
	$(PY_RAGAS) judges/ragas_official.py --limit 20 --run-label pilot --out results/ragas_official_pilot.json --authorized-llm-calls 400 --concurrency 12

ragas:
	$(PY_RAGAS) judges/ragas_official.py --run-label run1 --out results/ragas_official_run1.json --authorized-llm-calls 1200 --concurrency 12
	$(PY_RAGAS) judges/ragas_official.py --run-label run2 --out results/ragas_official_run2.json --authorized-llm-calls 1200 --concurrency 12
	$(PY_RAGAS) judges/ragas_official.py --run-label run3 --out results/ragas_official_run3.json --authorized-llm-calls 1200 --concurrency 12

ragas-aggregate:
	$(PY_RAGAS) judges/ragas_aggregate.py --runs results/ragas_official_run1.json results/ragas_official_run2.json results/ragas_official_run3.json --out results/ragas_official_aggregate.json --report reports/2026-10-27_ragas_official.md --baseline baselines/ragas_official_baseline.json
