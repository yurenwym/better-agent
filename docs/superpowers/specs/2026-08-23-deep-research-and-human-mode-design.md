# 深度研究、定时任务与真人对话模式：方案 A 开发设计

> 文档状态：正式开发设计，待评审
>
> 创建日期：2026-08-23
>
> 目标项目：Better Agent
>
> 参考实现：[Comet v0.0.4](https://github.com/lm041520/Comet)

## 0. 结论

本设计采用方案 A：吸收 Comet v0.0.4 的深度研究流水线、定时执行、通知推送与真人聊天表达机制，但按 Better Agent 的现有架构重新实现，不复制 Comet 的基础设施栈。

最终方案为：

1. 新增独立 `ResearchEngine`，按“规划 → 多源检索 → 确定性过滤 → 逐源提炼 → 一轮反思补搜 → 大纲与证据分配 → 分节写作 → 摘要与引用拼装”执行；
2. 研究请求落为独立 `research_jobs`，由单并发 `ManagedResearchWorker` 持租约执行，不把长流水线塞进普通 `AgentRuntime` 或 `turn_jobs`；
3. 手动研究从现有 Thread/Turn 发起，研究进度写入同一 Thread 的 append-only `thread_events`，继续使用现有 SSE 序号、`Last-Event-ID` 与快照补偿；
4. 定时任务使用进程内 `ManagedScheduler` 扫描 SQLite 到期记录，原子生成研究 Job；启动恢复与重试都基于租约和幂等键，不引入 Celery、Redis 或外部 cron；
5. 运行历史就是按 `schedule_id` 查询 `research_jobs`，不额外建设重复的通用历史子系统；
6. Server酱、企业微信、钉钉与通用 Webhook 由四个小适配器完成，复用现有 `httpx`；报告提交成功与通知投递分离，通知失败不能回滚报告；
7. 真人模式是全局、用户可控的“可见正文表达策略”，只影响回答正文，不得进入 JSON 控制头、结构化模型调用、工具协议、研究报告或系统事件；
8. 真人回复在正文中使用 `[[next]]` 分隔，前端最多渲染 3 个气泡。历史消息立即展开，只有本次新生成消息逐条揭示；
9. 本期不实现群聊，也不为单一的 direct channel 预存无用字段；真正出现第二种渠道时再抽象响应策略接口。

本方案维持单进程 FastAPI、SQLite WAL 和现有依赖。只有真实指标证明单机方案达到上限时，才评估多进程调度、外部队列或独立数据库。

## 1. 用户承诺

系统对用户作出以下可验证承诺：

1. 深度研究不会只把搜索摘要拼成文章；正文中的事实先被绑定到具体来源，再进入章节写作；
2. 页面关闭、SSE 断开不会停止研究；重新打开线程后能从已持久化游标和快照恢复；
3. 同一个手动请求、定时触发时刻或重试操作不会产生重复研究 Job；
4. 局部来源检索、抓取或提炼失败时，系统保留成功来源并尽力完成；没有足够证据时必须失败或明确报告不足，不能无引用硬写；
5. 报告状态一旦为 `COMPLETED`，通知服务故障不能把它改回失败；
6. 每次研究的来源、证据、阶段、尝试次数、错误、报告和通知结果都可追溯；
7. 开启真人模式后，普通聊天变得口语、短句和多气泡，但计划文档、深度研究报告、工具调用及内部控制协议保持结构化与稳定；
8. 关闭真人模式后，新回复立即恢复标准表达，历史消息不被改写。

## 2. 范围与非目标

### 2.1 本期范围

- 手动创建、查看、取消和重试深度研究；
- 研究计划、多源检索、逐源提炼、一次反思补搜、大纲整理、分节流式写作、摘要与引用；
- 研究进度在当前 Thread 中实时展示，完成后成为一条普通 assistant 消息；
- 研究报告、来源、证据、章节和错误持久化；
- 每日、每周、固定小时数三种定时规则；
- 定时任务启停、立即运行、编辑、删除和运行历史；
- Server酱、企业微信、钉钉、通用 Webhook 的配置、测试和完成后通知；
- 全局真人对话开关和最多 3 个气泡的逐条展示；
- 启动恢复、取消、有限重试、幂等和安全测试。

### 2.2 非目标

- 群聊、成员身份、@提及、群权限和群消息路由；
- Redis、Celery、PostgreSQL、ORM、LangChain、MCP 或通用工作流框架；
- 任意 cron 表达式、秒级调度、分布式 Worker 和多节点一致性；
- 通知渠道插件市场、用户编写通知脚本或任意模板语言；
- 全自动无限反思、无限搜索、自动事实裁决或学术级引文格式；
- 将研究过程映射为现有 Goal/Run/PlanVersion；
- 将真人风格用于深度研究报告、计划正文、通知报告正文或 Agent Runtime 的结构化输出；
- 为只有一个实现的未来群聊策略提前创建接口、工厂或插件系统。

## 3. 当前架构基线

Better Agent 已经具备方案所需的大部分可靠性原语：

- `backend/app/db.py`：SQLite、`BEGIN IMMEDIATE`、WAL、30 秒 busy timeout；
- `backend/app/conversation.py`：`threads`、`turns`、`turn_jobs`、租约认领、心跳续租、取消和过期租约恢复；
- `backend/app/events.py`：每 Thread 单调递增序号的 append-only `ThreadEventStore`；
- `backend/app/api.py`：事件快照 API、SSE、`Last-Event-ID` 与 keep-alive；
- `backend/app/main.py`：FastAPI lifespan 中的恢复、Worker 启停；
- `backend/app/live_model.py`：一行 JSON 控制头与可见正文严格分离；
- `frontend/src/hooks/useThreadTelemetry.ts`：事件缺口检测、快照恢复、消息增量偏移校验；
- `frontend/src/components/ConversationThread.tsx`：统一消息展示入口。

现有请求链路为：

```text
POST /api/threads/{thread_id}/turns
  -> ConversationService.accept_turn()
  -> turns + user message + turn_jobs + turn.accepted（同一事务）
  -> ManagedTurnWorker claim/heartbeat
  -> LiveConversationModel.route_and_respond()
  -> ControlHeadDecoder
  -> thread_messages + message.* / turn.* events
  -> Thread SSE / Last-Event-ID
  -> useThreadTelemetry snapshot recovery
```

深度研究不应破坏这条链路。它是比普通回答更长、可单独取消和重试的领域任务，应使用独立账本与 Worker；用户仍从 Thread 进入，并继续通过 Thread SSE 观察它。

## 4. 对 Comet v0.0.4 的实现解读

### 4.1 深度研究如何工作

Comet 的 `run_research()` 是与传输层解耦的 async generator。它不会直接操作 SSE，而是连续产生 `status`、`plan`、`sources`、`progress`、`section_start`、`token`、`section_done`、`report` 或 `error` 事件。业务服务消费事件后广播、维护重连快照并落库；定时任务也消费同一个引擎，但不经过在线 SSE。

核心阶段如下：

| 阶段 | 输入 | 关键动作 | 输出 |
|---|---|---|---|
| 规划 | 用户主题 | 生成标题、章节、多视角子问题与搜索词 | `ResearchPlan` |
| 多源检索 | 搜索词 | 并发查 Web、知识库和工具来源，抓正文 | `Source[]` |
| 来源过滤 | 原始来源 | URL 去重、最短正文阈值、质量排序、数量截断 | 可用来源 |
| 逐源提炼 | 单个来源 + 主题 + 章节 | 每条事实只绑定当前来源号，并给相关度 | `Learning[]` |
| 反思补搜 | 计划 + 已有证据 | 找证据缺口，最多补一轮查询与提炼 | 补充来源、证据 |
| 大纲整理 | 章节 + 全部证据 | 为各节确定论点并分配 evidence IDs | `CuratedSection[]` |
| 分节写作 | 本节证据 + 前文摘要 | 逐节流式写作，引用使用来源号 | 章节正文 |
| 汇总拼装 | 全部章节 + 来源 | 生成 TL;DR、核心要点、参考来源并链接引用 | Markdown 报告 |

最值得继承的设计不是“调用多个搜索源”，而是引用对齐发生在逐源提炼阶段。`Learning` 保存 `text + source_index + relevance`；写作者只拿分配给当前章节的 Learning。这样 `[来源 N]` 不是文章写完后猜出来的，而是从证据产生时就绑定。

Comet 还做了三类降级：单个来源失败不终止整批；规划和大纲解析失败有确定性 fallback；研究失败时保留部分正文、计划与来源用于排查和重试。方案 A 保留这些语义。

### 4.2 定时任务与运行历史

Comet 的任务表保存研究指令、触发类型、下一次运行时间和最近状态。心跳扫描到期任务，先推进 `next_run_at`，再派发研究。研究报告通过 `task_id` 关联任务，因此“历史”就是查询该任务产生的报告，不另建历史表。

这个领域划分是正确的；但 Comet 用 Celery beat、Celery worker、Redis 锁和 PostgreSQL `SKIP LOCKED`。Better Agent 当前是本地单机应用，复制这套基础设施只会增加部署和故障面。方案 A 用现有 SQLite 事务与租约实现同等的本机语义。

### 4.3 通知

Comet 将 `(title, content)` 转成四种 HTTP payload，并检查渠道业务响应：Server酱检查 `code`，企微和钉钉检查 `errcode`。这比只依赖 HTTP 2xx 更可靠。通知作为研究成功后的降级步骤，异常只记录 warning，不修改报告结果。

### 4.4 真人模式

Comet 用一段额外系统提示把普通聊天约束为口语、短句、无报告腔，并让模型在明显换话题或停顿处输出独占分隔符 `[[next]]`。前端最多拆成 3 段：历史消息立即全部显示，本次生成从“正在输入”开始依次展示。

这套交互可以复用，但 Better Agent 比 Comet 多一层严格的一行 JSON 控制头。因此真人提示不能粗暴追加到所有模型调用；它只能进入 `LiveConversationModel` 的可见正文规则，并且必须明确控制头、ask 工具与任何 JSON 优先。

## 5. 目标架构

```text
普通对话 POST /turns                         定时心跳
        |                                      |
        v                                      v
LiveConversationModel -- research policy --> research_schedules
        |                                      |
        +---------- create research_job <------+ 
                           |
                           v
                 ManagedResearchWorker
                   claim + heartbeat
                           |
                           v
                    ResearchEngine
 Plan -> Retrieve -> Filter -> Distill -> Reflect once
      -> Curate -> Write sections -> Summarize -> Cite
                           |
             +-------------+--------------+
             |                            |
             v                            v
 research_* authority tables      thread_events mirror
 report/source/evidence/section    existing seq + SSE replay
             |                            |
             +-------------+--------------+
                           v
                assistant report message
                           |
                           v
                    NotificationService
                  delivery attempts only

全局 human_mode
  -> only LiveConversationModel visible body prompt
  -> [[next]] in one logical assistant message
  -> frontend HumanBubbles (history instant / new animated)
```

### 5.1 权威边界

| 数据 | 权威来源 | 投影/传输 |
|---|---|---|
| 研究生命周期、阶段、错误、租约 | `research_jobs` | `thread_events` |
| 来源元数据和抓取正文 | `research_sources` | 来源简表事件/API |
| 来源绑定证据 | `research_evidence` | 默认不放完整事件正文 |
| 最终报告与部分正文 | `research_reports` | assistant message、报告 API |
| 章节进度与完成正文 | `research_sections` | `research.section.*`、`message.delta` |
| 定时规则和下一触发时刻 | `research_schedules` | Schedule API |
| 通知配置 | 环境变量引用 + `notification_channels` | 掩码 API |
| 通知结果 | `notification_deliveries` | Job 详情 API |
| 实时游标 | `threads.next_event_seq` + `thread_events` | SSE |
| 真人模式开关 | `app_settings` | bootstrap/settings API |
| 真人气泡分隔 | `thread_messages.content` 中的 `[[next]]` | 前端展示 |

`thread_events` 是进度账本，不存来源全文、证据全文或完整报告，以免事件无限膨胀。完整内容由资源 API 读取。

## 6. 深度研究领域设计

### 6.1 入口与路由

普通 Turn 的控制协议增加 `policy='start_research'`。控制头仍必须是第一行 JSON，建议 V3：

```json
{"v":3,"policy":"start_research","content_shape":"research","reason_code":"explicit_deep_research","research":{"topic":"Comet 的深度研究机制","scope":"web"}}
```

约束：

- 只有用户明确要求“深度研究、调研、形成带来源报告”时使用；
- `research.topic` 必须是非空字符串，最大 2,000 字符；
- V3 只增加 `research` 字段，不改变 V1/V2 解析；
- `start_research` 不允许同时声明 `artifact`、调用 `ask_user` 或输出普通正文；
- 解码完成后，普通 Turn Worker 在同一事务中创建 `research_jobs`，发出 `research.queued`，把 Turn 置为 `COMPLETED`；
- `UNIQUE(source_turn_id)` 保证请求重放只产生一个 Job；
- 前端也可提供“深度研究”显式按钮，但仍提交同一个 Turn API，不增加第二条手动入口链路。

所有 Job 必须有一个 `source_turn_id`，因为现有 `thread_events.turn_id` 非空：

- 自然语言入口直接使用发起研究的真实 Turn；
- 首版显式研究 API 在目标 Thread 没有非终态 Turn 时，原子创建一个已完成的锚点 Turn 和对应 user message，再创建 Job；
- 每个 Schedule 在创建时配置一个专用 Thread；每次 occurrence 在专用 Thread 中创建一个 `reason_code='scheduled_research'` 的已完成锚点 Turn；
- 手动重试创建新锚点 Turn 和新 Job，不复用旧 Turn；
- 锚点 Turn 的 `policy='start_research'`、`content_shape='research'`，不进入 `turn_jobs`，因此不会被普通 Turn Worker 重复回答。

显式 API 如果发现目标 Thread 存在非终态 Turn，返回 `409`，不覆盖 `threads.active_turn_id`。Schedule 使用专用 Thread，避免定时 occurrence 与用户正在进行的普通对话竞争 active Turn。

如果产品初期不希望立即升级控制头，可以先由独立 `POST /api/threads/{thread_id}/research` 按上述规则创建锚点 Turn 和 Job；但正式目标仍应统一进 Turn 路由，使自然语言入口、审计和幂等保持一致。实现顺序见第 16 节。

### 6.2 `ResearchEngine` 接口

```python
async def run_research(request: ResearchRequest) -> AsyncIterator[ResearchEvent]:
    ...
```

引擎只负责阶段编排和结构化事件，不知道 FastAPI、SSE、SQLite 租约、Thread、Schedule 或通知。

核心数据类：

```text
ResearchRequest
  job_id, topic, source_scopes, limits, cancel_event

ResearchPlan
  title, sections[], queries[]

Source
  id, ordinal, kind, canonical_url, title, content, content_hash,
  published_at, retrieved_at, quality_score

Evidence
  id, source_id, text, date_hint, relevance

CuratedSection
  ordinal, heading, thesis, evidence_ids[]

ResearchEvent
  type, phase, data
```

不要让事件使用任意 dict 到处透传。引擎内部使用 dataclass；持久化/事件边界再显式序列化和校验。

### 6.3 阶段状态机

`research_jobs.status`：

```text
QUEUED -> RUNNING -> COMPLETED
                  -> FAILED
                  -> CANCELLED
```

`research_jobs.phase`：

```text
queued
planning
retrieving
distilling
reflecting
curating
writing
summarizing
finalizing
completed | failed | cancelled
```

规则：

- `status` 是调度/恢复状态，`phase` 是用户可见进度，不把二者混为一个枚举；
- 每次 claim 将 `attempts + 1`，只在持有有效租约时写阶段和最终状态；
- `COMPLETED` 事务必须同时写最终报告、结束当前 Job、创建/完成 assistant message，并追加 `research.completed`；
- 取消请求写 `cancel_requested_at`；Worker 心跳和每个外部调用边界检查取消；
- 丢失租约后立即停止写入，不把旧 Worker 的结果覆盖新 owner；
- Job 失败保留计划、来源、证据、已完成章节和 `partial_markdown`；
- 用户重试创建新 Job，`retry_of_job_id` 指向旧 Job。历史不原地重跑，避免覆盖审计数据。

### 6.4 阶段一：规划

模型返回严格 JSON：报告标题、2–6 个章节、每节子问题、最多 12 个去重查询。解析失败时使用确定性 fallback：

- 标题使用截断后的主题；
- 章节为“背景与定义、核心事实与证据、争议与限制、结论与建议”；
- 查询由主题和章节标题组合生成。

规划不是最终报告，不应用真人模式。

### 6.5 阶段二：多源检索

MVP 定义最小 `ResearchRetriever` 协议，并至少实现：

1. `WebSearchRetriever`：调用配置好的搜索 HTTP API，随后抓取候选网页正文；
2. `LocalNoteRetriever`：在 Better Agent workspace 的允许目录内读取用户明确纳入范围的 Markdown/TXT 资料。

不为尚不存在的知识库或 MCP 创建空实现。未来增加来源时，只需返回同一 `Source` 数据类。

检索策略：

- 查询并发上限 4；网页抓取并发上限 4；
- 每个请求独立 10–20 秒超时；
- 对 429、408 和 5xx 做最多 2 次指数退避；4xx 配置错误不重试；
- 每个查询取最多 5 条，整轮抓取最多 20 个唯一 URL；
- 只允许 `http`/`https`，拒绝环回、私网、链路本地、`file:` 和重定向后落入这些地址，防止 SSRF；
- 响应体设置字节上限，只接受文本/HTML；
- 来源正文按配置上限截断，并保存 content hash；
- 外部来源始终视为不可信数据，不能覆盖系统提示、工具权限或引用规则。

### 6.6 阶段三：确定性过滤

过滤先于 LLM 提炼：

- URL 去 fragment、规范 host/path，按 canonical URL 去重；
- 无 URL 的本地来源按 `kind + locator + content_hash` 去重；
- 正文低于最短有效字符数时剔除；
- 过滤抓取错误页、登录页和几乎无正文页面；
- 对域名多样性、查询覆盖、正文长度、标题匹配和发布时间给确定性分数；
- 同域名默认最多保留 3 条，防止单站垄断证据；
- 最终来源按分数稳定排序后分配 `ordinal`，一旦分配不再重新编号。

过滤规则要单元测试，不能由模型自行决定“这个网站是否可信”。权威性判断仍在报告中以来源性质和交叉印证表达。

### 6.7 阶段四：逐源提炼

每个模型调用只接收一个 Source、研究主题和章节列表，并返回：

```json
{"evidence":[{"text":"...","date_hint":"2026-08","relevance":0.86}]}
```

强制规则：

- 本次输出的每条 Evidence 自动绑定当前 `source_id`，模型不能指定别的来源；
- Evidence 必须是来源中可找到支撑的简洁事实、数据或有归属的观点；
- 不允许把多个来源混成一条 Evidence；
- relevance 限制在 0–1，低于阈值的记录丢弃；
- 单来源最多保留 6 条，全局最多 80 条；
- 同时最多提炼 4 个来源；单个来源失败只记录失败原因；
- 所有来源都无有效 Evidence 时，Job 以 `insufficient_evidence` 失败。

### 6.8 阶段五：一次反思补搜

反思输入只有主题、计划、已有 Evidence 摘要和已用查询，不输入整篇网页。输出最多 3 个新查询。

只有以下条件同时满足才执行：

- `reflection_rounds=1`；
- 第一轮至少产生一条 Evidence；
- 模型指出具体章节或事实缺口；
- 新查询与旧查询规范化后不同。

补搜结果走同样的抓取、过滤和逐源提炼流程。MVP 不允许第二轮反思，避免无限搜索和不可预测成本。

### 6.9 阶段六：大纲与证据分配

模型为每节返回 `heading + thesis + evidence_ids[]`。服务端必须：

- 过滤不存在的 ID；
- 去除单节重复 ID；
- 限制每节 Evidence 数量；
- 保留计划章节顺序；
- 解析失败时按 Evidence 与章节关键词的确定性匹配分桶；
- 没有证据的章节可以删除或明确写“现有来源不足”，不能凭空填充。

### 6.10 阶段七：分节写作

每节写作只接收：标题、本节 thesis、本节 Evidence 及前面章节的一句话摘要。它不接收所有来源全文，降低提示长度和跨来源错引。

正文引用使用稳定标记 `[[source:<source_id>]]`，而不是让模型生成 URL 或可变序号。最终拼装时由服务端转成 Markdown 链接。对于引用标记：

- 未知 source ID：报告提交前校验失败；
- 有 URL：渲染为带标题的 Markdown 链接；
- 本地来源：渲染为非链接编号并在参考来源中标注定位信息；
- 没有引用的事实性段落由最低验收规则标记，测试环境可直接失败。

章节 token 仍通过 `message.delta` 进入同一 logical assistant message；每个完成章节同时持久化到 `research_sections`。重启后从第一个未完成章节继续，已完成章节不重新写。

### 6.11 阶段八：摘要和最终提交

摘要输入是已完成章节，不重新读取来源全文。服务端拼装：

```text
# 标题
> TL;DR
## 核心要点
## 章节 1..N
## 研究限制
## 参考来源
```

最终提交前必须验证：

- 至少一个完成章节；
- 至少一个有效来源和 Evidence；
- 所有引用都可解析到当前 Job 的来源；
- 来源编号连续且稳定；
- 报告大小未超过配置上限；
- 报告中不含控制头、内部 source ID、密钥或原始工具协议。

## 7. 数据模型

以下为 V1 权威 schema。ID 继续使用项目现有前缀 + UUID hex；时间统一存 UTC ISO 8601。

### 7.1 `research_jobs`

```text
id                         TEXT PRIMARY KEY
thread_id                  TEXT NOT NULL
source_turn_id             TEXT NOT NULL UNIQUE
schedule_id                TEXT
retry_of_job_id            TEXT
trigger_kind               TEXT NOT NULL       -- manual | scheduled | retry | run_now
occurrence_key             TEXT UNIQUE
topic                      TEXT NOT NULL
source_scopes_json         TEXT NOT NULL DEFAULT '["web"]'
status                     TEXT NOT NULL
phase                      TEXT NOT NULL
available_at               TEXT NOT NULL
lease_owner                TEXT
lease_until                TEXT
attempts                   INTEGER NOT NULL DEFAULT 0
max_attempts               INTEGER NOT NULL DEFAULT 2
cancel_requested_at        TEXT
started_at                 TEXT
finished_at                TEXT
last_error_json            TEXT
created_at                 TEXT NOT NULL
updated_at                 TEXT NOT NULL
```

索引：

```text
INDEX(status, available_at, lease_until, created_at)
INDEX(schedule_id, created_at DESC)
INDEX(thread_id, created_at DESC)
```

`occurrence_key`：

- 手动：`manual:{source_turn_id}`；
- 定时：`schedule:{schedule_id}:{scheduled_for_utc}`；
- 立即运行：客户端提供的 idempotency key；
- 重试：`retry:{old_job_id}:{client_retry_key}`。

### 7.2 `research_job_attempts`

```text
id                         TEXT PRIMARY KEY
job_id                     TEXT NOT NULL
attempt                    INTEGER NOT NULL
lease_owner                TEXT NOT NULL
status                     TEXT NOT NULL       -- RUNNING | COMPLETED | FAILED | LEASE_LOST | CANCELLED
started_at                 TEXT NOT NULL
finished_at                TEXT
error_json                 TEXT
UNIQUE(job_id, attempt)
```

`research_jobs.attempts` 是 claim 使用的当前计数；`research_job_attempts` 是不可覆盖的逐次执行审计。Schedule 的运行历史仍是其 Job 列表，不再创建通用 history 表；Job 详情附带 attempt 列表。

### 7.3 `research_reports`

```text
job_id                     TEXT PRIMARY KEY
title                      TEXT
outline_json               TEXT
summary_json               TEXT
markdown                   TEXT
partial_markdown           TEXT NOT NULL DEFAULT ''
source_count               INTEGER NOT NULL DEFAULT 0
evidence_count             INTEGER NOT NULL DEFAULT 0
assistant_message_id       TEXT UNIQUE
created_at                 TEXT NOT NULL
updated_at                 TEXT NOT NULL
completed_at               TEXT
```

一个 Job 最多一份报告，失败也保留 `partial_markdown`。重试产生新的 Job/Report，不修改旧报告。

### 7.4 `research_sources`

```text
id                         TEXT PRIMARY KEY
job_id                     TEXT NOT NULL
ordinal                    INTEGER NOT NULL
kind                       TEXT NOT NULL         -- web | local_note
canonical_url              TEXT
locator                    TEXT
title                      TEXT NOT NULL
content                    TEXT NOT NULL
content_hash               TEXT NOT NULL
published_at               TEXT
retrieved_at               TEXT NOT NULL
quality_score              REAL NOT NULL
metadata_json              TEXT NOT NULL DEFAULT '{}'
UNIQUE(job_id, ordinal)
UNIQUE(job_id, kind, content_hash)
```

### 7.5 `research_evidence`

```text
id                         TEXT PRIMARY KEY
job_id                     TEXT NOT NULL
source_id                  TEXT NOT NULL
text                       TEXT NOT NULL
date_hint                  TEXT
relevance                  REAL NOT NULL
created_at                 TEXT NOT NULL
UNIQUE(job_id, source_id, text)
```

### 7.6 `research_sections`

```text
id                         TEXT PRIMARY KEY
job_id                     TEXT NOT NULL
ordinal                    INTEGER NOT NULL
heading                    TEXT NOT NULL
thesis                     TEXT NOT NULL DEFAULT ''
evidence_ids_json          TEXT NOT NULL DEFAULT '[]'
status                     TEXT NOT NULL          -- PENDING | WRITING | COMPLETED
markdown                   TEXT NOT NULL DEFAULT ''
summary                    TEXT NOT NULL DEFAULT ''
generation                 INTEGER NOT NULL DEFAULT 1
updated_at                 TEXT NOT NULL
completed_at               TEXT
UNIQUE(job_id, ordinal)
```

### 7.7 `research_schedules`

```text
id                         TEXT PRIMARY KEY
name                       TEXT NOT NULL
thread_id                  TEXT NOT NULL UNIQUE  -- 任务专用 Thread
topic                      TEXT NOT NULL
source_scopes_json         TEXT NOT NULL DEFAULT '["web"]'
trigger_type               TEXT NOT NULL          -- daily | weekly | interval_hours
trigger_time               TEXT                   -- HH:MM
trigger_weekday            INTEGER                -- 0 Monday .. 6 Sunday
interval_hours             INTEGER
timezone                   TEXT NOT NULL          -- IANA zone, default Asia/Shanghai
enabled                    INTEGER NOT NULL DEFAULT 1
notify_enabled             INTEGER NOT NULL DEFAULT 1
next_run_at                TEXT
last_run_at                TEXT
last_job_id                TEXT
deleted_at                 TEXT
created_at                 TEXT NOT NULL
updated_at                 TEXT NOT NULL
```

用 `CHECK` 约束确保三种 trigger 只设置各自所需字段。时区计算使用 Python 3.11 标准库 `zoneinfo`。

### 7.8 `notification_channels`

```text
id                         TEXT PRIMARY KEY
name                       TEXT NOT NULL
channel_type               TEXT NOT NULL          -- serverchan | wecom | dingtalk | webhook
secret_env_name            TEXT NOT NULL
enabled                    INTEGER NOT NULL DEFAULT 1
created_at                 TEXT NOT NULL
updated_at                 TEXT NOT NULL
UNIQUE(channel_type, name)
```

V1 不把 secret 写入 SQLite。`secret_env_name` 只允许匹配 `^[A-Z][A-Z0-9_]{2,63}$`，运行时读取环境变量；API 只返回 `configured: true|false`，不返回变量值。对于 Server酱，变量保存 SendKey；其他渠道保存完整 webhook URL。

### 7.9 `notification_deliveries`

```text
id                         TEXT PRIMARY KEY
job_id                     TEXT NOT NULL
channel_id                 TEXT NOT NULL
attempt                    INTEGER NOT NULL
status                     TEXT NOT NULL          -- PENDING | SENT | FAILED
http_status                INTEGER
provider_code              TEXT
error                      TEXT
created_at                 TEXT NOT NULL
finished_at                TEXT
UNIQUE(job_id, channel_id, attempt)
```

### 7.10 `app_settings`

```text
id                         INTEGER PRIMARY KEY CHECK(id = 1)
human_mode                 INTEGER NOT NULL DEFAULT 0 CHECK(human_mode IN (0, 1))
updated_at                 TEXT NOT NULL
```

V1 只保存一行全局设置。先使用显式 `human_mode` 列，出现第二个真实设置时再评估是否增加列或改成其他结构，不为一个布尔值建设通用配置系统。

### 7.11 约束与删除语义

- schema 必须包含 Job → Thread/Turn/Schedule、Report/Attempt/Source/Evidence/Section → Job、Evidence → Source、Delivery → Job/Channel 的外键；
- 删除 Schedule 不级联删除 Job；统一写 `deleted_at` 并保留历史关联；
- 首版不提供删除 Job；未来增加显式删除时，事务内级联删除报告中间数据和消息关联，但 append-only Event 只保留随机 ID 与删除事件，不保留报告正文；
- `status`、`phase`、`trigger_kind`、`trigger_type`、`channel_type`、布尔整数和 relevance 范围使用 `CHECK`；
- 所有 JSON 字段写入前由服务层验证类型与大小，读取失败视为数据完整性错误，不能静默使用空对象；
- `research_jobs.source_turn_id` 非空且唯一；`schedule_id` 为空仅允许 manual/retry trigger。

## 8. Worker、恢复与重试

### 8.1 `ManagedResearchWorker`

实现方式直接遵循 `ManagedTurnWorker`：

- 单并发循环；
- `BEGIN IMMEDIATE` 查找 `available_at <= now` 的 `QUEUED` 或租约已过期的 `RUNNING` Job；
- 原子写 `RUNNING + lease_owner + lease_until + attempts+1`，并插入对应 `research_job_attempts`；
- 每 `lease_seconds / 3` 续租；
- 每次阶段提交都验证 owner 和有效租约；
- 进程关闭时停止认领新 Job，并向当前引擎发取消信号；不把应用 shutdown 当成用户取消；
- 启动后过期租约自然被重新认领，不需要另写“把 RUNNING 全部改回 QUEUED”的恢复脚本。

新 Worker 接管租约已过期的 Job 时，在同一 claim 事务中把上一条 `RUNNING` attempt 标为 `LEASE_LOST`，再插入新的 attempt。旧 Worker 即使稍后返回，也会因 owner/lease 校验失败而不能提交。

研究阶段可恢复粒度：

- planning/retrieving/distilling/reflecting/curating 中断：本次 attempt 从该阶段重新计算，依靠唯一约束去重写入；
- writing 中断：从首个非 `COMPLETED` section 继续；
- finalizing 中断：若报告已完成但 message/event 未提交，不可能出现，因为它们在同一 SQLite transaction 完成；
- notification 中断：不影响研究 Job，由 delivery 状态单独重试。

### 8.2 自动重试

自动重试只处理短暂故障：超时、429、连接错误、5xx 和租约接管。规则：

- 默认 `max_attempts=2`；
- 第一次可重试失败：当前 attempt 标记 `FAILED`，Job 回到 `QUEUED`，写 `available_at = now + backoff`；
- 结构化输出连续失败、无有效证据、认证错误、非法配置和用户取消不自动重试；
- 达到上限后置 `FAILED`；
- 手动“重试”始终创建新 Job，并允许用户修改主题或来源范围。

退避不得使用阻塞 `sleep` 占住 Worker。

### 8.3 `ManagedScheduler`

调度器是单独的轻量协程，不执行研究：

1. 每 30 秒查询 `enabled=1 AND next_run_at <= now` 的任务；
2. 在一个 `BEGIN IMMEDIATE` 事务内重新读取 due schedule；
3. 计算本次 `scheduled_for` 和新的 `next_run_at`；
4. 在任务专用 Thread 内创建已完成锚点 Turn/user message，并以 `schedule:{id}:{scheduled_for}` 插入 `research_jobs`；唯一冲突视为已触发；
5. 更新 schedule 的 `next_run_at`；
6. 提交后由 `ManagedResearchWorker` 正常认领。

错过触发策略：启动时每个 schedule 最多补跑一次，即以当前 `next_run_at` 代表的最近一次到期 occurrence 创建 Job，然后把下一次时间推进到未来；不回放停机期间的所有小时，避免“惊群式补跑”。

编辑规则：

- 修改规则、时区或启用状态时立即重新计算 `next_run_at`；
- 禁用时 `next_run_at=NULL`；
- 删除统一写 `deleted_at` 并禁用 schedule；列表默认隐藏，历史 Job 必须保留；
- 同一 schedule 同时最多一个非终态 Job。到期时若已有活动 Job，本次 occurrence 记录为跳过并直接推进；V1 不积压。

### 8.4 lifespan

`backend/app/main.py` 的启动顺序：

```text
recover plan projection intents
start ManagedTurnWorker
start ManagedResearchWorker
start ManagedScheduler
start ManagedNotificationWorker（若采用独立 delivery worker）
yield
stop scheduler
stop notification worker
stop research worker
stop turn worker
```

V1 可以由 Research Worker 在完成后直接逐渠道投递并落 delivery，省去第四个 Worker；但投递必须发生在报告完成事务之后，且有独立超时。只有需要跨重启自动补投时，再增加 `ManagedNotificationWorker`。本设计推荐首版直接投递，失败由用户在 Job 详情中手动重试。

## 9. 事件与 SSE 协议

### 9.1 事件命名

所有研究事件继续写 `thread_events`，actor 使用 `research_worker` 或 `scheduler`：

| 事件 | 最小 data |
|---|---|
| `research.queued` | `job_id, trigger_kind, schedule_id?` |
| `research.started` | `job_id, attempt` |
| `research.phase_changed` | `job_id, phase, detail` |
| `research.plan_ready` | `job_id, title, section_count, query_count` |
| `research.sources_updated` | `job_id, source_count` |
| `research.section_started` | `job_id, section_id, ordinal, heading` |
| `research.section_completed` | `job_id, section_id, ordinal` |
| `research.retry_scheduled` | `job_id, attempt, available_at, reason_code` |
| `research.completed` | `job_id, report_url, message_id, source_count` |
| `research.failed` | `job_id, reason_code, retryable` |
| `research.cancelled` | `job_id` |
| `notification.sent` | `job_id, channel_id` |
| `notification.failed` | `job_id, channel_id, reason_code` |

事件不得包含来源全文、Evidence 全文、报告 Markdown、webhook URL、SendKey、模型 prompt 或异常堆栈。

### 9.2 消息流

研究开始时创建一个 `thread_messages` assistant 占位消息，状态 `streaming`。为了明确关联，在 `thread_messages` 增加：

```text
research_job_id TEXT UNIQUE
```

阶段状态通过 `research.*` 展示在研究进度卡；只有章节正文进入 `message.delta`。现有 offset/generation 校验继续有效：

- 重试当前 attempt 时发 `message.completed(finish_reason='retry')`，新建 generation；
- Worker 重启恢复时先发 `message.snapshot`，再续写；
- 最终报告完成时使用权威 Markdown 替换消息快照，防止增量拼接差异；
- 报告消息仍是一条 logical message，不按章节创建多条聊天消息。

### 9.3 SSE 重连

不新增 research SSE endpoint。前端继续订阅：

```text
GET /api/threads/{thread_id}/events/stream
Last-Event-ID: <thread seq>
```

如检测到事件序号缺口，继续调用现有事件快照和消息快照 API。新增研究详情快照只在事件要求展示来源/章节详情时读取：

```text
GET /api/research/jobs/{job_id}
```

这样 Redis stream buffer 在 Better Agent 中没有存在必要。

现有 `_thread_event_stream()` 在活动 Turn 进入终态且暂时没有事件时会关闭。加入研究后，关闭条件必须改为：

```text
no new events
AND no non-terminal turn_jobs in this thread
AND no QUEUED/RUNNING research_jobs in this thread
```

不能只查看 `threads.active_turn_id`。研究锚点 Turn 可以在 Job 入队事务中结束，但 Thread SSE 必须一直跟随独立 Research Job；浏览器断线后仍通过相同序号重连。

## 10. API 设计

所有 mutation 继续使用现有 JSON content type、本地 Origin 与 CSRF 校验。

### 10.1 研究

```text
POST   /api/threads/{thread_id}/research
GET    /api/research/jobs?thread_id=&schedule_id=&status=&limit=&offset=
GET    /api/research/jobs/{job_id}
POST   /api/research/jobs/{job_id}/cancel
POST   /api/research/jobs/{job_id}/retry
GET    /api/research/jobs/{job_id}/report
GET    /api/research/jobs/{job_id}/sources
```

首版显式创建请求：

```json
{
  "client_request_id":"research-request-uuid",
  "topic":"研究 Comet 的深度研究机制并形成开发文档",
  "source_scopes":["web"]
}
```

响应 `202`：

```json
{"job_id":"research_...","status":"QUEUED","event_cursor":123}
```

当第 6.1 节 V3 路由完成后，UI 和自然语言入口统一改走普通 Turn；保留此 endpoint 供研究页面显式发起，但其服务层必须调用同一个 `ResearchService.create_job()`。

取消是幂等操作。终态 Job 再取消返回当前快照，不报 500。重试要求客户端 idempotency key，响应新 Job ID。

### 10.2 定时任务

```text
POST   /api/research/schedules
GET    /api/research/schedules
GET    /api/research/schedules/{schedule_id}
PUT    /api/research/schedules/{schedule_id}
DELETE /api/research/schedules/{schedule_id}
POST   /api/research/schedules/{schedule_id}/run
GET    /api/research/schedules/{schedule_id}/jobs
```

`run` 接口要求 `client_request_id`，通过 occurrence key 幂等。创建/编辑响应必须返回服务端计算后的 `next_run_at` 和 timezone，前端不自行推算。

### 10.3 通知与设置

```text
POST   /api/notification/channels
GET    /api/notification/channels
PUT    /api/notification/channels/{channel_id}
DELETE /api/notification/channels/{channel_id}
POST   /api/notification/channels/{channel_id}/test
POST   /api/research/jobs/{job_id}/notifications/{channel_id}/retry
GET    /api/settings
PUT    /api/settings/human-mode
```

通知创建示例只传环境变量名，不传 secret：

```json
{"name":"手机","channel_type":"serverchan","secret_env_name":"BETTER_AGENT_SERVERCHAN_KEY"}
```

## 11. 通知设计

### 11.1 适配器

`NotificationService.send(channel, title, content)` 内部按 `channel_type` 选择四个普通函数，不创建抽象基类或插件注册表：

| 渠道 | 请求 |
|---|---|
| Server酱 | SendKey 组装官方 URL，form data: `title, desp` |
| 企业微信 | webhook URL，JSON markdown payload |
| 钉钉 | webhook URL，JSON markdown payload |
| Webhook | JSON `{title, content, job_id, report_url}` |

共同规则：

- `httpx.AsyncClient` 总超时 10 秒；
- 不跟随或严格限制跨 host 重定向；
- webhook URL 必须通过与检索相同的 SSRF 校验；若用户明确需要局域网 webhook，增加单独的 `ALLOW_PRIVATE_WEBHOOKS` 本机配置，默认关闭；
- Server酱检查 `code`；企微、钉钉检查 `errcode`；通用 webhook 非 JSON 时以 2xx 为成功，JSON 可选读取 `ok=false`；
- 错误只保存截断后的安全文本，不保存完整响应头或 secret URL；
- 标题与内容按渠道限制截断；通知默认发送 TL;DR、前 5 个要点和报告相对链接，不发送完整长报告。

### 11.2 提交顺序

```text
transaction A:
  report = COMPLETED
  assistant message = ready
  job = COMPLETED
  research.completed event
commit

for each enabled channel:
  create delivery attempt
  HTTP send
  mark SENT/FAILED
  append notification.* event
```

任何通知异常都不能进入 transaction A，也不能修改 `research_jobs.status`。

## 12. 真人对话模式

### 12.1 适用矩阵

| 输出类型 | 应用真人模式 | 原因 |
|---|---:|---|
| 普通 `policy=answer` 可见正文 | 是 | 用户要求的聊天体验 |
| `policy=clarify` 的普通可见问题 | 是 | 仍是用户对话 |
| 一行 JSON 控制头 | 否 | 机器协议 |
| `ask_user` tool call / questions JSON | 否 | 结构化交互协议 |
| `policy=propose_execution` 控制字段 | 否 | 执行边界 |
| 计划文档 artifact 正文 | 否 | 需要稳定 Markdown 文档 |
| 深度研究规划、中间 JSON、报告 | 否 | 结构化交付物 |
| Agent Runtime plan/decide/reflect | 否 | 严格 JSON |
| 工具参数和工具结果 | 否 | 机器协议/审计 |
| 通知 TL;DR | 否 | 从报告确定性提取 |

不变量：真人模式只能降低“表达正式度”，不能降低权限、安全、事实、引用和协议约束。

### 12.2 后端注入位置

在 `LiveConversationModel.route_and_respond()` 构造第一条 system message 时读取一次 `human_mode` 快照，并把真人规则作为“user-facing body only”的附加段落插入。提示必须明确：

```text
- The JSON control header and tool protocol remain exact and take precedence.
- Apply this style only to the text after the control header.
- Never apply it to a plan_document artifact or research report.
- Use natural spoken Chinese, usually 1-2 bubbles, at most 3.
- Put [[next]] on its own line only at a real conversational pause.
- Do not emit Markdown headings, tables or report-style numbered lists in body.
```

不要给 `ModelGateway` 增加全局 style 参数，因为它也服务 `LiveRuntimeModel` 的 JSON 调用。不要在 `ControlHeadDecoder` 之后用规则重写正文；后处理会破坏流式 offset，也可能误改代码和引用。

开关在 Turn 开始模型调用时固定。本次生成中途修改设置只影响下一 Turn，保证 retry/resume 使用同一风格。

### 12.3 存储语义

`[[next]]` 保存在同一条 `thread_messages.content` 中：

- 搜索、复制、导出时把它规范化为换行；
- 后端最多不强行切段，前端 `splitHumanBubbles()` 过滤空段并把第 3 段以后的内容合并进第 3 个气泡；
- artifact 和研究报告正文若出现该字符串，应按普通文本处理，不启用 HumanBubbles；
- 消息 API 增加 `presentation='standard'|'human_bubbles'`，在生成开始时固定保存，历史渲染不依赖当前全局开关。

因此 `thread_messages` 增加：

```text
presentation TEXT NOT NULL DEFAULT 'standard'
```

这解决了用户关闭开关后旧消息应如何显示的问题：每条消息按生成时的 presentation 渲染；全局开关只决定新消息。

### 12.4 前端动画

新增：

```text
frontend/src/components/HumanBubbles.tsx
frontend/src/humanBubbles.ts
```

`MessageRecord` 增加：

```text
presentation?: 'standard' | 'human_bubbles'
origin?: 'history' | 'live'
```

规则：

- `hydrateThreadMessages()` 把快照消息标记为 `origin='history'`；
- `message.started` 创建的消息标记 `origin='live'`；
- snapshot 恢复不能把已有 live message 误改成 history；页面完整刷新后的所有既有消息都立即展开；
- 是否播放动画由 `origin === 'live'` 决定，不只看 `streaming`；短回复一次性到达也要逐条展示；
- 流式未结束时最后一个尚未闭合的气泡不提前揭示；完成后揭示剩余内容；
- 第一条约 700–900 ms，后续按字符数计算，单条延时上限 3 秒；
- `prefers-reduced-motion` 时立即显示；
- typing indicator 使用 `role=status`/可读 label，不能反复播报每个 token；
- React key 使用 `message.id + segment index`，流式追加不能重置已展示气泡。

`ConversationThread` 只在 assistant 且 `presentation='human_bubbles'` 时渲染 HumanBubbles，否则保持现有 `presentMessage + MarkdownMessage`。

### 12.5 群聊的未来边界

本期所有请求按 direct channel 处理，不新增或持久化 `channel_kind`。未来群聊真正进入需求时，在请求上下文增加：

```text
ConversationChannelContext
  kind: direct | group
  participant_count
  mentioned_agent
  sender_role
```

届时响应策略再基于 `channel_kind + human_mode` 选择单聊/群聊提示。当前不创建只有一个实现的 `ResponseStylePolicy` 接口，不实现群内插话频率、@回复或成员画像。

## 13. 前端产品形态

### 13.1 对话内研究卡

研究占位 assistant message 上方显示阶段卡：

- 标题/主题；
- 当前阶段和简短 detail；
- 来源数、已完成章节数/总章节数；
- 取消按钮；
- 失败后的错误摘要、查看部分报告和重试；
- 完成后的“查看报告”和来源数。

卡片状态来自 `research.*` 事件，刷新后以 `GET /api/research/jobs/{id}` 恢复。不要从自然语言 detail 推断状态。

### 13.2 研究与定时任务页面

在现有页面结构中增加最小两个视图：

- 研究历史：按时间列出主题、触发方式、状态、来源数和完成时间；
- 定时任务：名称、规则、时区、下一次运行、启停、立即运行和最近状态。

任务详情的“运行历史”直接调用 `/schedules/{id}/jobs`。通知配置与真人模式开关可放设置抽屉，不建设独立管理后台。

## 14. 安全与隐私

### 14.1 外部内容

- 搜索结果、网页、本地笔记、通知响应都视为不可信数据；
- 研究 prompt 明确来源内容不能修改指令、工具权限或引用格式；
- HTML 转纯文本，移除脚本、样式、表单和隐藏元素；
- 所有外部 URL 在 DNS 解析前后和重定向后校验；
- 限制响应大小、连接数、超时、字符数和总来源数；
- 报告 Markdown 继续经过现有安全渲染路径，不允许原始 HTML 执行。

### 14.2 Secret

- 默认只从环境变量读取通知 secret 和搜索 API key；
- bootstrap、日志、事件、数据库错误和 API 都不返回 secret；
- 环境变量名做白名单格式校验；
- URL 日志只记录渠道类型和随机 channel ID，不记录 query/token；
- 测试通知和真实通知使用同一适配器与校验逻辑。

### 14.3 权限与副作用

- 手动深度研究属于用户明确授权的联网读取，不写外部系统；
- 创建/编辑 schedule 是持续副作用，必须由显式 API 操作或现有 `propose_execution` 执行预览授权，不从普通回答静默创建；
- 通知只在 schedule 的 `notify_enabled` 与 channel 的 `enabled` 同时为真时发送；
- 通用 webhook 的 payload 不含来源全文、本地路径或模型内部数据。

## 15. 配置

配置继续使用 `AppConfig`/环境变量，首版建议：

```text
RESEARCH_SEARCH_BASE_URL
RESEARCH_SEARCH_API_KEY_ENV=RESEARCH_SEARCH_API_KEY
RESEARCH_MAX_SECTIONS=6
RESEARCH_MAX_QUERIES=12
RESEARCH_SEARCH_CONCURRENCY=4
RESEARCH_FETCH_CONCURRENCY=4
RESEARCH_DISTILL_CONCURRENCY=4
RESEARCH_FETCH_TOP_N=20
RESEARCH_MIN_SOURCE_CHARS=300
RESEARCH_SOURCE_MAX_CHARS=20000
RESEARCH_MAX_EVIDENCE=80
RESEARCH_REFLECTION_ROUNDS=1
RESEARCH_JOB_TIMEOUT_SECONDS=1800
RESEARCH_LEASE_SECONDS=30
RESEARCH_MAX_ATTEMPTS=2
RESEARCH_SCHEDULER_POLL_SECONDS=30
ALLOW_PRIVATE_WEBHOOKS=false
```

配置解析时设置合理上下限，避免用户通过环境变量创建无限并发或无限提示。不要为这些参数建设首版 UI；只有 topic、来源范围、schedule 和通知开关进入产品表单。

## 16. 文件改动规划

### 16.1 后端新增

```text
backend/app/research/
  models.py             # dataclass 与序列化
  engine.py             # 纯 async generator 编排
  planner.py
  retriever.py
  distiller.py
  reflector.py
  curator.py
  writer.py
  citations.py
  service.py            # Job/report/source persistence 与事件镜像
  worker.py             # claim、lease、heartbeat、cancel、recovery
  scheduler.py          # next_run_at 与 occurrence claim

backend/app/notifications.py
backend/app/settings.py

backend/tests/test_research_engine.py
backend/tests/test_research_service.py
backend/tests/test_research_worker.py
backend/tests/test_research_api.py
backend/tests/test_research_scheduler.py
backend/tests/test_notifications.py
backend/tests/test_human_mode.py
```

小模块按研究阶段拆分是因为每阶段有独立输入/输出和可测降级，不是为了插件化。通知只有一个文件和四个函数。

### 16.2 后端修改

```text
backend/app/db.py             # 新表、索引、检查约束和两列迁移
backend/app/runtime.py        # 组装 research service/worker/scheduler
backend/app/startup.py        # 构建检索器和通知服务
backend/app/main.py           # lifespan 启停顺序
backend/app/api.py            # research/schedule/notification/settings routes
backend/app/conversation.py   # V3 research policy，消息 presentation
backend/app/live_model.py     # 可见正文限定的 human prompt
backend/app/config.py         # 有界配置
backend/app/events.py         # 仅在需要时增加事件 data 校验，不建第二套 store
```

### 16.3 前端新增与修改

```text
frontend/src/components/HumanBubbles.tsx
frontend/src/components/ResearchProgressCard.tsx
frontend/src/pages/ResearchPage.tsx
frontend/src/pages/SchedulesPage.tsx
frontend/src/humanBubbles.ts

frontend/src/types.ts
frontend/src/api.ts
frontend/src/hooks/useThreadTelemetry.ts
frontend/src/components/ConversationThread.tsx
frontend/src/pages/ChatPage.tsx
frontend/src/App.tsx
frontend/src/styles.css

frontend/src/__tests__/HumanBubbles.test.tsx
frontend/src/__tests__/ResearchProgressCard.test.tsx
frontend/src/__tests__/researchApi.test.ts
frontend/src/__tests__/threadTelemetryResearch.test.ts
```

## 17. 分阶段实施顺序

### Phase 1：研究内核与离线验收

- 数据类、规划 fallback、来源过滤、提炼、反思上限、大纲校验、引用拼装；
- 使用 fake model/fake retriever 跑完整引擎；
- 验证来源失败降级、零证据失败、未知引用拒绝和取消。

完成标准：不依赖 FastAPI/SSE 即可从固定输入得到确定性的带引用报告事件流。

### Phase 2：Job、Worker 与 Thread 集成

- 新表和迁移；
- `ResearchService`、`ManagedResearchWorker`；
- 显式 research API；
- `research.*` 事件、assistant 占位消息、SSE 恢复；
- Job 详情、取消和新 Job 重试。

完成标准：断开浏览器、终止 Worker 并重启后，Job 能恢复且不重复完成消息。

### Phase 3：Web 检索与报告 UI

- 安全 Web Search/Fetch；
- 研究进度卡、报告详情、来源列表；
- 历史页。

完成标准：真实联网场景在来源局部失败时仍能产出可点击、引用一致的报告。

### Phase 4：定时任务与通知

- schedule schema、计算、scheduler、run now 和历史；
- 四个通知适配器、环境 secret 引用、测试发送、delivery 记录；
- 启动补跑一次与重复心跳幂等测试。

完成标准：同一 occurrence 在并发心跳/重启测试中只生成一个 Job，通知失败不改变报告完成状态。

### Phase 5：真人模式

- settings API、提示注入、message presentation；
- `[[next]]` 拆分、动画和 reduced-motion；
- 标准输出/结构化输出隔离测试。

完成标准：真人模式下普通回答最多 3 气泡；控制头、ask、计划文档、研究报告和 Runtime JSON 的现有测试全部不回归。

### Phase 6：自然语言研究路由

- ControlHead V3 `start_research`；
- 普通 Turn 与显式 research endpoint 统一到 `ResearchService.create_job()`；
- 自然语言路由评测和误触发测试。

把这一步放最后是为了先让研究领域独立稳定，避免同时调试路由协议和长任务引擎。

## 18. 测试与验收

### 18.1 后端关键测试

- 规划 JSON 损坏时 fallback 合法且查询有界；
- URL/内容去重、域名配额、质量排序稳定；
- 单源提炼的 Evidence 永远绑定该 source；
- 无证据不能完成报告；
- 反思最多一次、补充查询最多 3 条且不重复；
- curator 返回越界 ID 时被过滤；
- 未知引用阻止 finalizing；
- Job claim 在并发调用下只有一个 owner；
- 心跳丢失后旧 Worker 无法提交；
- 过期租约被新 Worker 接管；
- writing 恢复不重写完成章节；
- cancel 在检索、模型调用和写作中生效；
- source turn、occurrence、run-now 和 retry 幂等键分别生效；
- scheduler 跨 DST 使用 IANA timezone 算出唯一下一时刻；
- 停机错过多次只补跑一次；
- 渠道 HTTP 200 + 业务错误被记为失败；
- 通知异常后 Job/Report 仍为 `COMPLETED`；
- secret 不出现在 API、事件或日志捕获中；
- human mode 只进入 conversation visible-body prompt；
- 控制头解析、ask tool、plan artifact 与 Runtime JSON 回归通过。

### 18.2 前端关键测试

- 事件缺口后恢复研究卡和消息快照；
- 重复事件不重复追加来源、章节或完成状态；
- 历史 human message 立即显示全部气泡；
- live 短回复即使非 streaming 也逐条展示；
- 流式追加不重置已显示段；
- 超过 3 段合并到第三段；
- standard/plan/research message 不误用 HumanBubbles；
- reduced-motion 无延时；
- 刷新后完成报告、部分失败报告和 schedule 历史一致。

### 18.3 端到端验收场景

1. 手动研究一个主题，关闭页面后重新打开，看到继续推进并得到报告；
2. 检索 10 个来源，其中部分超时，最终报告只引用成功且已提炼的来源；
3. 研究中强制结束进程，租约过期后重启，只有一条最终 assistant 报告消息；
4. 同一 schedule occurrence 被两次扫描，数据库只有一个 Job；
5. Server酱返回 HTTP 200 但业务 code 非 0，报告完成、delivery 失败且可手动重试；
6. 开启真人模式，普通回答分 1–3 个口语气泡；随后创建计划和深度研究，两个文档仍是标准 Markdown；
7. 关闭真人模式，新消息标准显示，旧真人消息仍按其 `presentation` 正确展示且刷新不重播动画。

## 19. 可观测性与保留策略

首版使用结构化应用日志和 SQLite 查询，不引入监控平台。日志最少包含：

```text
job_id, schedule_id?, thread_id, phase, attempt, duration_ms,
source_count, evidence_count, section_count, error_kind
```

不记录 source content、Evidence text、完整报告、用户 secret 或完整 webhook URL。

建议默认保留：

- Job、最终报告和来源元数据：用户删除前保留；
- 抓取正文和 Evidence：90 天后可清理，但完成报告和来源引用元数据保留；
- 失败 partial markdown：30 天；
- delivery 错误：30 天；
- append-only thread events：沿用项目统一策略，不由研究模块单独删除。

清理不是首版阻塞项；达到本地 DB 体积阈值后再实现确定性 cleanup command。

## 20. 升级触发器

只有监测到以下情况之一，才重新评估方案 A 的单机边界：

- 待运行研究 Job 持续超过 20 个或 P95 排队超过 5 分钟；
- 单次研究经常超过 30 分钟，应用 shutdown 无法在合理时间完成；
- SQLite `database is locked` 在正常负载持续出现；
- 需要两个以上 API/Worker 进程同时运行；
- 通知需要跨重启自动保证投递；
- 定时任务达到数千条或需要秒级触发；
- 本地研究正文使数据库持续超过可接受体积。

届时优先按问题升级：并发不足才拆 Worker/队列，SQLite 写竞争才评估 PostgreSQL，通知保证才增加 delivery worker。不要一次性迁移整套基础设施。

## 21. 评审清单

- [ ] `ResearchEngine` 与传输、持久化、通知完全解耦；
- [ ] 研究 Job 没有被塞进 `AgentRuntime` 或普通 `turn_jobs`；
- [ ] 所有手动/定时/立即/重试入口都有唯一幂等键；
- [ ] 状态、阶段、租约和取消语义无歧义；
- [ ] 引用在 Evidence 阶段绑定，finalizing 拒绝未知引用；
- [ ] 事件只含摘要和资源 ID，不含大正文或 secret；
- [ ] Thread SSE 与消息 offset/generation 机制被复用；
- [ ] schedule 计算以 UTC 持久化、IANA timezone 展示和推进；
- [ ] 通知失败不回滚已完成报告；
- [ ] 真人模式只作用于可见聊天正文；
- [ ] 历史/实时消息的动画依据明确，不依赖 `streaming` 单一字段；
- [ ] 群聊保持非目标，没有提前建立插件化策略体系；
- [ ] 未引入 Redis、Celery、PostgreSQL、ORM、LangChain 或 MCP；
- [ ] 新依赖为零，复用 Python 标准库和现有 `httpx`；
- [ ] 每个阶段都有可执行测试和明确完成标准。

## 22. 最终决策摘要

Comet v0.0.4 已经证明了“证据先行的多阶段研究 + 同一引擎同时服务在线和定时执行 + 完成后降级通知 + 真人气泡表达”的产品价值。Better Agent 不需要复制其 Redis/Celery/PostgreSQL 运行环境，因为现有 SQLite WAL、租约 Worker、append-only Thread Events 和 SSE 游标已经提供了本机可靠执行所需的核心原语。

本设计因此选择最短且可演进的路径：新增一个独立研究领域，复用现有可靠性机制；把定时任务简化成“按时幂等创建研究 Job”；把历史定义为 Job 查询；把通知定义为报告完成后的独立副作用；把真人模式严格限制在普通聊天可见正文。它满足当前单用户本地 Agent 的真实需求，也保留了达到可测瓶颈后逐项升级的空间。

## 23. 源码追溯

本设计对照的 Comet v0.0.4 关键文件：

| 主题 | 文件 |
|---|---|
| 研究总编排、事件流、报告拼装 | `api/app/core/agent/research/engine.py` |
| 阶段间结构化模型 | `api/app/core/agent/research/models.py` |
| 规划、检索、提炼 | `planner.py`、`retriever.py`、`distiller.py` |
| 反思、大纲、写作 | `reflector.py`、`curator.py`、`writer.py` |
| 在线后台消费与重连快照 | `api/app/services/research_service.py` |
| 定时扫描、研究执行、完成通知 | `api/app/tasks/agent_task.py` |
| 任务表与“报告即历史”关联 | `api/app/models/agent_task_model.py` |
| 四渠道 payload 与业务响应校验 | `api/app/core/notify/pusher.py` |
| 真人模式提示 | `api/app/core/agent/prompts/human_style.jinja2` |
| 多气泡动画与历史即时展示 | `web/src/pages/chat/HumanBubbles.tsx`、`types.ts` |

Better Agent 的目标接入文件：

| 现有能力 | 文件 |
|---|---|
| SQLite WAL、事务与 schema | `backend/app/db.py` |
| Turn Job 租约、心跳、取消与消息生成 | `backend/app/conversation.py` |
| append-only Thread Event 与单调游标 | `backend/app/events.py` |
| Thread API、SSE 与 `Last-Event-ID` | `backend/app/api.py` |
| lifespan Worker 管理 | `backend/app/main.py` |
| Runtime 组件组装 | `backend/app/runtime.py`、`backend/app/startup.py` |
| 控制头与可见正文分离 | `backend/app/live_model.py` |
| 前端事件缺口和消息快照恢复 | `frontend/src/hooks/useThreadTelemetry.ts` |
| 消息渲染入口 | `frontend/src/components/ConversationThread.tsx` |
