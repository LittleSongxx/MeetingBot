# RAGAS 官方口径重测报告（r1 仪器）

> 生成时间：2026-10-28T07:45:13.499141+00:00
> 仪器：ragas `0.4.3`；judge `deepseek-flash`（temp=0）；embedding `qwen3.7-text-embedding`（dim=1024）；prompt 语言 `en-official`
> 数据：`evaluation/results/hybrid1_packet.json` sha256 `ac068833159b78d234afe1e8e70f232c9644bbfdc8869f2e62770e48736fd803`；reference `evaluation/gold/qa_ref_answers.json` sha256 `60a0c41f46edf108c51f5dde8e7ddec0c713239fa45b05031da3827d9ef52c5c`（human_checked=False）
> 运行：N=3；100 cases/run；标签 `r1`（**禁止与旧自研口径 71.83/90.02/88.34/99.00 相减**）
> reference 校验：167/167 句与 gold facts 逐字一致（脚本 `gold/build_qa_ref_answers.py`）；人工抽检 30 条 CSV 待填写（`gold/qa_ref_spotcheck_30.csv`），`human_checked=false` 如实保留。

## 1. 四指标（case 级均值；3 次运行）

| 指标 | run1 | run2 | run3 | 3 次均值 | 极差 | SD | 逐题 bootstrap 95% CI | 有效 n / null |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| faithfulness | 0.8814 | 0.9095 | 0.8962 | **0.8957** | 0.0280 | 0.0140 | [0.8526, 0.9344] | 99 / 3 |
| answer_relevancy | 0.9146 | 0.9228 | 0.9143 | **0.9172** | 0.0085 | 0.0048 | [0.8916, 0.9399] | 100 / 0 |
| context_precision | 0.6927 | 0.6927 | 0.6981 | **0.6945** | 0.0055 | 0.0032 | [0.6058, 0.7766] | 99 / 3 |
| context_recall | 0.9057 | 0.9057 | 0.9057 | **0.9057** | 0.0000 | 0.0000 | [0.8401, 0.9562] | 99 / 3 |

## 2. 分层读数（3 次运行合并）

| 数据集 | n | faithfulness | answer_relevancy | context_precision | context_recall |
|---|---:|---:|---:|---:|---:|
| cross30 | 30 | 0.7994 | 0.8537 | 0.2606 | 0.7556 |
| holdoutA | 20 | 0.9342 | 0.9543 | 0.9722 | 1.0000 |
| holdoutB | 15 | 0.9500 | 0.9602 | 0.8331 | 0.9333 |
| holdoutD | 20 | 0.9127 | 0.9252 | 0.8538 | 0.9474 |
| newmeeting | 15 | 0.9611 | 0.9413 | 0.8515 | 1.0000 |

## 3. 口径与边界（必须随数字一起引用）

- 四个分母不同：context_precision = 被判有用的切片数（AP 公式）；context_recall = reference 句子数；faithfulness = judge 拆出的断言数；answer_relevancy = case 数。
- reference 由 `expected_facts` 逐条转成陈述句（1 fact = 1 sentence），`human_checked=false`；它是 AI-draft reference，不是人工金标。
- 单裁判（deepseek-flash）+ 未做人工校准；`answer_relevancy` 依赖 embedding 模型，中文场景官方 issue #1829 提示可能偏低。
- 本报告是**第二台仪器**（r1）的读数，只与同仪器前后对比；与旧自研指标不可相减。

## 4. 每指标 3 次运行明细

```json
{
  "faithfulness": {
    "runs": [
      {
        "file": "results/hybrid1_ragas_run1.json",
        "mean": 0.8814145730812397,
        "n_numeric": 99,
        "n_null": 1
      },
      {
        "file": "results/hybrid1_ragas_run2.json",
        "mean": 0.9094569011235679,
        "n_numeric": 99,
        "n_null": 1
      },
      {
        "file": "results/hybrid1_ragas_run3.json",
        "mean": 0.8962111780739233,
        "n_numeric": 99,
        "n_null": 1
      }
    ],
    "mean": 0.8956942174262436,
    "range": 0.028042328042328202,
    "sd": 0.014028309831972762,
    "bootstrap_ci": [
      0.8525546469990917,
      0.9344423066645288
    ],
    "n_cases": 99,
    "n_null_total": 3
  },
  "answer_relevancy": {
    "runs": [
      {
        "file": "results/hybrid1_ragas_run1.json",
        "mean": 0.9145929985586398,
        "n_numeric": 100,
        "n_null": 0
      },
      {
        "file": "results/hybrid1_ragas_run2.json",
        "mean": 0.9227869220129021,
        "n_numeric": 100,
        "n_null": 0
      },
      {
        "file": "results/hybrid1_ragas_run3.json",
        "mean": 0.9143337927523822,
        "n_numeric": 100,
        "n_null": 0
      }
    ],
    "mean": 0.917237904441308,
    "range": 0.008453129260519887,
    "sd": 0.004807337508423361,
    "bootstrap_ci": [
      0.891586997199196,
      0.9399374084048886
    ],
    "n_cases": 100,
    "n_null_total": 0
  },
  "context_precision": {
    "runs": [
      {
        "file": "results/hybrid1_ragas_run1.json",
        "mean": 0.6926627384554883,
        "n_numeric": 99,
        "n_null": 1
      },
      {
        "file": "results/hybrid1_ragas_run2.json",
        "mean": 0.6926627384559935,
        "n_numeric": 99,
        "n_null": 1
      },
      {
        "file": "results/hybrid1_ragas_run3.json",
        "mean": 0.6981341189278157,
        "n_numeric": 99,
        "n_null": 1
      }
    ],
    "mean": 0.6944865319464325,
    "range": 0.005471380472327425,
    "sd": 0.003158902988391276,
    "bootstrap_ci": [
      0.6058361391353254,
      0.776613355737573
    ],
    "n_cases": 99,
    "n_null_total": 3
  },
  "context_recall": {
    "runs": [
      {
        "file": "results/hybrid1_ragas_run1.json",
        "mean": 0.9057239057239058,
        "n_numeric": 99,
        "n_null": 1
      },
      {
        "file": "results/hybrid1_ragas_run2.json",
        "mean": 0.9057239057239058,
        "n_numeric": 99,
        "n_null": 1
      },
      {
        "file": "results/hybrid1_ragas_run3.json",
        "mean": 0.9057239057239058,
        "n_numeric": 99,
        "n_null": 1
      }
    ],
    "mean": 0.9057239057239058,
    "range": 0.0,
    "sd": 0.0,
    "bootstrap_ci": [
      0.84006734006734,
      0.9562289562289561
    ],
    "n_cases": 99,
    "n_null_total": 3
  }
}
```
