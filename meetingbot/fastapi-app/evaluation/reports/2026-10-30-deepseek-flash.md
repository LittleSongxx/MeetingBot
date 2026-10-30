# DeepSeek Flash 本地评测报告（2026-10-30）

> 评测对象：`MeetingBot` 主分支（commit `1737f25` + 本轮 `LLM_JSON_MODE` 支持）在隔离评测栈上的真实运行。
> 结论定位：**评测管线验证**，不是统计意义上的质量结论——golden set 只有 1 个演示用例。

## 1. 环境与模型

| 项 | 值 |
|---|---|
| 评测栈 | `meetingbot-aim-eval`，与演示栈隔离 |
| 网页 / 后端 | `http://127.0.0.1:18182` / `http://127.0.0.1:19192` |
| 数据库 | `ai_meeting_eval`（独立命名卷） |
| LLM | **`deepseek-flash`**，`https://api.deepseek.com` |
| 结构化输出 | `LLM_JSON_MODE=json_object`（DeepSeek 暂不支持 strict `json_schema`） |
| ASR | `paraformer-v2`（百炼；本轮复用种子转写，未重新转写） |
| 生成参数 | `MAX_CHUNK_CHARS=6000`，`CHUNK_SECONDS=1200` |
| 数据集 | 演示会议「产品发布评审会」1 场：1 任务 / 1 纪要 / 3 条种子行动项 / 3 个 golden 决策 / 3 个 golden 行动项 / 8 个 golden 证据片段 |

模型配置在评测栈启动时由 bootstrap 写入：MINUTES / AGENT / SPEAKER 用 `deepseek-flash`，TRANSCRIPTION 用 `paraformer-v2`（DashScope）。

为了方便接入只支持 `json_object` 的厂商，本轮给 `services/model_factory.py` 增加了 `LLM_JSON_MODE` 开关：

- `json_schema`（默认，百炼等）：严格 `response_format={"type":"json_schema","strict":true,...}`；
- `json_object`（DeepSeek 等）：`response_format={"type":"json_object"}`，schema 约束继续由提示词里的 schema 说明 + Pydantic validator 兜底。

## 2. 基线：种子演示数据

| 指标 | 结果 |
|---|---|
| 六字段覆盖率 | 100.0% |
| 有证据的条目占比 | 100.0%（13/13） |
| 证据引用合法率 | 100.0%（13/13） |
| 人工修改率（AI 草稿→终稿） | 0.0% |
| Golden 决策命中 | 3/3 |
| Golden 行动项召回 | 3/3 |
| Golden 证据片段命中 | 8/8 |

## 3. 真实模型生成 + 确认 + 行动项 + 问答

操作：放弃演示自检运行 → 对 task 1 重新生成纪要（真实调用 DeepSeek）→ 确认 → 生成行动项并确认生效 → 提问一次 → 再次评测。

### 3.1 生成结果

| 字段 | 条数 | 带 evidence 的条目 | evidence 引用数 |
|---|---|---|---|
| topics 议题 | 5 | 5 | 9 |
| viewpoints 观点 | 6 | 6 | 6 |
| decisions 决策 | 3 | 3 | 3 |
| pending_items 待确认 | 3 | 2 | 2 |
| risks 风险 | 3 | 3 | 3 |
| **合计** | **20** | **19** | **23** |

摘要（节选）：会议确认功能验收 17 项通过 15 项，消息推送免打扰已回归通过；报表导出压测超时（单用户 3 个月数据 40 多秒、网关 30 秒超时、500 并发失败率 12%），决定改为异步导出；灰度按 10%/30%/100% 三阶段放量，9 月 1 日先放 10%，对外通稿 9 月 12 日定稿；异步导出任务堆积风险由张涛评估队列与限流方案。

### 3.2 生成后指标

| 指标 | 结果 |
|---|---|
| 六字段覆盖率 | 100.0% |
| 有证据的条目占比 | **95.0%（19/20）** |
| 证据引用合法率 | **100.0%（23/23）** |
| 人工修改率（AI 草稿→终稿） | 0.0%（本轮未人工修改） |
| Golden 决策命中 | 3/3 |
| Golden 行动项召回（仅种子行动项） | 2/3 |
| Golden 行动项召回（生成行动项后） | **3/3** |
| Golden 证据片段命中 | 8/8 |

### 3.3 行动项

- 重新生成时按设计清理了 DRAFT 种子行动项，保留 2 条已生效/已完成；
- 本次生成接口返回 `created=4, skipped=2`，最终 6 条行动项，4 条确认生效；
- 生成内容覆盖了决策、灰度放量、对外通稿、风险跟进等；
- **仍有近义重复风险**：种子已有“报表导出改为异步导出，前端增加导出任务列表”，本次又生成“报表导出异步化后端改造与前端任务列表联调”；行为/时间/负责人相同的任务建议下一轮做相似度去重；
- **有一项证据缺失**：20 个条目里 1 个 pending 条目没有绑定 evidence，导致覆盖率 95%；引用合法率仍是 100%（23/23），说明绑定的都是真实逐字片段。

### 3.4 单场问答（真实调用 DeepSeek）

问：`报表导出最终决定改成什么方案？`

答：`报表导出最终决定改为异步导出方案：先落任务再下载文件，由张涛负责后端，前端任务列表由李悦跟进，9 月 10 日前联调完。`

引用两条（均可在转写中逐字定位）：

- `[01:10] 张晨`：segment 10，70200–82600ms；
- `[00:49] 张晨`：segment 7，49200ms，异步导出方案原始讨论。

## 4. 结论与下一步

**已验证**：DeepSeek Flash 通过 `json_object` 模式可以走通「真实生成 → 强 Schema/Pydantic 校验 → 证据绑定 → 确认 → 行动项 → 问答 → 指标」全链路；23/23 引用逐字合法；问答回答与引用均正确。

**不能下的结论**：只有 1 个演示会议 + 1 个 golden 用例，不能证明 DeepSeek Flash 的整体质量或与其他模型的高低；证据合法率不等于语义正确率；修改率 0% 只代表没人改。

**下一步**：

1. **扩 golden set**：10~20 场真实会议，人工标注决策/行动项/关键证据片段，再谈模型结论；
2. **DeepSeek 内 A/B**：同一 Key 下对比 `deepseek-flash` 与 `deepseek-v4-pro`，固定 Prompt/分块/温度，每个配置重复 3 次，记录均值/方差；两者都用 `LLM_JSON_MODE=json_object`；
3. **补证据覆盖**：针对 pending_items 的 evidence 缺失，检查 Prompt 的【证据要求】与模型输出 schema，目标 100% 条目带引用；
4. **行动项去重**：按归一化相似度合并近义任务，增加“重复率”指标；
5. **延迟/成本**：从 `ai_call_log` 汇总 token、耗时、失败率，与质量指标一起看性价比。

## 5. 复现

```bash
# 启动评测栈（密钥在 eval/.env：ASR 用百炼 Key、LLM 用 DeepSeek Key）
docker compose --project-directory eval --env-file eval/.env \
  -f eval/compose.eval.yaml up -d --build

# 离线指标
pwsh ./eval/run_eval.ps1

# 真实生成前：先放弃演示自检运行
#   GET  /api/agent/selectPage
#   DELETE /api/agent/delete/{run_id}
# 然后 POST /api/meetingMinutes/generate/1，轮询到 DRAFT，再确认/生成行动项/问答
```

报告生成时间：2026-10-30 19:59（Asia/Shanghai）。
