# MeetingBot

**一场会开完，纪要已经能用了。**

录音或视频传进来，MeetingBot 负责剩下的：转写（谁说了什么）→ 六字段结构化纪要 → Agent 对着原文自检一遍 → 人工确认归档 → 进知识库可检索可问答（答案带可点回的引用）。FastAPI + Vue 3 + MySQL + Qdrant + LangGraph + Temporal + MCP，Docker Compose 一条命令起全套。

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-asyncio-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Vue 3](https://img.shields.io/badge/Vue-3-4FC08D?style=flat-square&logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![Element Plus](https://img.shields.io/badge/Element--Plus-2.8-409EFF?style=flat-square)](https://element-plus.org/)
[![MySQL 8](https://img.shields.io/badge/MySQL-8-4479A1?style=flat-square&logo=mysql&logoColor=white)](https://www.mysql.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-agent-1C3C3C?style=flat-square)](https://github.com/langchain-ai/langgraph)
[![Temporal](https://img.shields.io/badge/Temporal-durable--execution-00B4A0?style=flat-square)](https://temporal.io/)
[![Qdrant](https://img.shields.io/badge/Qdrant-hybrid%20search-DC244C?style=flat-square)](https://qdrant.tech/)
[![MCP](https://img.shields.io/badge/MCP-streamable%20HTTP-000000?style=flat-square)](https://modelcontextprotocol.io/)
[![docker compose](https://img.shields.io/badge/docker--compose-7%20services-2496ED?style=flat-square&logo=docker&logoColor=white)](compose.yaml)
[![license](https://img.shields.io/badge/license-MIT-3DA639?style=flat-square)](LICENSE)

![登录页](docs/assets/screenshots/login.png)

## 一场会议在这里经历什么

**① 上传**：会议资料页上传音视频，按内容哈希去重（同一会议内同一份文件不会存两遍）。

**② 转写**：FFmpeg 抽单声道音轨后走云 ASR 单请求直传（说话人分离），逐段落到数据库；片段支持人工修订，说话人拿不准时可以一键让 LLM 从参会人名单里给建议。

![转写片段](docs/assets/screenshots/transcription-detail.png)

**③ 纪要**：单遍长上下文调用生成六字段结构化纪要——概要、议题、观点、决策、待办、风险；每个字段带类型化槽位与证据来源，契约层把「不许凭空断言」做成数据结构不变量。

![纪要六字段详情](docs/assets/screenshots/minutes-detail.png)

**④ 自检**：LangGraph 状态机跑「审查 → 重写 → 复审」反思环，Agent 对着转写原文核对六块内容，全程 SSE 流式推给前端；修订稿必须人工确认才会覆盖原纪要。

![Agent 自检](docs/assets/screenshots/agent-detail.png)

**⑤ 确认归档**：确认后的纪要一键导出 Word / PDF；运行观测页把每次模型调用记账——成功率、Token 用量、失败原因，卡的住的证据都在。

![运行观测](docs/assets/screenshots/observability.png)

**⑥ 知识问答**：转写成功自动建索引、纪要确认后权威入库；用户在「知识问答」页提问，检索走 Qdrant 混合检索（dense + sparse，服务端 RRF 融合），回答逐 token 流式渲染，引用编号可点回原会议原句。同一组工具也通过 MCP（`/mcp`）暴露给外部客户端——**一个工具层、两种传输**。

（这一节的页面截图待一次真实运行后补：本仓库的截图都是实拍，不放占位图。）

## 转写：谁在什么时候说了什么

- 云端 ASR 单请求直传整段录音，一次调用拿到带说话人标签的分段结果，不再切块拼接
- 时长预检（≤2h）先拦一道，超长录音在入队前拒绝，不浪费调用
- 引擎可切换：默认云端（DashScope 文件转写），也可以指向本地侧车（FunASR + MOSS-Transcribe-Diarize）
- 片段级人工修订：改错字、合并拆分、重指说话人都落在 `transcript_segment` 表，可回溯
- 说话人建议是 suggestion-only：LLM 只从参会人名单里选，采纳与否由人决定

## 纪要：六个字段，每个都有出处

- 概要 / 议题 / 观点 / 决策 / 待办 / 风险，Pydantic 模型由契约层派生，结构化输出走 strict JSON Schema
- 每个槽位带类型（是否明说、是否推导、是否未提及）与来源闭合（origin + 证据 span），展现层做版本适配
- 磁盘调用缓存按「模型 + 提示词」哈希去重，重试不会为已经成功过的调用重复付费
- 修订、确认 / 取消确认、强制结束三条状态出口都在页面上，状态流转全部留痕

## 自检：改完必须人工确认

- 审查、重写、汇总、中断检查四个动作由 LangGraph 编排，图本身不碰数据库和模型，动作注入，可独立测试
- 每一步的输入输出、判定依据存 `agent_step`，运行状态机覆盖中断与恢复
- SSE 把步骤实时推到前端，长任务不用轮询
- 修订稿与原稿并存，人工确认前不覆盖；确认后原稿进入历史

## 知识库：人拍板的内容才进库

- 摄入挂在已有状态迁移上：转写成功 → 索引全文（RAW）；**纪要确认 → 索引权威条目（CONFIRMED），草稿永不进库**
- 切片：转写按片段边界聚合成重叠窗口（目标 300 字、重叠 2 句），纪要按条目切；切片 id 同时是向量库 point id，不做第二套编号
- 混合检索：稠密向量 + 稀疏词法（字符 bigram）各召回一批，由 Qdrant 在服务端做 RRF 融合——RRF 只看排名，规避两路分数量纲不同
- **权限过滤发生在检索层**：`None`＝不过滤（管理员）、空列表＝无可见会议，两种语义分开，混用即越权
- 幂等三层：workflow id、任务条件抢占、内容指纹；嵌入失败重试只补没嵌过的切片（不为已成功的部分重复付费）

## 问答：一个工具层，两种传输

- 单 Agent + 四工具（检索 / 结构化事实 / 取纪要 / 取转写原文）：业界建议 3–5 个，每多一个都降低选择可靠性
- 事实类问题走**具名参数查询**，不做自由文本转 SQL（那同时带来注入面与幻觉 SQL）
- 三道硬闸由代码兜住：最大轮数、token/成本预算、停滞检测——终止由模型判定，上限必须由代码守
- 回答逐 token 经 SSE 推送（`astream_events` v2，15 秒心跳，反代禁用缓冲），引用编号由宿主分配、答案写 `[n]`，页面渲染成可点回原句的卡片
- **MCP**：同一组工具以 streamable HTTP 挂在 `/mcp`（stateless），鉴权复用同一个 JWT 校验（含密码指纹），权限判在工具内部——所以中间件（限流/追踪）随父应用自动继承，也不存在"MCP 侧忘了判权"这条路

## 观测：每次调用都记账

- 每次模型调用写一行 `ai_call_log`：调用类型、耗时、Token、失败原因——「一行 = 一次真实调用」
- 失败分类器把超时 / 连接 / 限流 / 服务端错误分开，重试策略只对瞬态错误生效
- 观测页按天汇总趋势、成功率与模型排行，员工视角按业务归属过滤

## 架构

```mermaid
flowchart LR
    subgraph client[浏览器]
        UI[Vue 3 + Element Plus]
    end
    subgraph compose[Docker Compose]
        web[web :18082<br/>nginx 反代 + SSE 不缓冲]
        backend[backend :19091<br/>FastAPI + Tortoise ORM]
        agent[LangGraph<br/>纪要反思环 + 问答环]
        temporal[temporal :7233<br/>持久执行 / 幂等防双跑]
        db[(MySQL 8)]
        qdrant[(Qdrant<br/>混合检索)]
        redis[(Redis<br/>限流与预算计数)]
    end
    UI --> web --> backend
    backend --> db
    backend --> temporal
    backend --> agent
    backend --> qdrant
    backend --> redis
    backend -->|OpenAI 兼容| LLM[Qwen / 任意兼容模型]
    backend -->|文件转写| ASR[云 ASR]
    MCP[MCP 客户端] -->|/mcp| backend
```

| 链路 | 走过谁 |
|---|---|
| 转写链路 | 上传 → Temporal 提交任务 → ASR → 分段落库 →（可选）说话人建议 → 自动建索引 |
| 纪要链路 | 转写就绪 → 单遍长上下文生成 → 契约校验 →（可选）自检反思环 → 人工确认 → 导出 + 权威入库 |
| 问答链路 | 提问 → 检索（Qdrant 混合）或结构化查询 → 带引用回答 → 逐 token 流式渲染 → 引用点回原句 |

转写 / 纪要 / 自检 / 索引四类长任务全部走 Temporal 持久执行：workflow id 幂等防双跑、崩溃自动续跑，**每类任务一个队列、并发上限各自可配**（一类打满不影响另一类）。链路里每一环都能在代码里指到，详细分层与"五处统一串联"见 [docs/架构.md](docs/架构.md)。

## 技术栈

- **后端**：Python 3.12 · FastAPI · Tortoise ORM · Pydantic v2 · LangChain / LangGraph · MCP SDK
- **调度**：Temporal（durable execution，四类长任务，按类型分队列）
- **前端**：Vue 3 · Vite 5 · TypeScript（strict，`vue-tsc` 进 CI）· Element Plus · ECharts · markdown-it + DOMPurify
- **数据**：MySQL 8（18 张表，建库脚本含种子数据）+ Qdrant（混合检索）+ Redis（限流与预算计数，故障时 fail-open）
- **模型**：OpenAI 兼容接口（默认 Qwen3.8-Max + Qwen-Audio ASR），ASR 引擎可切换云端 / 本地侧车
- **部署**：Docker Compose 七服务 · FFmpeg · nginx
- **安全**：bcrypt 哈希 · JWT 密码指纹（改密即吊销）· 集中式滑动窗口限流（标准 429 头）· 内容审核三层 · 服务端 MIME + nosniff · 统一权限口径
- **可观测**：标准库 JSON 日志 · Prometheus `/metrics` · OTel GenAI 语义 span（OTLP 默认关闭）

```
meetingbot/
├── fastapi-app/                     # 后端
│   ├── api/                         # 路由：会议 / 资料 / 转写 / 纪要 / 自检 / 观测 / 配置
│   ├── services/                    # 领域服务：契约层 / 模型工厂 / 反思环 / Temporal 工作流
│   ├── common/                      # 基础设施：认证 / 限流 / JSON 修复 / 供应商能力表
│   └── tests/                       # 产品侧零调用测试
├── vue/                             # 前端：15 个管理页面（全量 TypeScript）
db/                                  # 建库脚本：01-schema.sql（结构）+ 02-seed.sql（演示数据）
                                     # 空卷初始化时按序导入；结构真源是 models.py，有漂移测试兜底
evaluation/                          # 评测体系（与产品代码物理隔离，只允许登记在案的四处委托）
├── README.md / DESIGN.md            # 评测宪法与四层设计书
├── LEDGER.md                        # 结论账本：可引用结论 + 撤回记录
├── asr/ benchmarks/ judges/ gate/ gold/ regression/ lib/ tests/
scripts/                             # compose.sh · 本地 ASR 侧车 · 索引脚本
docs/assets/                         # logo 与 README 实拍截图
```

## 快速开始

```bash
cp meetingbot/fastapi-app/.env.example meetingbot/fastapi-app/.env
# 编辑 .env，至少填：
#   MYSQL_PASSWORD / MYSQL_ROOT_PASSWORD   数据库口令
#   JWT_SECRET                             openssl rand -hex 32
#   DASHSCOPE_API_KEY                      模型密钥（不填服务也能起，调用会失败但页面可逛）
#   APP_ADMIN_PASSWORD                     管理员初始密码

./scripts/compose.sh up -d --build       # 七服务全起（db / backend / web / qdrant / redis / temporal ×2）
```

配置统一从 `meetingbot/fastapi-app/.env` 读取（密钥不进根目录、不进镜像、不进版本控制）。种子数据带示例会议、转写、纪要与模型配置；管理员账号见 `.env` 的 `APP_ADMIN_USERNAME / APP_ADMIN_PASSWORD`（首次启动自动建号）。

| 入口 | 地址 |
|---|---|
| 网页 | <http://127.0.0.1:18082> |
| 后端健康检查 | <http://127.0.0.1:19091/health> |
| Temporal | 127.0.0.1:17233（仅本机回环） |
| MCP 端点 | <http://127.0.0.1:19091/mcp>（streamable HTTP，需带 JWT） |
| 指标 | <http://127.0.0.1:19091/metrics>（Prometheus 文本） |

## 怎么验证它真的能跑

```bash
make -C evaluation smoke    # 零调用回归：契约不变量 / 证据可定位率 / 问答引用不变量 / 可靠性复算
make -C evaluation test     # 评测侧单测（223 项）
cd meetingbot/fastapi-app && .venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # 产品侧单测（405 项）
cd meetingbot/vue && npm ci && npm run type-check && npm run lint && npm run test   # 前端：类型 / lint / 单测
```

产品侧本地环境：`cd meetingbot/fastapi-app && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt`（pip 等价亦可）。

评测体系分五个入口：`smoke`（零调用回归）/ `deep`（裁判判读）/ `gate`（留出集门禁）/ `gold`（金标注与裁判校准）/ `rag`（轴 5 检索与回答）——`evaluation/README.md` 把这套写成"三层入口 ＋ 一个金标锚"，两种数法指的是同一份 `Makefile`：`gold` 是校准锚，算第三层的一部分还是独立一层而已。公开评测集（AliMeeting / VCSum）的语料与音频不入库，用 `evaluation/benchmarks/download_alimeeting.py` 按清单复得；live 运行有预算护栏并必须显式开启。

## 运维与排查入口

| 想做什么 | 入口 |
|---|---|
| 起停 / 看日志 | `./scripts/compose.sh up -d / stop / logs --tail=100 backend` |
| 后端健康 | `curl http://127.0.0.1:19091/health`（查 DB 连通） |
| 换模型 / 换密钥 | 改 `.env` 后 `./scripts/compose.sh up -d --force-recreate backend` |
| 失败调用排查 | 前端「运行观测」页，或直接查 `ai_call_log` 表 |
| 卡死的任务 | 纪要页「强制结束」出口（forceFail） |
| 问答质量与成本 | 「知识问答」页（引用卡片）+「运行观测」页（成本趋势与排行） |
| 限流参数 | `CHAT_ASK_LIMIT_PER_USER` / `CHAT_ASK_LIMIT_PER_IP` / `CHAT_DAILY_TOKEN_BUDGET`（见 `docs/容量模型.md`） |
| 内容审核 | 配 `MODERATION_WORDS` / `MODERATION_WORDS_FILE`；未配置时 `/chat/status` 会如实报告"未启用" |
| 部署细节与红线 | [DEPLOYMENT.md](DEPLOYMENT.md)（单 worker 约束、密钥泄露处置、索引修复） |

## 决策记录

产品机制的默认值不是拍脑袋定的：每个候选机制先写预注册规则再跑数，过了门禁才翻转默认；不显著的结论如实登记「检不出」，撤回的结论连理由保留。这些记录连同证据路径都在评测账本里，按章节节选：

| 主题 | 记录在哪 |
|---|---|
| 转写切分参数的产品默认值 | [evaluation/LEDGER.md](evaluation/LEDGER.md) · 预注册：切分提默认的判定规则 |
| 单请求直传 vs 切块拼接 | `LEDGER.md` · R5 ASR 单请求复测（假设修正过程） |
| 自检反思环的保默认机制 | `LEDGER.md` · R3 保默认机制验证 |
| 优化机制的默认开关状态 | `LEDGER.md` · 产品机制状态表 |
| 问答引用的两条零调用不变量 | `LEDGER.md` · 预注册：问答引用的两条零调用不变量 |
| 轴 5 检索与回答的基线口径 | `LEDGER.md` · 预注册：轴5 检索与回答四指标基线 |
| 全部撤回与存疑结论 | `LEDGER.md` · 存疑与已撤回 |

## 这个仓库里没有的东西

- **没有语料与音频**：AliMeeting / VCSum 评测语料与会议音频不入库（体积与数据集许可），脚本按清单复得；评测证据与运行账本在库。
- **没有外部基础设施依赖**：Qdrant 与 Redis 都在 compose 里（前者不映射宿主端口，后者只做计数）；模型走 OpenAI 兼容接口，换供应商只改 `.env`。Redis 故障时限流与预算闸 fail-open（放行并记日志），不阻断业务。
- **没有内置敏感词表**：合规口径取决于部署地与业务，词表由 `MODERATION_WORDS` / `MODERATION_WORDS_FILE` 提供；未配置时审核如实报"未启用"。
- **没有密钥**：`.env` 与任何密钥都不在版本控制内，仓库只有 `.env.example`。
- **没有个人资料与生成式架构图目录**：仓库只含项目代码、评测资产与实拍截图。

## 许可

- 本项目使用 [MIT 许可证](LICENSE)，可自由使用、修改与商用，保留版权声明即可。
- 使用云模型服务（Qwen / ASR）时请遵守对应供应商的服务条款；评测数据集的使用请遵守数据集自身的许可。
