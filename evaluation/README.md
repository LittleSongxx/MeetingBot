# AIMeeting 评测主线（评测宪法）

一条主线，四条指标轴，三层入口，一个金标锚。目录结构与设计依据见
`DESIGN.md`；可引用的数字与运行账本见 `LEDGER.md`。

## 四条指标轴

| 轴 | 问题 | 仪器 |
|---|---|---|
| 1 转写对 | 转写错多少、说话人归属错多少 | `asr/cer_tool.py`（CER 三口径）；cpCER 待接 MeetEval |
| 2 纪要实 | 条目/整篇有没有转写依据 | `judges/meeting_level.py`、`judges/item_judge.py`、`judges/citations.py` |
| 3 纪要全 | 该记的漏了多少 | `judges/completeness.py`（清单式，清单只看转写） |
| 4 结构准 | 行动项 owner/deadline 对不对 | `judges/actions.py`（AMC-AID 锚点）+ `gold/` 标注流水线 |

可靠性（崩溃/截断/超时率）单列为体验层：`regression/reliability.py`。

## 三层入口 + 金标（Makefile）

```bash
cd evaluation
make smoke   # L0 零调用回归：契约不变量、可定位率、可靠性复算（秒级，每次改动必跑）
make deep    # L1 深度评测：抽样 + 裁判判读（judges/，里程碑时跑，需授权调用）
make gate    # L2 变更门禁：留出集双臂 A/B + 内容盲判（产品改动合并前置）
make gold    # 金标与裁判校准：标注包构建 / 裁判跨族校准（防"无锚点裁判"）
make test    # 评测代码自身单测（190 项，零调用）
```

判定标准（什么算"有依据"）：`ai_meeting/fastapi-app/JUDGMENT_STANDARD.md`
（已冻结，S/D/X/C + D1–D5）。**改它 = 换尺子，所有绝对数重述。**

## 评测宪法（六条硬规则）

1. **主结局在看数据之前写死。** L2 门禁先在 `LEDGER.md` 登记假设、唯一主
   指标、判定阈值、样本量、何为失败，然后才跑。
2. **裁判与被测不同族；单裁判读数只是方向信号。** 进入产品决策必须过
   `make gold` 校准（或双族一致）。换裁判 = 换仪器，历史绝对数全部重述。
3. **功效算不出的差异写"检不出"，禁止写"无效果"。** n=6 只算方向把关。
   配对设计是唯一救命稻草（同批会议跑两臂，逐场算 delta）。
4. **holdout 有账。** dev（test26/dev22）可日常消费；holdout6/extra8 只在
   门禁裁决时打开并在 LEDGER 登记；消费 5–10 次后从 191 场未消费池换血。
5. **评测不 import 产品内部实现**；共享归一化只允许"产品定义、评测委托"
   的单一方向。任何"产品机制与评测端点共用同一机械"的结构在评审时打回。
6. **不报加权总分。** 只报按轴 pass 率、配对 delta 与 CI。一个数字如果
   由同一份数据既校准又报收益，作废。

## 快速上手

```bash
# 环境探针（裁判调用慢时先分三段查：网络/响应头/吐字）
PYTHONPATH=evaluation python -c "from lib import model_client; print(model_client.probe_transport('dashscope'))"

# L0（当前基线数字的复现路径）
ai_meeting/fastapi-app/.venv/bin/python evaluation/regression/run.py --set all

# 单测
cd evaluation && ../ai_meeting/fastapi-app/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

付费/判定类运行（deep/gate）凭据只从环境读，先读各脚本顶部 docstring 的
预注册说明，再申请调用授权。
