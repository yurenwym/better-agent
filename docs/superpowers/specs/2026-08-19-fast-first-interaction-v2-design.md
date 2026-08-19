# 快速首响应与会话—执行分层：V2 开发设计

> 状态：开发设计，待用户评审
>
> 日期：2026-08-19
>
> 适用基线：`165c77a` 及其之前的 Better Agent V1 实现
>
> 与原方案关系：本文取代 `2026-08-18-fast-first-interaction-design.md` 作为后续实现依据；原文档保留为决策历史。

## 1. 结论

V2 采用“会话优先、明确执行才升级”的交互架构：

1. 普通问答、攻略、说明、比较和内容型计划直接流式回答。
2. 内容覆盖 7 天、6 周或一年，不等于 Agent 要运行同样长的时间。
3. 不增加通用的“转为目标”“仅保存方案”按钮。
4. 只有用户明确要求持续跟踪、调用工具、修改外部状态或产生副作用时，系统才展示该任务专属的“继续执行 / 修改方案”。
5. 用户确认后才创建 `Goal`、`Run` 和完整 `PlanVersion`，并进入现有规划、审批、ReAct、Checkpoint 和反思流程。
6. `AWAITING_DIRECTION` 属于会话 Turn，不加入 Agent 执行状态机。
7. 首次模型调用同时完成响应策略判断和第一版正文生成，不增加独立分类器调用。
8. 消息提交先持久化，再返回 `202 Accepted`；后台处理必须可恢复、幂等、可取消，不能裸用 `asyncio.create_task()`。

这不是在现有 Run 上增加一条分支，而是在 Agent Runtime 前增加一层轻量 Conversation Runtime。对话负责回答和确认，Agent Runtime 只负责已经获得明确执行授权的工作。

## 2. 用户体验原则

### 2.1 长期内容不等于长期执行

以下请求都属于内容生成，默认直接回答：

- “帮我创建一份桂林 7 天旅游攻略。”
- “给我一份 6 周 Python 学习计划。”
- “写一个未来一年的职业成长路线。”
- “比较三种健身方案的优缺点。”

它们的输出可能描述较长时间，但系统只需生成一份内容，不需要在后台持续运行。

以下请求才属于执行候选：

- “按这份攻略持续跟踪准备进度，每天提醒我。”
- “帮我读取本地清单，按计划更新完成状态。”
- “确认后把结果写入我的旅行笔记。”
- “未来四周持续检查结果并提醒我调整。”

判断依据是系统是否需要持续执行、使用工具或产生外部影响，而不是文本主题或时间跨度。

### 2.2 默认先提供价值

用户发送后依次看到：

```text
本地立即追加用户消息
→ 显示与当前请求相关的轻量状态
→ 第一段有用正文开始流式出现
→ 继续补全回答，或在确有执行需求时展示任务专属确认
```

“已收到”只解决感知反馈，不能代替有用结果。核心指标是首个有用正文出现时间，而不是 HTTP 返回时间或供应商首 Token 时间。

### 2.3 不诱导升级

普通回答结束后不显示“转为目标”。用户可以自然地继续追问、补充预算或修改偏好。只有新消息本身明确提出执行要求时，才进入执行预览。

## 3. 当前实现诊断

### 3.1 消息提交同步等待两次模型调用

当前前端在 `frontend/src/pages/ChatPage.tsx:53-70` 中同步等待 `sendMessage()`；后端 `backend/app/api.py:84-104` 又同步等待 `AgentRuntime.handle_message()`。

`backend/app/runtime.py:253-285` 在首轮依次执行：

1. `needs_clarification()`；
2. 转入 `PLANNING`；
3. `plan()`；
4. 创建完整 `PlanVersion`；
5. 转入 `AWAITING_APPROVAL`；
6. 最后返回 HTTP 响应。

因此“创建桂林 7 天攻略”也会先完成澄清和完整计划，用户才看到审批状态。

### 3.2 会话与执行领域被绑定

`backend/app/db.py:10-105` 中：

- `sessions.goal_id` 非空；
- `interactions.run_id` 非空；
- `messages.run_id` 非空；
- `events` 同时要求 `run_id` 和 `goal_id`。

这意味着普通问答也必须伪装成 Goal/Run。若只增加 `AWAITING_DIRECTION`，会继续混淆“本轮回答状态”和“Agent 执行状态”，并造成终态后的多轮会话、多个目标和直接回答难以表达。

### 3.3 现有流式内容不是用户正文

`backend/app/live_model.py:61-90` 的澄清和规划都要求 JSON；`backend/app/runtime.py:786-885` 却把所有模型调用的原始 delta 写入可见消息。

前端 `frontend/src/conversation.ts:88-106` 只能在 JSON 字段逐渐完整后猜测可见摘要。因此供应商已经返回首 Token，不代表用户已经看到第一段有用内容。

`model.first_token` 也在完整调用返回后的 `_record_model_response()` 中补记，不能作为端到端首响应指标。

### 3.4 快速返回会暴露新的状态同步问题

审批卡片由 `frontend/src/pages/ChatPage.tsx:106-115` 的 `run.state` 决定。现有 SSE Hook 更新事件、消息和统计，但不会更新页面持有的 `run` 对象。若 POST 改为后台处理而不重构状态归属，任务已经进入等待确认，按钮仍可能不出现。

### 3.5 状态、消息和事件存在半提交窗口

当前状态更新、消息内容更新和事件追加分别开启事务。例如 `_set_run_fields()`、`EventStore.append()`、`_append_model_delta()` 分别提交。进程若在两次提交之间退出，可能出现：

- 状态已变但没有事实事件；
- 消息已追加但客户端无法通过事件获知；
- 事件已存在但对应读取模型尚未更新。

后台化会放大这个问题，V2 必须先提供同事务的 Unit of Work。

## 4. 子智能体评审结论

本设计使用三个只读子智能体并行分析，再由主智能体交叉核验：

| 角色 | 重点 | 主要结论 |
|---|---|---|
| 代码链路审计 | Runtime、API、SSE、前端状态与测试 | 当前确有两次模型调用；直接扩展 `_json()` 会因 repair 破坏单调用承诺；裸后台任务不可恢复 |
| GitHub 对标 | 热门对话与 Agent 项目的一手实现 | 成熟项目普遍区分会话和 Run、使用乐观消息、语义事件、显式停止、持久 pause/resume |
| 架构红队 | 状态机、崩溃、幂等、取消和事务 | 五分类不互斥；`AWAITING_DIRECTION` 不应加入 Run；状态与事件必须原子提交 |

三方共同否决了“只给现有 Run 增加一个状态”的方案，并共同推荐 Conversation Turn 与 Agent Run 分层。

## 5. GitHub 对标

热度通过 GitHub REST API 于 2026-08-19 核验。Star 仅作为社区活跃度背景，不作为采用架构的充分理由。

| 项目 | 约 Star | 相关机制 | 本项目借鉴 | 不照搬 |
|---|---:|---|---|---|
| [Dify](https://github.com/langgenius/dify) | 152.9k | Question Classifier、Human Input、SSE 工作流 | 分类结果只决定流程建议；人工确认是持久暂停点 | 不引入其工作流平台、RAG 或多服务部署 |
| [Open WebUI](https://github.com/open-webui/open-webui) | 149.2k | 本地乐观消息、任务 ID、显式停止、重连 | 用户消息先本地落屏；停止对应服务端任务 | 不复制其大型聊天产品状态和插件体系 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 40.0k | Durable Execution、Interrupt、Checkpoint | 执行确认后才进入可暂停恢复的 Run | 不替换现有薄 Runtime，不增加框架依赖 |
| [CopilotKit](https://github.com/CopilotKit/CopilotKit) / [AG-UI](https://github.com/ag-ui-protocol/ag-ui) | 36.8k / 15.4k | 生命周期事件、消息 start/delta/end、HITL | 采用一个小型、带 ID 的语义事件子集 | 不引入完整 AG-UI 兼容层和生成式 UI |
| [Vercel AI SDK](https://github.com/vercel/ai) | 26.3k | submitted/streaming/ready/error、Data Stream、stop/resume 区分 | 明确消息阶段；网络断线不等于用户停止 | 不引入 Node 服务或 Redis resumable-stream |
| [Chainlit](https://github.com/Chainlit/chainlit) | 12.4k | 流式消息、按钮式 AskAction | 执行候选使用明确按钮，不要求用户输入“继续” | 不采用其应用框架 |

关键一手资料：

- [Vercel AI SDK Stream Protocol](https://github.com/vercel/ai/blob/main/content/docs/04-ai-sdk-ui/50-stream-protocol.mdx)
- [Vercel AI SDK Resumable Streams](https://github.com/vercel/ai/blob/main/content/docs/04-ai-sdk-ui/03-chatbot-resume-streams.mdx)
- [Vercel AI SDK Stop 与 Resume 的区别](https://github.com/vercel/ai/blob/main/content/docs/09-troubleshooting/15-abort-breaks-resumable-streams.mdx)
- [AG-UI Events](https://github.com/ag-ui-protocol/ag-ui/blob/main/docs/concepts/events.mdx)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [Open WebUI Chat 实现](https://github.com/open-webui/open-webui/blob/main/src/lib/components/chat/Chat.svelte)
- [Chainlit Streaming](https://docs.chainlit.io/advanced-features/streaming)
- [Chainlit AskAction](https://docs.chainlit.io/api-reference/ask/ask-for-action)

## 6. 方案比较

### 6.1 方案 A：在现有 Run 上增加状态

新增 `ANSWERING`、`AWAITING_DIRECTION` 和回到 `RECEIVED` 的循环转换。

优点：表面改动较少。

缺点：

- Run 同时代表会话和执行；
- 普通问答仍创建 Goal；
- 一个会话只能尴尬地复用一个终态 Run；
- 后续状态、Checkpoint、统计和审批语义变得不可靠。

结论：不采用。

### 6.2 方案 B：Conversation Runtime + Agent Runtime 分层

普通消息先进入 Thread/Turn，由轻量 Conversation Runtime 生成回答或执行预览。只有明确执行并获得确认后，才创建 Goal/Run 交给现有 Agent Runtime。

优点：

- 领域边界清晰；
- 普通回答不消耗计划和 ReAct 预算；
- 保留现有 Agent 安全边界；
- 可独立测试首响应、重连、取消和执行升级。

缺点：需要增加会话表、会话事件和受管 Worker，并重构前端状态来源。

结论：采用。

### 6.3 方案 C：采用完整工作流或 Agent UI 框架

使用 LangGraph、AG-UI 或类似框架统一对话和执行。

优点：成熟的中断、恢复和协议生态。

缺点：超出 V1 的薄 Runtime、单进程和最小依赖边界，迁移风险显著。

结论：不在 V2 实施；只借鉴语义。

## 7. 响应策略

### 7.1 三种运行策略

模型只返回三个互斥策略：

| 策略 | 含义 | 后续行为 |
|---|---|---|
| `answer` | 用户要的是内容或解释 | 直接流式回答，Turn 完成 |
| `propose_execution` | 用户明确要求系统持续执行、调用工具或产生影响 | 流式展示短执行预览，Turn 等待任务专属选择 |
| `clarify` | 缺少对象、结果或关键安全条件，无法给出有用版本 | 说明合理假设，只问一个关键问题，Turn 完成 |

原文档中的 `goal`、`guide`、`long_term` 可保留为分析标签或 `content_shape`，但不控制权限、不进入状态机，也不决定是否创建 Goal。

### 7.2 代码拥有最终权限

以下字段不能由模型自由决定：

- 是否调用工具；
- 是否创建 Goal/Run/PlanVersion；
- 是否展示执行按钮；
- 按钮对应的服务端动作；
- 是否写入长期记忆；
- 是否绕过计划或 WRITE 审批。

`needs_user_choice` 和 `choice_kind` 由代码根据 `policy` 派生。模型可以给出 `reason_code` 和内容形态，但数值 `confidence` 只用于评测，不能作为安全授权。

### 7.3 路由示例

| 用户输入 | 策略 | 原因 |
|---|---|---|
| 创建桂林 7 天攻略 | `answer` | 生成一次内容即可 |
| 给我 6 周 Python 学习计划 | `answer` | 计划是交付内容，不是系统执行计划 |
| 按攻略每天提醒并更新准备清单 | `propose_execution` | 持续跟踪并修改状态 |
| 把确认后的攻略写入本地笔记 | `propose_execution` | 存在 WRITE 副作用 |
| 帮我把这个做好 | `clarify` | 缺少对象和完成标准 |

## 8. 推荐架构

```text
React Conversation UI
        │
        ├─ POST Turn → SQLite 原子持久化 → 202 Accepted
        │                                  │
        │                                  ▼
        │                         Managed Turn Worker
        │                                  │
        │                           route_and_respond
        │                                  │
        ◄──────── Thread SSE：语义事件 ────┤
        │                                  │
        │                    ┌─────────────┴─────────────┐
        │                    │                           │
        │                 answer                  propose_execution
        │                    │                           │
        │               Turn 完成                 等待任务专属确认
        │                                                │
        │                                      用户点击继续执行
        │                                                │
        │                                                ▼
        └────────────────────────────────────── AgentRuntime
                                                Goal / Run / Plan
                                                Approval / ReAct
                                                Checkpoint / Memory
```

### 8.1 ConversationService

职责：

- 创建和读取 Thread；
- 幂等接收 Turn；
- 保存用户和助手消息；
- 维护 Turn 状态；
- 处理执行方向选择；
- 在确认时调用 ExecutionMaterializer。

它不执行工具，也不修改 Agent Run 状态。

### 8.2 RouteAndRespondModel

职责：在一次逻辑 ModelInvocation 中同时给出策略头和用户正文。

该调用必须：

- 不暴露 Tool Schema；
- 不允许工具调用；
- 不使用当前 `_json()` 的结构修复二次调用；
- 传输失败可以由 ModelGateway 在同一逻辑调用内重试；
- 结构头非法时使用代码定义的安全失败消息，不再发起独立 LLM repair。

### 8.3 ManagedTurnWorker

职责：

- 领取已持久化 Turn Job；
- 维护租约和尝试次数；
- 调用 RouteAndRespondModel；
- 批量保存流式正文和事件；
- 响应持久化取消请求；
- 启动时恢复未开始的 Job，并处理被中断的流。

V2 仍然是单进程、并发度 1。SQLite Job Ledger 是本地持久工作记录，不引入 Redis、Celery 或外部消息队列。

### 8.4 ExecutionMaterializer

只处理已经处于 `AWAITING_DIRECTION` 的 Turn，并要求：

- `action=continue_execution`；
- Turn 版本匹配；
- 幂等键未被其他操作使用；
- Turn 的策略确为 `propose_execution`。

它在一个事务中创建或复用 Goal、Session、Run，保存 `source_turn_id` 关联，并追加 `execution.materialized`。随后现有 AgentRuntime 进入规划流程。

## 9. 数据模型

V2 增加独立会话表，暂不破坏现有 Agent 表：

### 9.1 `threads`

```text
id                  TEXT PRIMARY KEY
title               TEXT NOT NULL
version             INTEGER NOT NULL
active_turn_id      TEXT
next_event_seq      INTEGER NOT NULL
created_at          TEXT NOT NULL
updated_at          TEXT NOT NULL
```

Thread 不带 `long_term`、`goal_id` 或 `active_goal_id`。它只是连续会话容器。

### 9.2 `turns`

```text
id                  TEXT PRIMARY KEY
thread_id           TEXT NOT NULL
client_turn_id      TEXT NOT NULL
parent_turn_id      TEXT
status              TEXT NOT NULL
policy              TEXT
content_shape       TEXT
reason_code         TEXT
version             INTEGER NOT NULL
materialized_goal_id TEXT
materialized_run_id TEXT
direction_action    TEXT
direction_idempotency_key TEXT UNIQUE
created_at          TEXT NOT NULL
updated_at          TEXT NOT NULL
UNIQUE(thread_id, client_turn_id)
```

### 9.3 `turn_jobs`

```text
turn_id             TEXT PRIMARY KEY
status              TEXT NOT NULL
lease_owner         TEXT
lease_until         TEXT
attempts            INTEGER NOT NULL
cancel_requested_at TEXT
started_at          TEXT
finished_at         TEXT
last_error_json     TEXT
```

### 9.4 `thread_messages`

```text
id                  TEXT PRIMARY KEY
thread_id           TEXT NOT NULL
turn_id             TEXT NOT NULL
role                TEXT NOT NULL
content             TEXT NOT NULL
status              TEXT NOT NULL
generation          INTEGER NOT NULL
content_length      INTEGER NOT NULL
created_at          TEXT NOT NULL
completed_at        TEXT
```

### 9.5 `thread_events`

沿用现有事件 Envelope，但以 `thread_id + seq` 排序，并显式包含 `turn_id`。Thread 事件和 Run 事件分别维护游标；`execution.materialized` 提供二者的关联。

### 9.6 现有表

`goals`、`sessions`、`runs`、`plan_versions`、`approvals`、`checkpoints`、`tool_calls` 和现有 Run 事件继续由 AgentRuntime 使用。

普通回答不得在这些表中产生记录。

## 10. 状态机

### 10.1 Turn 状态

```text
ACCEPTED
  → ROUTING
  → STREAMING
      ├─→ COMPLETED
      ├─→ AWAITING_DIRECTION
      ├─→ FAILED
      └─→ CANCELLED

AWAITING_DIRECTION
  ├─→ COMPLETED             用户放弃或修改为普通回答
  ├─→ MATERIALIZING
  │     ├─→ COMPLETED       Goal/Run 已幂等创建
  │     └─→ FAILED
  └─→ CANCELLED
```

`clarify` 在问出关键问题后完成当前 Turn。用户补充信息会创建一个带 `parent_turn_id` 的新 Turn，而不是让旧 Turn 反向变回处理中。

### 10.2 Agent 状态

现有 AgentState 保持：

```text
RECEIVED → CLARIFYING / PLANNING → AWAITING_APPROVAL
→ EXECUTING → AWAITING_OUTCOME / REFLECTING → COMPLETED
```

V2 不向该枚举添加 `ANSWERING`、`DIRECT` 或 `AWAITING_DIRECTION`。

## 11. 单次路由—正文协议

模型输出采用一个受限控制头和 Markdown 正文：

```text
{"v":1,"policy":"answer","content_shape":"travel_guide","reason_code":"content_only"}

## 桂林 7 天行程建议
……
```

服务端流解码器：

1. 最多缓冲 1 KiB 控制头；
2. 只接受单行 JSON 和已知字段；
3. 在控制头通过校验前不向用户发出正文；
4. 通过后立即发送 `turn.policy_decided` 和 `message.started`；
5. 后续字符原样作为 Markdown 正文流；
6. 控制头缺失、超长或非法时停止该 generation，输出代码定义的安全错误和重试入口；
7. 不把控制头、内部 Prompt 或原始结构化 JSON 保存为用户消息。

按钮完全由代码生成：

- `answer`：无按钮；
- `clarify`：无执行按钮，保留输入框；
- `propose_execution`：显示“继续执行 / 修改方案”。

## 12. API

### 12.1 会话 API

```text
POST   /api/threads
GET    /api/threads/{thread_id}
POST   /api/threads/{thread_id}/turns
GET    /api/threads/{thread_id}/messages
GET    /api/threads/{thread_id}/events
GET    /api/threads/{thread_id}/events/stream
POST   /api/turns/{turn_id}/cancel
POST   /api/turns/{turn_id}/direction
```

提交 Turn：

```json
{
  "client_turn_id": "client_uuid",
  "content": "创建一份桂林 7 天攻略",
  "skill_names": []
}
```

成功接收后立即返回：

```http
HTTP/1.1 202 Accepted
```

```json
{
  "thread_id": "thread_xxx",
  "turn_id": "turn_xxx",
  "status": "ACCEPTED",
  "event_cursor": 12
}
```

相同 `client_turn_id` 的重试必须返回同一 Turn，不得重复生成。

方向选择：

```json
{
  "action": "continue_execution",
  "expected_version": 3,
  "idempotency_key": "client_action_uuid"
}
```

### 12.2 Agent API

现有 `/api/runs/*`、计划、审批、预算、工具和导出 API 保留。前端收到 `execution.materialized` 后再订阅对应 Run 的事件流。

## 13. 语义事件与流式恢复

V2 使用一个最小事件集合：

```text
turn.accepted
turn.started
turn.policy_decided
message.started
message.delta
message.snapshot
message.completed
turn.awaiting_direction
turn.direction_selected
turn.cancel_requested
turn.cancelled
turn.failed
turn.completed
execution.materialized
```

`message.delta` 包含：

```json
{
  "message_id": "message_xxx",
  "generation": 1,
  "offset": 128,
  "delta": "第三天前往龙脊梯田……"
}
```

客户端只在 `generation` 相同且 `offset` 等于当前内容长度时追加。若发现缺口、重复或 generation 变化，则获取 `message.snapshot` 或消息 REST 快照，不盲目拼接。

模型碎片按 25～50 ms 或 64～256 个字符批量提交，避免逐 Token 开启 SQLite 事务。每批消息更新和对应事件必须在同一事务中提交。

## 14. 事务与幂等

### 14.1 原子接收

以下操作在一个事务中完成：

- 插入或读取幂等 Turn；
- 插入用户消息；
- 插入 Turn Job；
- 更新 Thread 版本和活跃 Turn；
- 追加 `turn.accepted`。

只有事务提交后 API 才返回 `202`。

### 14.2 原子状态与事件

新增 `UnitOfWork` 或允许 `EventStore.append(connection=...)`。任何领域状态改变必须和事实事件同事务提交。统计投影可以在提交后幂等重建，不属于授权事实。

事件序号不得继续依赖无保护的 `MAX(seq)+1`。Thread 和 Run 应维护可原子递增的下一序号，或在唯一约束冲突时进行受限重试。

### 14.3 Job 领取

Worker 使用状态比较并交换领取 Job。启动时：

- `QUEUED`：自动继续；
- 租约过期但尚未输出正文：可以重新领取；
- 已输出部分正文：标记当前 generation 为 `interrupted`，保留部分内容并允许用户重试为新 generation；
- 已取消：不得恢复。

模型生成是无副作用操作，可以按 generation 重试；外部工具副作用仍由 AgentRuntime 管理。

## 15. 取消与断线

必须区分：

1. 浏览器刷新、页面跳转、SSE 断开：只是客户端暂时离线，后台 Job 继续，重连后补事件。
2. 用户点击“停止生成”：调用 `/api/turns/{turn_id}/cancel`，持久化取消请求并中断模型调用。
3. 用户点击“取消任务”：调用现有 Run Cancel，终止 Agent 执行。

Turn 取消后：

- 保留已生成部分；
- `message.completed.finish_reason=cancelled`；
- 追加 `turn.cancelled`；
- 不再发出成功完成事件；
- 重复取消返回当前终态，不报冲突。

若 WRITE 工具已被外部系统执行、但本地结果未提交，不能宣称 exactly-once。Run 应进入 `BLOCKED` 并记录 `outcome_unknown`，要求用户核对后恢复。

## 16. 前端设计

### 16.1 乐观消息

提交时立即用 `client_turn_id` 追加用户消息，状态为 `submitted`。服务端返回 canonical ID 后原位对账，不新增第二条消息。

助手消息状态统一为：

```text
submitted → streaming → ready
                      ↘ error
                      ↘ cancelled
```

不再同时显示“真实流式消息”和固定的“正在等待模型”助手卡片。只保留一个消息槽，并根据阶段改变状态文案。

### 16.2 决策 UI

- `answer`：只显示正文和普通输入框；
- `clarify`：正文包含一个问题，输入框保持可用；
- `propose_execution`：在正文下显示该任务专属的“继续执行 / 修改方案”；
- 不显示通用“转为目标”或“仅保存方案”。

“修改方案”聚焦输入框并让下一条消息引用当前 Turn，不直接修改旧消息。

### 16.3 状态来源

会话按钮和消息状态由 Thread/Turn Store 驱动，不能继续依赖滞后的 `ChatPage.run`。`execution.materialized` 到达后，前端获取并订阅 Run；执行轨迹仍由现有 Run Telemetry 展示。

## 17. 桂林 7 天攻略完整示例

用户输入：

```text
帮我创建一份桂林 7 天旅游攻略。
```

处理结果：

1. 本地立即显示用户消息；
2. Turn 持久化并返回 `202`；
3. Router 返回 `policy=answer`、`content_shape=travel_guide`；
4. 正文直接流式给出默认假设、7 天逐日安排、交通、住宿区域、预算和避坑提醒；
5. 对天气、门票、开放时间等实时事实注明出发前核验；
6. Turn 进入 `COMPLETED`；
7. 不创建 Goal、Run、PlanVersion、审批或工具调用；
8. 不显示执行按钮。

用户继续输入：

```text
预算控制在 5000 元，带两位老人，重新调整。
```

这是新的 `answer` Turn，基于会话历史直接修订攻略。

若用户输入：

```text
按这份攻略持续检查准备清单，每天提醒我，并在我确认后写入旅行笔记。
```

处理结果：

1. Router 返回 `propose_execution`；
2. 正文只展示执行范围、频率、假设、可能使用的工具和 WRITE 审批边界；
3. 显示“继续执行 / 修改方案”；
4. 未确认前不创建完整计划、不调用工具；
5. 确认后 Materializer 创建 Goal/Run；
6. AgentRuntime 生成详细计划并等待计划审批；
7. WRITE 工具仍需逐次审批。

## 18. 安全不变量

以下条件必须由代码和数据库约束，而不是 Prompt 保证：

1. `answer`、`clarify` 和未确认的 `propose_execution` 无 Tool Schema、无工具调用。
2. 未执行 `continue_execution` 前不存在对应 Goal/Run/PlanVersion。
3. LLM 不能直接写 Turn 或 Run 状态。
4. LLM 输出未知策略时不能进入执行路径。
5. 用户未批准计划前不得进入 `EXECUTING`。
6. WRITE 工具仍绑定 `run_id + tool_call_id + params_hash` 并逐次审批。
7. 会话回答不得自动创建或应用长期记忆。
8. 取消、失败、结构错误和网络断线都不得被记录为成功完成。

## 19. 指标

### 19.1 端到端指标

| 指标 | 定义 | V2 目标 |
|---|---|---:|
| `optimistic_visible_ms` | 点击发送到本地用户消息出现 | p95 ≤ 50 ms |
| `turn_ack_ms` | 请求发出到收到 `202` | 本地 p95 ≤ 150 ms |
| `time_to_first_useful_ms` | 点击发送到第一段用户可见正文 | p50 ≤ 2 s，p95 ≤ 5 s |
| `cancel_propagation_ms` | 点击停止到模型任务终止 | p95 ≤ 500 ms |
| `stream_gap_recoveries` | 通过快照修复的缺口数 | 可观测、最终内容一致 |
| `direct_run_leaks` | `answer/clarify` 错误创建 Run 的次数 | 0 |
| `preconfirm_tool_calls` | 确认前工具调用次数 | 0 |

模型首 Token 必须在 delta callback 第一次触发时记录。端到端首个有用结果由前端记录，两者不可混用。

### 19.2 路由质量

评测重点不是五分类准确率，而是：

- 内容请求被错误升级执行的比例；
- 明确执行请求未显示确认的比例；
- 模糊请求是否先给合理假设且只问一个问题；
- 任何误判是否仍被代码安全边界阻止产生副作用。

## 20. 测试策略

### 20.1 单元测试

- 控制头被任意字符边界拆分；
- Unicode、转义符和 Markdown 正文；
- 控制头超长、未知策略、缺字段和非法 JSON；
- generation、offset、重复 delta 和缺口恢复；
- Turn 和 Job 状态转换；
- 按策略派生按钮；
- 取消幂等。

### 20.2 后端集成测试

- POST 在模型完成前返回 `202`；
- 相同 `client_turn_id` 不重复生成；
- 事务失败不留下半个 Turn；
- queued Job 在重启后恢复；
- streaming Job 重启后保留部分内容并标记 interrupted；
- SSE 使用 `Last-Event-ID` 重连，无重复可见正文；
- `answer` 不创建 Goal/Run/Plan；
- 未确认的 `propose_execution` 不调用工具；
- 双击“继续执行”只创建一个 Goal/Run；
- 断开 SSE 不取消 Job；
- 显式停止会取消模型调用并产生正确终态。

### 20.3 前端测试

- 用户消息在网络请求完成前出现；
- canonical 消息对账不重复；
- submitted/streaming/ready/error/cancelled 状态正确；
- answer 无通用目标按钮；
- propose_execution 才显示任务专属按钮；
- Thread 事件到达后按钮出现，不依赖同步 POST 返回；
- materialized 后正确切换到 Run Telemetry；
- 流缺口触发快照获取而非盲目追加。

### 20.4 模型评测案例

至少覆盖：

- 普通知识问答；
- 桂林 7 天攻略；
- 6 周学习计划；
- 明确持续提醒；
- 明确 READ/WRITE 工具请求；
- 模糊指代；
- Prompt Injection 要求绕过确认；
- 用户在执行预览后修改方案；
- 用户取消流式回答；
- 低质量或非法控制头。

## 21. 实施阶段

### 阶段 0：指标与事务基础

- 增加端到端 Ack、首个可见正文和取消指标；
- 提供状态/消息与事件同事务写入能力；
- 修正模型首 Token 记录时点。

### 阶段 1：Conversation 数据与快速接收

- 增加 Thread、Turn、Job、Message、Event 表；
- 增加受管 Worker 和 FastAPI lifespan；
- 消息 API 改为持久化后返回 `202`；
- 前端实现乐观用户消息和 Thread SSE。

### 阶段 2：单次 route_and_respond

- 实现控制头/正文流解码器；
- 实现三种策略和语义事件；
- 禁止 Router 使用工具和 JSON repair；
- 实现批量 delta 与快照恢复。

### 阶段 3：任务专属执行确认

- 实现 `AWAITING_DIRECTION` Turn 状态；
- 实现方向 API 和 ExecutionMaterializer；
- 将 materialized Run 接入现有计划、审批、SSE 和轨迹。

### 阶段 4：迁移与评测

- 更新前端信息架构和旧 API 调用；
- 增加确定性回放和 live 模型评测；
- 验证重启、重复提交、取消、断线和 WRITE 未知结果场景；
- 保留旧表数据的只读兼容，稳定后再决定是否合并消息模型。

## 22. 验收标准

- 桂林 7 天攻略直接流式返回完整可用内容。
- 普通回答不创建 Goal、Run、PlanVersion、审批或 ReAct 预算。
- 普通回答末尾不显示“转为目标”或类似通用按钮。
- 只有明确执行请求显示任务专属“继续执行 / 修改方案”。
- 用户确认前不创建完整计划、不调用工具、不产生副作用。
- 用户消息在本地 50 ms 目标内出现，消息 API 在本地 p95 150 ms 内完成持久化 Ack。
- 首个有用正文无需等待整个 `handle_message` 返回。
- SSE 断线可重连，显式停止可取消，二者语义不混淆。
- 重复提交、双击确认和进程重启不产生重复 Turn 或 Run。
- 消息更新和事实事件不存在半提交窗口。
- 所有策略、消息、方向选择和执行关联可审计。
- 不引入 RAG、多 Agent、MCP、Shell、Redis、Celery、微服务或工具并行。

## 23. 明确不做

- 不增加通用“转为目标”或“保存方案”按钮；
- 不把攻略、学习计划等内容型回答标记为长期会话；
- 不增加独立顶层 Plan 产品实体；
- 不用五类意图直接驱动状态和权限；
- 不把 `AWAITING_DIRECTION` 加入 AgentState；
- 不采用完整 LangGraph、AG-UI、Dify 或 Chainlit 运行时；
- 不实现多 Turn 并行、消息排队或多用户协作；
- 不实现供应商原生流跨进程无缝续传；V2 通过持久化消息批次和快照恢复客户端视图。

## 24. 最终设计判断

快速首响应问题的根因不是“模型没有尽快吐字”，而是系统把所有输入过早解释成可执行 Goal，并把内部结构化调用当成用户消息流。

V2 通过以下边界解决问题：

```text
内容请求 → 直接回答
明确执行 → 简短预览 → 用户确认 → Agent Runtime
信息不足 → 合理假设 + 一个问题
```

会话负责尽快提供价值，Agent Runtime 负责在获得授权后安全执行。两者通过显式、可审计的 materialization 事件连接，而不共享一套含义混杂的状态机。
