# VCSum 离线评分口径

`scoring.py` 只读取已保存的模型结果和锁定的 VCSum dev 参考摘要，不调用模型。完整 dev 集包含 22 场；报告必须同时看 `reference_case_count`、`result_case_count`、`paired_case_count`、`excluded` 和 `missing_case_ids`。中断后的部分结果只能称为已完成样例的分数，不能称为 22 场完整成绩。

```bash
python evaluation/public_benchmark/scoring.py \
  --manifest evaluation/public_benchmark/manifest_all_dev.json \
  --results evaluation/public_benchmark/results/RUN_ID/report.json \
  --output evaluation/public_benchmark/results/RUN_ID/scores.json \
  --markdown evaluation/public_benchmark/results/RUN_ID/scores.md
```

评分固定比较 `baseline.output.summary`、`revised.output.summary` 与官方 `summary`，输出逐场字符级 ROUGE-1/2/L 的 Precision、Recall、F1，以及同场修订前后的 F1 差值。`aggregate` 为会议宏平均，`paired_mean_delta_f1_bootstrap_95ci` 按会议配对重采样；每项另列改善、持平、退化场数。`telemetry` 汇总所有已有结果，包括失败样例已记录的调用开销；`scored_telemetry` 仅汇总参与配对评分的场次。没有供应商价格记录时不推断费用。

该实现采用 Unicode NFC 和字符级切分，去除空白、标点及符号。它与论文可能采用的中文分词 ROUGE 口径不同，不能直接对照论文分数。ROUGE 反映与单条参考摘要的文字重叠，不能判定事实正确、遗漏是否严重或人工是否可直接采纳。VCSum 官方整体摘要没有负责人、期限、风险和行动项的完整逐项金标；这些指标需依据原始转写另做独立人工盲审。[空白标注模板](annotation_template.csv)覆盖 22 场的 A/B 两份输出，不预设哪份是初稿。
