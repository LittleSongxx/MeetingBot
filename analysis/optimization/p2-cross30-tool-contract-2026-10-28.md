# P2 跨会议覆盖：失败分类与工具契约缺口（2026-10-28）

> 基于 M1b cross30 30 题 × 3 次 r1 判定与逐题上下文/答案检查；明细 `cross30_taxonomy.csv`、`cross30_diagnosis.csv`。

## 1. 分类结果（30 题）

| 类别 | 数量 | 含义 |
|---|---:|---|
| CP artifact（CP 0 / CR 高） | 12 | 答案与 reference 重叠高、CR=1，CP=0；RAGAS CP 结构性判 0，不是检索失败（m001–m006 等） |
| coverage/gold | 8 | CR 低，答案看似对但上下文/参考不齐（m007/009/012/014/016/017/018/022） |
| ok-ish | 9 | 指标已可接受或单项 |
| AR low | 3 | 否定式/答案风格（m016/018/024） |
| faith low | 3 | 同上叠加 unsupported 断言（m016/017/018） |

## 2. coverage/gold 8 题的具体缺口

| case | 现象 | 根因（按证据） |
|---|---|---|
| m007 | 答案很长，含 `source_id=6` 内部标识；CR=0 | 答案把"哪段录音"写成内部 id；上下文虽含转写，但 reference 用"上午/下午录音"表述；工具字段未提供"上午/下午/场次"语义 |
| m009 | 答案声称"下午录音未涉及"，CR=0 | 模型把"下午检索结果没有"写成事实断言（negative claim）；上下文只有上午证据 |
| m012 | 答案正确（PROPOSED/未确认），CR=0.5 | 上下文只有 1 条 state item，reference 还要求"待办 OPEN"；工具结果没有同时给出 task 状态 |
| m014 | 答案正确（CONFIRMED 偏好），CR=0.08 | gold 写 scope=USER/user1；上下文 source_text 只写"负责人：系统管理员"，结构化 scope 字段未进入 context |
| m016 | 答案正确（草稿三项结论），faith/AR/CP/CR 全低 | 只抓到 1 条会议元数据 context，没拿到草稿纪要内容；`get_minutes(include_draft=true)` 未被有效使用 |
| m017 | 答案正确（无 supersedes），CR=0 | 上下文 source_text 没有 `supersedes_id` 字段文本；结构化字段存在于 state item，但未进入 citation/context 文本 |
| m018 | 答案正确（DRAFT 非 CONFIRMED），AR/CP/CR 低 | 上下文是原始转写复述，没有 DRAFT/CONFIRMED 状态字段；否定式答案又被 AR 惩罚 |
| m022 | 答案漏掉关键日期节点，CR=0.17 | 只检索到纪要摘要与复述段，未检索到 8/20、8/26、8/30、8/31 等日期原文；需要跨会议/多段覆盖 |

## 3. P2 通用机制（不按题写规则）

1. **状态字段进入 context 文本**：`query_meeting_state` 的 item 已有 `status/source_state/supersedes_id/scope/series` 等结构化字段，但 citation/source_text 只渲染正文；把这些字段以固定格式（如 `状态：PROPOSED；替代：无；系列：…`）写进 source_text，让模型与 RAGAS judge 都能看到证据。这是"工具契约/渲染"层，不是提示层。
2. **系列成员与场次语义**：系列题的 citation 应带"系列/会议场次/上午/下午/录音序号"字段，避免模型输出 `source_id` 这种内部 id；答案是给人看的，不能靠内部 id 表达"哪段录音"。
3. **草稿/候选可用性**：系列状态题若 confirmed 为空，工具应显式返回"是否有草稿/候选、如何取"（已有 notice，但需与 `get_minutes(include_draft=true)` 的覆盖回执绑定）。
4. **覆盖回执**：对系列题，工具结果附"已覆盖会议/场次/状态类型"，与 `coverage.py` 的 EvidenceState 对齐；仅在缺口时提示补查，不重复已试失败的 state coverage prompt。
5. **内部 id 后处理**：生成后把 `meeting_id/source_id/task_id` 等替换成可读会议名/场次；这是确定性清洗，不依赖模型。

## 4. 预注册建议（P2，待 Docker 恢复）

- 主指标：cross30 的 CR（目标 +5pp 以上）与整体 CR；护栏：faith/AR/CP 非劣、引用可定位率不降。
- 只改工具结果/渲染层（source_text 字段），不改检索参数、不改 prompt 长度；100 题 per-case，r1 N=1 筛 → N=3 确认。
- 与 OPT-4（混合回答风格）分开实验，避免同时改 prompt 与工具契约。

## 5. 已实施（离线单元测试，2026-10-28）

- 修复 `citations._from_state` 的 supersedes 分支：生产 `query_meeting_state` 条目用 `kind`，旧代码只认 `state_type`，导致 m017 类"无替代关系"永远进不了 context。现兼容两者。
- `state_service._memory_state` 增加 `scope_type/scope_id`；`_from_state` 对 USER 偏好输出"范围：用户 {id}"，对齐 m014 类 reference 的 scope=USER。
- 测试：`tests/test_tool_enrichment.py` 新增 2 条；`test_citations.py + test_tool_enrichment.py` 共 **10 passed**（仅单测，未 live）。
- Docker 恢复后用 P2 A/B 验证 cross30 CR 是否随 context 字段可见性提升。
