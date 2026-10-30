# 当前项目作为「前后端 AI Agent 项目」的能力差距分析（只读，2026-10-30）

> 对照对象：当前 `MeetingBot` main（45 个后端 py 文件 / 8 个 service / 12 个测试文件 / 14 个管理端 Vue 页面）
> 参考对象：重写前旧分支 `meetingbot-legacy`（151 个后端 py 文件、~40 个 service、1053 个 evaluation 文件、TS+vitest 前端、compose 含 postgres/redis/temporal/jaeger）
> 证据来源：仓库源码 `path:line`、旧分支 `git show meetingbot-legacy:<path>`、`git ls-tree`、旧分支 README。

---

## 一、一句话结论

**当前项目是一个「用 LLM 做结构化生成的工具」，而不是一个「Agent 系统」**：唯一的自主部分是「审查→重写→汇总」的反思环（`meetingbot/fastapi-app/services/agent_graph.py:52-110`，3 个节点、固定边），没有工具调用、没有规划、没有检索、没有记忆、没有多轮对话、没有反馈学习、没有评测回归。作为个人项目它「够用且诚实」，但如果定位成 AI Agent 项目，核心欠缺在 **Agent 能力层、检索/记忆层、评测层、可靠性层** 四块，而不是前端页面。

---

## 二、旧分支有、当前 main 没有的能力（按对 Agent 定位的重要性排序）

| # | 能力 | 旧分支证据 | 当前 main 状态 |
|---|---|---|---|
| 1 | **工具调用 Agent + 多轮对话** | `services/chat_tools.py`、`chat_graph.py`、`chat_service.py`、`intent_router.py`、`query_plan.py`、`api/chat.py`；旧 README「单 Agent + 五工具（检索/结构化事实/取纪要/取转写原文/查已确认状态）」 | **无**：只有一次性结构化生成 + 反思环，无 tool、无对话、无多轮 |
| 2 | **知识库 / RAG 检索 + 引用** | `services/kb_*.py`、`pg_kb.py`、`hybrid.py`（pgvector dense + ParadeDB BM25 + RRF）、`retrieval_policy.py`、`citations.py`、`citation_bind.py`、`api/kb.py`；旧 README「纪要确认 → 索引权威条目（CONFIRMED），草稿永不进库」 | **无**：无向量库/全文检索，六块纪要没有可点击的转写引用，`MeetingMinutes` 模型无 evidence/citation 字段（`models.py:336-390`） |
| 3 | **评测体系 / 回归** | `evaluation/` 1053 个文件：RAGAS 官方口径报告（100 题×3）、`evaluation/baselines/ragas_official_hybrid_baseline.json`、`evaluation/tests/test_rag.py|test_recall_quality.py|test_trajectory.py|test_tool_choice.py|test_agent...`；旧 README「评测与项目文档入口」 | **无**：0 个评测脚本/基线/指标；CI 只跑 60 个 DB 单元测试（`meetingbot/fastapi-app/tests/`），AI 输出质量没有任何量化口径 |
| 4 | **记忆 / 会议系列 / 待办（跨会议事实层）** | `services/memory_service.py`、`series_service.py`、`action_item_service.py`、`action_validate.py`、`api/{memory,series,action_item}.py`；旧 README「记忆：PROPOSED→CONFIRMED，确认后才进问答工具」「系列：周会/项目例会」「待办：确认纪要的待办自动生成」 | **无**：13 张表里没有 series/memory/action_item（`models.py` Meta 列表）；每次会议是一次性孤岛 |
| 5 | **可靠任务编排（持久化工作流）** | 旧 compose 有 `temporal` + `temporal-worker` + `redis`；`services/agent_runtime.py`、`chat_checkpoint.py`、`chat_persistence.py`；旧 README「temporal 持久化」「Redis 限流/预算闸」 | **无**：进程内 `asyncio.create_task` + 启动 `recover_*`；无 checkpoint、无断点续跑、无租约/心跳、无独立 worker（`DEPLOYMENT.md:222-240` 已文档化单进程限制） |
| 6 | **模型工程化：失败分类/重试/降级/成本** | `services/pricing.py`、`online_eval_service.py`、`coverage.py`；旧 README「失败分类器把超时/连接/限流/服务端错误分开，重试策略只对瞬态错误生效」「token/成本预算闸」 | **无**：`model_factory.py:37-38` `max_retries=0`，单次失败=整个任务 FAILED；无成本字段/预算/限流；`ai_call_log` 只记 token 不记价格 |
| 7 | **安全护栏：prompt injection / 审核 / PII** | `services/prompt_injection.py`、`moderation.py`、`tests/test_injection_redteam.py`；旧 README「没有内置敏感词表，由 MODERATION_WORDS 提供」 | **无**：转写原文直接拼进 prompt（`minutes_service.py:99-104,167-172`），无注入检测、无内容审核、无 PII 脱敏 |
| 8 | **可观测：tracing / metrics / 告警** | 旧 compose 有 `jaeger`（OTel 链路）；旧 README「每次调用都记账 + 失败分类器 + 按天汇总」 | **部分**：有 `ai_call_log` + 观测页（`services/observability_service.py`），但无 trace/metric/告警；日志只到 stdout（`compose.yaml` 无 logging 配置） |
| 9 | **MCP / 外部工具生态** | 旧分支 `tests/test_mcp_server.py`（对外 MCP server 能力） | **无**：项目不能被其他 Agent 当工具调用 |
| 10 | **会议系列/待办/成本/评测前端** | 旧前端 `views/manager/{Chat,MeetingSeries,Memory,ActionItems}.vue`、`components/{CitationCard,CostPanel,OnlineEvalPanel,ActionItemBoard,MemoryReview}.vue`、TS+vitest+eslint | **无**：14 个页面全是 CRUD/管理页，没有 AI 对话/引用/评测面板；JS、无 lint/type-check/前端测试（`meetingbot/vue/package.json` 只有 dev/build/preview） |

---

## 三、当前 Agent 实质评估矩阵

| 能力 | 评级 | 事实 |
|---|---|---|
| 反思/自我批判 | **有（弱）** | `agent_graph.py:52-110` review→refine→finalize，最多 5 轮，`recursion_limit` 兜底；这是唯一的 Agent 行为 |
| 工具调用 / function calling | **无** | 全仓无 tool/function 定义，唯一「动作」是调 LLM 返回 JSON |
| 规划 / 多步任务分解 | **无** | 固定图，节点与边写死；`minutes_service` 的分块 map-reduce 是数据分片，不是规划 |
| 记忆（短期/长期） | **无** | 无会话、无跨会议事实；`AgentRun` 只是单次运行记录 |
| 检索（RAG） | **无** | 转写全文截断 2 万字符喂给审查（`agent_service.py:137-145`、`settings.py:93`），无检索 |
| 多轮对话 / 流式输出 | **无** | 无 chat 接口；SSE 只推运行状态（`api/agent.py:291-320`），没有 token 流式 |
| 人机协同 | **有（弱）** | 中断/恢复/应用/拒绝（`api/agent.py:205-289`），但无部分采纳、无修改后应用、无反馈沉淀 |
| 评测/回归 | **无** | 无 golden set、无 RAGAS/LLM-judge、无 CI 评测；改 prompt 无法量化影响 |
| 成本/预算控制 | **无** | 无 per-user 配额、无预算闸、无降级/限流；任何会议可见者可反复触发付费调用（见主报告权限项） |
| 可观测（Agent 轨迹） | **部分** | `agent_step` 记录每步输入/输出/耗时（页面时间线），但无 trace 关联 LLM 调用、无指标/告警 |
| 安全/护栏 | **无** | 无 prompt injection 防护、无输出审核、无 PII 处理、API Key 明文入库（`models.py:175-190`） |

---

## 四、工程化缺口（LLM/Agent 工程视角）

1. **Prompt 工程无版本管理**：`PromptTemplate` 只有 `code/name/system_prompt/user_prompt/enabled/update_time`（`models.py:297-330`），无版本表、无 diff、无回滚、无 A/B；模板改动不可追溯，无法回答「这次改动让质量变好还是变坏」。
2. **结构化输出约束过硬、无修复路径**：`model_factory.py:40-45` 强制 `json_schema strict=true`，`max_retries=0`；模型偶发多一个字段/少一个字段就直接失败整块，且失败不重试、不降级到「宽松解析/二次修复」。
3. **无评测即无质量工程**：没有 golden 会议/纪要数据集、没有指标（引用准确率、决策召回、幻觉率、人工采纳率）、没有 CI 基线；项目无法证明「改进了」，只能凭感觉。
4. **成本不可控/不可见**：`ai_call_log` 记 token 但不记价格/预算；`observability` 只有 token 汇总（`observability_service.py:110-148`），无「每次会议成本」「每个用户/部门成本」「预算超限拒绝」。
5. **失败无告警**：三类后台任务失败只写 DB + 日志（`minutes_service.py:192-195` 等），没有告警/重试通知；运营只能靠用户反馈。
6. **数据留存/清理**：`ai_call_log` 无删除/归档路径；上传文件/演示数据无生命周期管理；旧分支的「保留策略」没有继承。
7. **安全纵深不足**：token 在 localStorage、无 CSP/安全头、无登录限流（见主报告）；API Key 明文入 DB（`models.py:175-190`），仅列表接口掩码。
8. **多实例不可用**：单进程 asyncio + 内存去重（`DEPLOYMENT.md:222-240`），无法横向扩展；任务锁实际只保护第一次（主报告 M10）。
9. **前端 Agent UX 缺位**：没有 chat/对话入口、没有引用卡片可跳转转写时间点、没有「修订前后 diff」视图（应用是整体覆盖 `api/agent.py:243-251`）、没有流式输出，AI 能力在界面上主要表现为「按钮 + 等待 + 结果表」。

---

## 五、如果目标是「能讲清楚的 AI Agent 项目」，优先补 5 件事

> 排序 = 技术含金量 × 展示效果 ÷ 改动量；每项都可在当前框架上增量实现。

### 1）评测闭环（最高性价比，1~2 天）
- 建 5~10 场标注会议的小型 golden set（摘要/决策/待办字段级参考答案），写 `evaluation/` 脚本：字段覆盖率、决策召回、引用准确率、人工采纳率；把「改 prompt 前后跑同一套指标」做成 CI artifact（零模型调用的 mock 回归 + 可选真实跑）。
- 展示价值：面试可直接说「每次 prompt 改动都有指标回归，采纳率从 x% → y%」；这是 AI 工程和 demo 项目的分水岭。

### 2）引用/证据绑定（1~3 天）
- 六块纪要的每个条目增加 `evidence: [{segment_id, start_ms, speaker}]`；生成时让模型返回 segment 编号（或代码按文本匹配回填），前端可点击跳转到转写时间点；人工确认时能核对「这句话出自第几分钟」。
- 展示价值：可验证的 grounding，直接回应「LLM 幻觉怎么办」；也是 RAG/检索的铺垫。

### 3）工具化 Agent（1 周，可按小步做）
- 把「行动项」做成第一个工具：新增 `action_item` 表 + Agent 在纪要确认后「生成待办草稿」or「对话式查询『我这周有什么待办』」；再扩 2 个只读工具（`search_minutes`、`get_transcript`），用 function calling 做一个最小 chat 面板（单轮/多轮 + SSE 流式）。
- 展示价值：从「LLM 调用」升级为「Agent 使用工具」，是 AI Agent 项目最核心的展示点。

### 4）可靠性/成本护栏（2~3 天）
- 模型调用：失败分类 + 指数退避重试（仅瞬态错误）+ 单任务预算上限 + 每用户每日调用配额；中途失败可续跑（把已完成 chunk 落库）。
- 展示价值：讲清楚「Agent 不是一次 API 调用，需要失败处理、预算、幂等」；也是把项目从 demo 变「可上线」的关键。

### 5）前端 Agent 体验（3~5 天）
- 自检详情增加「修订前后 diff 视图 + 按条应用/拒绝」；聊天面板支持流式回答 + 引用卡片；路由懒加载 + nginx gzip/缓存/安全头（顺手修主报告 P1/P2）。
- 展示价值：同一套能力，界面看起来「像 AI 产品」而不是「管理后台」。

> 不建议现在补的：多 Agent 协作、Temporal/消息队列、pgvector 全套 RAG 基础设施——对个人项目投入产出比低，先把「工具调用 + 评测 + 引用」做扎实，比堆概念更有说服力。

---

## 六、取舍建议（个人项目）

- 旧分支能力完整但复杂度高（151 个 py 文件、9 个 compose 服务、1053 个评测文件），已被有意精简为可部署、可讲解的主线；**不建议整体回迁**。
- 最合适的定位叙述：**「单进程、可人工确认的会议纪要 LLM 工具 + 反思环自检」**；若升级为 Agent 项目，按第五节增量补，每补一项都能单独讲一个技术故事。
- 当前代码的注释/文档质量（大量「为什么这么做」的说明）是加分项，保留这种风格。
