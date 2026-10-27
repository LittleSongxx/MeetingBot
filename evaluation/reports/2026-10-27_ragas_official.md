# RAGAS 官方口径重测报告（r1 仪器）

> 生成时间：2026-10-27T11:32:28.179722+00:00
> 仪器：ragas `0.4.3`；judge `deepseek-flash`（temp=0）；embedding `qwen3.7-text-embedding`（dim=1024）；prompt 语言 `en-official`
> 数据：`evaluation/results/eval100_scope_fixed_packet.json` sha256 `50c5102607a36a3b2e72fec69b35e91bd556a4e3698d66c96f17294a58e40cf7`；reference `evaluation/gold/qa_ref_answers.json` sha256 `60a0c41f46edf108c51f5dde8e7ddec0c713239fa45b05031da3827d9ef52c5c`（human_checked=False）
> 运行：N=3；100 cases/run；标签 `r1`（**禁止与旧自研口径 71.83/90.02/88.34/99.00 相减**）
> reference 校验：167/167 句与 gold facts 逐字一致（脚本 `gold/build_qa_ref_answers.py`）；人工抽检 30 条 CSV 待填写（`gold/qa_ref_spotcheck_30.csv`），`human_checked=false` 如实保留。

## 1. 四指标（case 级均值；3 次运行）

| 指标 | run1 | run2 | run3 | 3 次均值 | 极差 | SD | 逐题 bootstrap 95% CI | 有效 n / null |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| faithfulness | 0.6680 | 0.6646 | 0.6734 | **0.6687** | 0.0088 | 0.0044 | [0.6270, 0.7134] | 100 / 0 |
| answer_relevancy | 0.9054 | 0.9084 | 0.9074 | **0.9071** | 0.0030 | 0.0015 | [0.8724, 0.9358] | 100 / 0 |
| context_precision | 0.6972 | 0.6987 | 0.6958 | **0.6973** | 0.0029 | 0.0015 | [0.6124, 0.7817] | 100 / 0 |
| context_recall | 0.8792 | 0.8792 | 0.8892 | **0.8825** | 0.0100 | 0.0058 | [0.8208, 0.9333] | 100 / 0 |

## 2. 分层读数（3 次运行合并）

| 数据集 | n | faithfulness | answer_relevancy | context_precision | context_recall |
|---|---:|---:|---:|---:|---:|
| cross30 | 30 | 0.6840 | 0.8417 | 0.2523 | 0.6917 |
| holdoutA | 20 | 0.6093 | 0.9744 | 0.9375 | 0.9750 |
| holdoutB | 15 | 0.6409 | 0.9005 | 0.8667 | 0.9333 |
| holdoutD | 20 | 0.7605 | 0.9327 | 0.8569 | 0.9500 |
| newmeeting | 15 | 0.6224 | 0.9204 | 0.8846 | 1.0000 |

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
        "file": "results/ragas_official_run1.json",
        "mean": 0.6679772418242231,
        "n_numeric": 100,
        "n_null": 0
      },
      {
        "file": "results/ragas_official_run2.json",
        "mean": 0.6646304863200213,
        "n_numeric": 100,
        "n_null": 0
      },
      {
        "file": "results/ragas_official_run3.json",
        "mean": 0.6733882081939015,
        "n_numeric": 100,
        "n_null": 0
      }
    ],
    "mean": 0.6686653121127154,
    "range": 0.008757721873880242,
    "sd": 0.004419219800653664,
    "bootstrap_ci": [
      0.626994265697415,
      0.7133702038416087
    ],
    "n_cases": 100,
    "n_null_total": 0
  },
  "answer_relevancy": {
    "runs": [
      {
        "file": "results/ragas_official_run1.json",
        "mean": 0.905425463721902,
        "n_numeric": 100,
        "n_null": 0
      },
      {
        "file": "results/ragas_official_run2.json",
        "mean": 0.9083832308661683,
        "n_numeric": 100,
        "n_null": 0
      },
      {
        "file": "results/ragas_official_run3.json",
        "mean": 0.90739318600884,
        "n_numeric": 100,
        "n_null": 0
      }
    ],
    "mean": 0.9070672935323035,
    "range": 0.002957767144266321,
    "sd": 0.0015055733292080725,
    "bootstrap_ci": [
      0.8723752593571092,
      0.9357832194688313
    ],
    "n_cases": 100,
    "n_null_total": 0
  },
  "context_precision": {
    "runs": [
      {
        "file": "results/ragas_official_run1.json",
        "mean": 0.69722222218169,
        "n_numeric": 100,
        "n_null": 0
      },
      {
        "file": "results/ragas_official_run2.json",
        "mean": 0.6987499999585488,
        "n_numeric": 100,
        "n_null": 0
      },
      {
        "file": "results/ragas_official_run3.json",
        "mean": 0.6958333332917778,
        "n_numeric": 100,
        "n_null": 0
      }
    ],
    "mean": 0.697268518477339,
    "range": 0.0029166666667710395,
    "sd": 0.0014588843756468241,
    "bootstrap_ci": [
      0.6123611110723589,
      0.7817129629155651
    ],
    "n_cases": 100,
    "n_null_total": 0
  },
  "context_recall": {
    "runs": [
      {
        "file": "results/ragas_official_run1.json",
        "mean": 0.8791666666666668,
        "n_numeric": 100,
        "n_null": 0
      },
      {
        "file": "results/ragas_official_run2.json",
        "mean": 0.8791666666666668,
        "n_numeric": 100,
        "n_null": 0
      },
      {
        "file": "results/ragas_official_run3.json",
        "mean": 0.8891666666666667,
        "n_numeric": 100,
        "n_null": 0
      }
    ],
    "mean": 0.8825,
    "range": 0.009999999999999898,
    "sd": 0.005773502691896198,
    "bootstrap_ci": [
      0.8208333333333334,
      0.9333333333333332
    ],
    "n_cases": 100,
    "n_null_total": 0
  }
}
```
