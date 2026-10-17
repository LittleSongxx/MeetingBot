# 人工复核操作单（意图 100 条 + 扩展集 68 条）

这份单子是给**人**的：模型不能代替人签字。两件事都只差人工填写，工具已经把
包、校验和导入路径准备好了。

## 一、意图标签（100 条，四类）

1. 打开 `evaluation/results/intent_review_100.csv`。
2. 每行只填 `label` 列，取值四选一：
   - `single_fact`：一个具体事实/一段流程，答案一句话能说清；
   - `aggregation`：答案天然是列表（≥2 项），或问「哪些/哪几项/分别」；
   - `cross_meeting`：答案必须来自两段录音或多场会议（**注意：这一条要看语料布局，
     不能只看问题**）；
   - `smalltalk`：寒暄/与会议无关，答案不该来自会议资料。
3. `confidence` 可填 0–1，`note` 写歧义点（可空）。**空 label = 未复核**，不会被当成人工标签。
4. 导回：
   ```bash
   cd evaluation
   ../meetingbot/fastapi-app/.venv/bin/python gold/import_review.py \
     --intent-csv results/intent_review_100.csv
   ```
   输出 `gold/qa_seed_human_reviewed.json`（原文件不动）；报告会列出更新数、
   空行数、非法标签与仍未确认的条数。
5. 只有 `intent_source=human` 的条目才按人工金标引用；其余仍是 `agent_draft`。

**优先看这 4 条**（`results/intent_audit.json` 的机械审计）：
`q06`（标 aggregation 但无枚举标记）、`q28`（single_fact 但问“几个议题”）、
`q34`（single_fact 但“分别存在哪里”）、`q39`（single_fact 但“哪些编号”）。
审计只查矛盾，不证明标签正确。

## 二、扩展集 68 条（问题值不值得问 + 金标是否正确）

1. 打开 `evaluation/results/extended_review_68.csv`。
2. 每行填两列：
   - `human_worth_asking`：`yes`/`no`（这条问题是否值得作为评测题）；
   - `human_gold_correct`：`yes`/`no`（`expected_facts` 是否真的在标注片段里、
     是否是回答该问题的充分事实）。
   `human_note` 写理由（可空）。
3. 导回：
   ```bash
   ../meetingbot/fastapi-app/.venv/bin/python gold/import_review.py \
     --extended-csv results/extended_review_68.csv
   ```
   输出 `gold/qa_seed_extended_human.json`。两列都是 yes/是/1/true 的条目才把
   `case_source` 改成 `human`；否定的保留 `agent_draft`，人工意见留在 `human_review`。
4. 人工确认后的扩展集才能与 27 条人工集并列引用；在那之前只能报“代理起草”。

## 三、不要做的事

- 不要直接手改 `qa_seed.json` / `qa_seed_extended.json`：导入工具会把结果写到新文件，
  原文件保持可追溯。
- 不要把空行补成代理标签：空 = 未复核，不是“默认同意”。
- 不要把 `cross_meeting` 按问题字面判：答案落在哪段录音是语料布局，判断前先看
  `source`/`also_in` 与转写片段。
