# 独立行动项抽取步（qwen）vs AI-draft gold（deepseek）：10 场

> 预注册 §54 的后续：比较“独立抽取步”与产品纪要 pending_items 相对 gold 的召回/精度。
> gold 为 AI-draft（human_checked=false），跨族只作方向信号。

| 量 | 值 |
|---|---|
| gold items | 29 |
| 抽取 items | 14 |
| matched | 6 |
| precision | 0.429 |
| recall | 0.207 |
| **F1（目标 ≥0.70）** | **0.279** |
| 判定 | FAIL |

## 与现有产品 pending_items 对比

| 臂 | gold | pred | matched | P | R | F1 |
|---|---:|---:|---:|---:|---:|---:|
| 产品 pending_items（同 10 场） | 29 | 17 | 4 | 0.235 | 0.138 | 0.174 |
| 独立抽取步（qwen） | 29 | 14 | 6 | 0.429 | 0.207 | 0.279 |

