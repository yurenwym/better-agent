# Better Agent 目标执行与每日陪伴 V1.1 优化设计

> 状态：可进入实施；按纵向切片交付
>
> 日期：2026-08-24
>
> 评审对象：`2026-08-24-goal-execution-and-daily-companion-v1.1-design.md`
>
> 历史基线：`2026-08-17-personal-agent-v1-design.md` 仅用于核对演进边界，不是本方案的主要设计对象

## 1. 结论

V1.1 的产品方向成立：Better Agent 需要在可编辑的 `PlanDocument` 和一次性的 `Run` 之间增加一个跨日执行层，让用户每天知道该做什么，并能用完成、跳过、延期和反馈持续推进真实目标。

原方案不能直接实施的原因不是产品能力不够，而是首版同时承担了过多机制，并在几个关键边界上作出了当前代码无法兑现的承诺。优化后保留核心闭环：

```text
固定 PlanDocumentVersion
  -> 生成可确认的 DRAFT ProgramVersion
  -> 用户激活 GoalProgram
  -> 一次性创建有限周期 Action
  -> Today 展示今天与逾期行动
  -> 完成 / 跳过 / 延期 / 反馈
  -> 用户主动求助时进入 Conversation
  -> 用户主动要求调整时生成 Proposal
  -> 确认后只修改未来执行版本
```

首版不建设通用工作流、通用调度平台、模板/实例双层、滚动物化、自动复盘 Worker、外部提醒、Program SSE 或常驻 Run。SQLite、原生 `sqlite3`、现有 Runtime 和现有记忆系统保持不变，不引入 AgentScope 或新的 ORM/数据库。

## 2. 相对原 V1.1 的关键修订

| 原 V1.1 | 优化决策 | 原因 |
|---|---|---|
| `GoalProgram.goal_id` 可能关联 Runtime Goal | 删除该字段；Program 自己保存目标标题和摘要 | 当前 `goals` 与 Run 一起创建，是单次执行容器，不是跨日产品目标 |
| `PREVIEW` 混合生命周期与编译状态 | 生命周期使用 `DRAFT`；同步编译另用 `compile_status` | 避免一个状态同时表示产品状态、模型任务状态和失败恢复状态 |
| `goal_action_templates` + `instances` | 改为单表 `goal_actions` | 编译结果已是带具体日期的一次性行动，一模板一实例没有信息增益 |
| 每天物化 Action | 激活时一次性物化完整有限周期 | 1–28 天、最多 120 项可在一个短事务完成，省去每日生成器和补偿扫描 |
| 最长 90 天 × 每天 8 项 | 默认和首发上限 28 天、总行动不超过 120、每天不超过 6 | 避免生成 720 个重复 JSON 项导致延迟、质量和修复成本失控 |
| 点击“让 Agent 帮我”即创建 Run | 先创建关联 Turn，普通问题由 Conversation 回答；需要工具时再走现有执行方向与 Run | 保留用户方向确认、`source_turn_id`、审批和 Checkpoint |
| Action 单个 `linked_run_id` | 首版不存；需要真实工具场景后增加关联表 | 一个 Action 可能有多个 Turn、Run 或 Research Job，单字段会覆盖历史 |
| 接受调整时原子创建 ProgramVersion 与 PlanDocumentVersion | ProgramVersion 和未来 Action 单 SQLite 事务提交；Markdown 回写是显式操作或后续可恢复投影 | 当前 PlanDocument 需要 SQLite prepare、事务外文件写、SQLite finalize，不能跨文件原子提交 |
| 复用 NotificationService | 只复用渠道配置、SSRF 防护和 HTTP transport；提醒后置并独立建模 | 当前 delivery 外键和重试正文都绑定 `research_jobs/reports` |
| 自动 Daily/Weekly Review Worker | 后置；先由用户主动触发调整 | 先验证 Today 闭环，避免无证据的后台 LLM 成本和打扰 |
| Program SSE | 首版 mutation 后 refetch | 核心 mutation 是短事务，没有持续进度流；有后台 Job 或多窗口需求时再加 |
| DELETE 级联清理 append-only event | 普通删除改为 tombstone | 保留审计不变量；敏感反馈正文可单独清理 |

## 3. 已有代码能力与真实复用边界

### 3.1 可直接复用

- `PlanDocumentService`：不可变版本、内容 Hash、CAS、write-intent、文件投影和启动恢复。
- Conversation：Thread/Turn/Message、幂等提交、worker claim/lease、Ask、Thread events。
- `ExecutionMaterializer` 与 `AgentRuntime`：计划审批、工具审批、预算、Checkpoint、Run event 和恢复。
- Memory V2：entry/revision/proposal/episode/context pin；Goal 执行记录无需另建记忆库。
- SQLite：WAL、foreign keys、`BEGIN IMMEDIATE` 短事务和显式迁移。
- 通用 API 安全：owner scope、CSRF、409 冲突、安全错误响应。

### 3.2 只能复用模式或底层函数

- `PlanExecutionCompiler` 的模型网关和固定来源输入可借鉴，但它的 `PlanDraft` 不包含日期、用时、完成标准和里程碑，不能作为 Goal 编译协议。
- Research 的 claim/lease/heartbeat 可作为未来 durable compilation/review job 的参考，不能直接复制其领域表。
- `ScheduleService.next_run()` 的时区算法可提取为纯函数，`research_schedules` 和 `ScheduleService.tick()` 不能承载 Goal。
- Notification 的 channel 配置、URL 校验和 provider transport 可提取复用；`notification_deliveries`、`retry()`、`deliver_completed()` 仍属 Research。
- Thread/Run SSE 的 cursor 模式可借鉴，但 Program SSE 是尚未实现的新通道。

## 4. 领域边界与权威关系

```text
PlanDocumentVersion
  用户可查看、编辑的计划资产快照
          |
          | compile（固定 version + hash）
          v
ProgramVersion
  已确认执行安排的唯一事实源，不可变
          |
          | activate / adjust
          v
GoalProgram ------> GoalAction ------> ActionFeedback
  生命周期事实        每日执行事实          可选敏感反馈
       |                   |
       |                   +--> Conversation Turn
       |                              |
       |                              +--> 必要时 Run / Research Job
       |
       +-- 可选、可恢复投影 --> 新 PlanDocumentVersion

Memory
  只接收周期 Episode 或经用户确认的稳定偏好，不接收原始打卡流水
```

权威规则：

1. `PlanDocumentVersion` 回答“用户维护的完整计划正文是什么”。
2. `GoalProgram` 回答“这次执行周期当前处于什么状态”。
3. `ProgramVersion` 回答“当前生效的未来安排是什么”；换言之，ProgramVersion 是执行唯一权威。
4. `GoalAction` 回答“某项行动实际上发生了什么”，历史终态不可改写。
5. Conversation 是交互通道；Run 是一次受控执行；两者都不能反向伪造 Action 完成。
6. PlanDocument 的用户编辑不静默修改 active Program；Program 调整也不依赖 Markdown 投影成功。

## 5. 产品流程

### 5.1 从计划开始执行

1. 用户在计划详情点击“开始执行”。
2. 表单只收集确定性必需信息：开始日期、IANA timezone、每日可用分钟数；缺失的结束日期默认编译 28 天。
3. 后端固定当前 committed `PlanDocumentVersion` 与 `content_hash`，创建 `DRAFT + COMPILING` Program。
4. SQLite 事务外调用 `GoalProgramCompiler`。
5. 代码校验模型结果；成功时写入不可变 ProgramVersion 并置为 `READY`，失败时记录安全错误摘要并允许重试，不创建 Action。
6. UI 展示每天行动、总用时、假设和里程碑预览。
7. 用户确认激活；同一 SQLite 事务设置 current version、创建全周期 Action、写事件和幂等回执。

用户确认前，Program 不进入 Today、不允许打卡、不发送提醒、不创建 Run。

### 5.2 Today

服务端按每个 Program 固定 timezone 计算其 local date，聚合：

- 当日 `SCHEDULED` Action；
- 当日之前仍未结束的逾期 Action；
- 目标标题、当前第几天、今日预计时长和完成数；
- 若 Program 为 `PAUSED`，不展示为今日待办。

首版直接查询事实表，不通过事件重放或新投影表生成 Today。

### 5.3 完成、跳过与延期

- 完成、跳过、延期均为不调用 LLM 的短事务。
- 每个 mutation 使用 CSRF、owner scope、`expected_version` 和 `Idempotency-Key`。
- 完成立即持久化，不等待反馈或模型分析。
- 延期将原 Action 置为终态 `DEFERRED`，创建新的 `SCHEDULED` Action，并用 `deferred_from_action_id` 关联；同一幂等键只能创建一个替代项。
- `SKIPPED` 不等于完成；optional Action 不阻塞目标完成；被延期或被新版本取代的源 Action不进入完成率分母。
- 所有 required Action 均 `COMPLETED` 时只产生 `completion_ready`，由用户确认 Program `COMPLETED`，避免误判目标达成。

### 5.4 反馈

原方案要求“完成先落库，随后再可选反馈”，这意味着反馈拥有独立并发和隐私生命周期。采用 append-only `goal_action_feedback`：

- 字段包含 `kind`、`actual_minutes`、`difficulty`、`reason_code`、可选 `note`、`sensitivity` 和幂等键；
- 反馈事件只存 feedback ID、类别和计数，不复制自由文本；
- feedback 可按隐私要求单独清除，不删除 Action 状态事件；
- GoalContextProvider 对自由文本设条数、字符数和 token 上限。

### 5.5 暂停、恢复、完成和取消

```text
DRAFT -> ACTIVE <-> PAUSED
DRAFT -> CANCELLED
ACTIVE | PAUSED -> COMPLETED
ACTIVE | PAUSED -> CANCELLED
```

- `COMPLETED` 与 `CANCELLED` 都是终态，不能互相转换。
- 暂停只影响 Today 可执行性和未来提醒，不删除或重排已创建 Action。
- 首版恢复保持原日期；逾期 Action 单独分组。将所有未来行动从今天顺延属于后续受控调整，不塞入简单 resume。
- 取消将未来 `SCHEDULED` Action 标记为 `CANCELLED`，历史保留。
- 普通“删除”只设置 `deleted_at` 并从默认列表隐藏；不删除来源 PlanDocument、Thread、Run 或 Research Job。

### 5.6 遇到困难与 Agent 帮助

```text
request-help
  -> 创建或打开来源 Thread
  -> 提交带受信任 action_context_ref 的 Turn
  -> GoalContextProvider 按 owner 从数据库读取有限上下文
  -> 普通解释/鼓励/拆解：Conversation 直接回答
  -> 需要工具：模型提出 execution direction
  -> 用户确认方向
  -> 现有 ExecutionMaterializer / AgentRuntime / Approval / Checkpoint
```

客户端只能提交 Action ID，不能拼接 Program 内容冒充上下文。首版先完成 Conversation 帮助，不直接创建 Run；出现第一个真实工具用例后再增加 Action 与 Turn/Run/Research Job 的多值关联表。

## 6. 编译协议

输入固定：

```text
owner_id
source_thread_id
source_plan_document_id
source_plan_document_version_id
source_plan_content_hash
start_date
timezone
daily_minutes
requested_end_date（可选）
```

结构化输出：

```json
{
  "objective_title": "string",
  "objective_summary": "string",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "assumptions": ["string"],
  "milestones": [
    {"logical_key": "m1", "title": "string", "target_date": "YYYY-MM-DD"}
  ],
  "actions": [
    {
      "logical_key": "d1-a1",
      "scheduled_date": "YYYY-MM-DD",
      "position": 1,
      "title": "string",
      "description": "string",
      "estimated_minutes": 30,
      "completion_criteria": "string",
      "required": true
    }
  ]
}
```

首发硬限制：

- 周期为 1–28 天；无结束日取 28 天；
- 总 Action 1–120；每天 0–6 项；
- 单项 5–180 分钟；每日总时长不得明显超过用户预算，超出必须出现在 assumptions；
- `logical_key` 在一版内唯一，日期位于周期内，position 在同日唯一；
- 标题、说明、完成标准、假设和里程碑均有字符上限；
- 未知字段、非法日期、空任务、重复 key、越界数量全部拒绝；
- 结构修复最多一次；仍失败则 `compile_status=FAILED`，不产生半成品 Action；
- 不跨供应商 fallback，不让 LLM 决定激活、状态转换、权限或工具范围。

同步编译是有意的首版取舍：用户发起后页面需保持打开；失败可安全重试。只有 p95 超过产品超时、用户明确要求关闭页面后继续、或出现排队需求时，才增加独立 `goal_compilation_jobs`、claim、lease generation、heartbeat 与恢复 worker。

## 7. 最小数据模型

以下为契约级 schema；实施时写入新的不可变 migration，并为旧库升级增加测试。所有外键启用，状态字段使用 `CHECK`，日期格式和跨行归属由 service 校验。

```text
goal_programs
  id, owner_id
  source_thread_id, source_plan_document_id
  source_plan_document_version_id, source_plan_content_hash
  objective_title, objective_summary
  status: DRAFT|ACTIVE|PAUSED|COMPLETED|CANCELLED
  compile_status: COMPILING|READY|FAILED
  compile_error_code
  timezone, start_date, end_date
  current_program_version_id
  version, next_event_seq
  created_at, updated_at, completed_at, cancelled_at, deleted_at

goal_program_versions
  id, program_id, version, base_version_id
  source_plan_document_version_id
  structure_json, change_summary, actor, created_at
  UNIQUE(program_id, version)

goal_actions
  id, program_id, program_version_id, logical_key
  scheduled_date, position
  title, description, estimated_minutes, completion_criteria, required
  status: SCHEDULED|COMPLETED|SKIPPED|DEFERRED|CANCELLED
  version
  completed_at, skipped_at, deferred_at, cancelled_at
  deferred_from_action_id, cancel_reason
  created_at, updated_at
  UNIQUE(program_id, program_version_id, logical_key)

goal_action_feedback
  id, owner_id, action_id, kind
  actual_minutes, difficulty, reason_code, note, sensitivity
  idempotency_key, created_at
  UNIQUE(owner_id, idempotency_key)

goal_command_receipts
  id, owner_id, aggregate_type, aggregate_id, operation
  idempotency_key, request_hash, response_json, created_at
  UNIQUE(owner_id, idempotency_key)

goal_program_events
  row_id, event_id, program_id, seq, action_id
  type, actor, schema_version, occurred_at, data_json
  UNIQUE(event_id), UNIQUE(program_id, seq)
```

必须满足的数据库/服务不变量：

- `source_plan_document_version_id + hash` 必须指向 committed 且属于 source document 的版本；
- current ProgramVersion 必须属于同一 Program；Action 的 ProgramVersion 也必须属于同一 Program；
- Program/Action 状态使用条件 UPDATE 做 CAS；
- 相同幂等键与相同 request hash 返回缓存响应，不同 hash 返回 409；
- 领域变更、command receipt 和 event 在同一 `BEGIN IMMEDIATE` 事务提交；
- Program 的 `next_event_seq` 分配事件序号，禁止用乐观锁 version 兼作事件序号；
- event append-only 且只含安全元数据；反馈正文和模型原始输出不得进入 event；
- Today 查询、Program 读取和 mutation 都必须按 `owner_id` 限定，即使当前只有 `local-user`。

## 8. 调整与 PlanDocument 同步

调整放在 Today 闭环稳定后的独立切片，首版只允许用户显式触发，不运行自动 Review Worker。

### 8.1 提案

`goal_adjustment_proposals` 固定：

- `program_id`、`base_program_version_id`；
- 生成提案时的 `expected_plan_document_version_id/hash`；
- 影响的 future Action ID 与状态快照；
- 完整候选结构、确定性 diff、reason；
- `PENDING|ACCEPTED|REJECTED|STALE`、version 和 decision idempotency key。

LLM 只能产生候选结构，代码负责校验。不能修改已完成、已跳过、已延期的历史；当天 `IN_PROGRESS` 首版不存在；未来 `SCHEDULED` 可被 `CANCELLED(reason=SUPERSEDED)` 替换。

### 8.2 接受提案

一个 SQLite 短事务完成：

1. CAS 将 PENDING Proposal 置为 APPLYING/决策中；
2. 验证 base version 仍是 current，受影响 Action 仍处于允许状态；
3. 创建新的不可变 ProgramVersion；
4. 取消被替换的 future Action，插入新 Action；
5. 更新 Program current version；
6. 将 Proposal 置为 ACCEPTED；
7. 写 command receipt 和 Program event。

任何验证失败均整体回滚并返回 409；相同命令重试返回同一 ProgramVersion。

### 8.3 同步回 Markdown

ProgramVersion 是执行权威，因此 PlanDocument 同步失败不能回滚已经接受的调整。第一版调整功能提供显式“同步到计划文档”：

1. 基于提案固定的 expected PlanDocument head/hash 生成新的 Markdown；
2. 调用现有 `PlanDocumentService.save_model_revision()`；
3. 成功记录目标 version ID；
4. head 已被用户编辑则返回 conflict，绝不覆盖；用户可查看 diff 后基于新 head 重试或放弃同步。

若后续需要自动同步，再新增 `plan_projection_requests(PENDING|COMMITTED|FAILED|CONFLICT)` 和恢复 worker。这是可恢复 Saga，不宣称跨 SQLite 与文件系统 exactly-once 或单事务原子性。

## 9. 提醒、通知、SSE 与记忆的后续边界

### 9.1 提醒

不阻塞 MVP。启用时新增小型专用模型：

```text
goal_reminders
  program_id, kind, local_time, timezone, enabled, next_fire_at

goal_reminder_deliveries
  reminder_id, occurrence_key, channel_id, status, attempt, lease...
  UNIQUE(reminder_id, occurrence_key, channel_id)
```

只提取 Notification 的安全 transport；不插入 `research_jobs`，不复用 research delivery 表。scanner 在事务内幂等创建 occurrence，sender 使用 outbox/lease。承诺本地 occurrence 不重复；外部 provider 只承诺 at-least-once 尝试，不能声称无法证明的 exactly-once。

### 9.2 Program SSE

MVP 使用 mutation 后 refetch。出现 durable compilation/adjustment job、多窗口实时一致或明显轮询压力后，再用 `after_seq`、keep-alive 和 committed event replay 实现 Program SSE。

### 9.3 Memory

- 原始 Action 和 feedback 留在 GoalProgram 领域。
- 周期完成后可调用现有 `save_episode()` 保存有限摘要。
- “工作日只有 45 分钟”等稳定偏好只能进入现有 MemoryProposal，经用户确认后生效。
- 某天跳过、困难或状态不好不能自动晋升为长期记忆。
- 健康、位置、工作等敏感信息默认不进入外部通知、跨项目检索或长期记忆。

## 10. API 契约

### 10.1 MVP

```text
POST /api/plans/{plan_document_id}/program-preview
POST /api/programs/{program_id}/compile-retry
POST /api/programs/{program_id}/activate
GET  /api/programs
GET  /api/programs/{program_id}
GET  /api/today?date=YYYY-MM-DD

POST /api/actions/{action_id}/complete
POST /api/actions/{action_id}/skip
POST /api/actions/{action_id}/defer
POST /api/actions/{action_id}/feedback

POST /api/programs/{program_id}/pause
POST /api/programs/{program_id}/resume
POST /api/programs/{program_id}/complete
POST /api/programs/{program_id}/cancel
```

### 10.2 后续切片

```text
POST /api/actions/{action_id}/request-help
POST /api/programs/{program_id}/adjustments
POST /api/adjustments/{proposal_id}/accept
POST /api/adjustments/{proposal_id}/reject
POST /api/adjustments/{proposal_id}/sync-plan-document
GET  /api/programs/{program_id}/events?after_seq=&limit=
GET  /api/programs/{program_id}/events/stream?after_seq=
```

统一规则：

- mutation 要求 CSRF、`Idempotency-Key` 和 body 中的 `expected_version`；
- key 相同但 request hash 不同返回 409；状态或 base version 冲突返回 409 并附安全的 current snapshot；
- 模型输出违反 schema 返回稳定 422 reason code；临时模型失败返回 503；
- list/events 有 limit 上限；错误中不返回 SQLite 文本、模型原始 JSON、Prompt、secret 或本地路径；
- owner、thread、plan、program、action 的归属一律从数据库重新验证，不信任客户端冗余 ID。

## 11. 时区与日期

- Program 保存固定 IANA timezone；`scheduled_date` 是该 timezone 下的 local calendar date。
- `created_at/updated_at/completed_at/event occurred_at` 等均保存 UTC instant。
- `/api/today` 默认分别按每个 active Program 的 timezone 判断“今天”，客户端 timezone 不能重解释已排期 Action。
- 显式 `date` 仅用于查看某个 local date，不修改 Program timezone。
- timezone 变化属于受控调整，必须预览未来日期 diff 并创建新 ProgramVersion。
- 提醒阶段必须测试 DST：不存在的 wall time 顺延到下一合法时刻；重复时间采用固定 fold 策略；occurrence key 仍按 Program local date 和 reminder kind 去重。

## 12. 前端方案

### 12.1 Plan 页面

- 保留现有 Markdown 列表、详情和编辑。
- 增加“开始执行”；生成预览时显示来源版本、日期、时区、每日预算、假设和行动日历。
- 激活后明确区分“原计划文档”和“当前执行版本”；编辑正文不静默改变 active Program。
- 调整切片上线后显示 diff；同步 Markdown 冲突显示“执行版本已生效，计划文档尚未同步”，而不是回滚进度。

### 12.2 Today 页面

首版只包含：日期摘要、按 Program 分组的 Action、逾期分组、完成/跳过/延期、轻量反馈、进度和暂停入口。mutation 成功后 refetch。

桌面端两栏、移动端单列；390px 无横向滚动；键盘焦点、按钮标签、错误提示、reduced-motion 和 loading/disabled 状态必须可用。

### 12.3 Conversation

求助切片上线后，在消息区显示可关闭的“正在推进：目标 / 日期 / Action”上下文条。模型看到的是服务端组装的有界 DTO，不显示内部 JSON，不把 Action 内容当作 system instruction。

## 13. 分阶段实施

### Slice 0：可执行规格与失败测试

- 固化 7 天力扣与 7 天桂林旅行两个主场景；28 天骑行和 30 天英语作为边界测试。
- 定义 Program/Action 状态机、完成率、时区、owner、CAS、幂等和延期不变量。
- 用固定编译 fixture 写 schema/service 测试，不依赖真实模型或前端。

验收：同一 SQLite 测试可激活、完成、跳过、延期、暂停、重启恢复，且没有重复 Action 或事件。

### Slice 1：Program 内核、编译与 Today MVP

- 新 migration、`goal_programs.py`、`goal_program_compiler.py`。
- 同步 DRAFT preview、一次性激活物化、Today query、complete/skip/defer/feedback。
- Plan 页面入口、最小 Today 页面、mutation 后 refetch。

验收：用户从已有 Markdown 计划启动 7 天周期，并连续两天仅通过 Today 推进；刷新和重启不丢失、不重复。

### Slice 2：生命周期与 Conversation 求助

- pause/resume/complete/cancel、逾期分组和 tombstone。
- `GoalContextProvider` 与 Action-linked Turn。
- 普通解释由 Conversation 回答；暂不增加直接 Action Run API。

验收：暂停不污染 Today；上下文按 owner 有界读取；无工具需要时不创建 Run。

### Slice 3：用户触发的受控调整

- Proposal、完整候选 ProgramVersion、deterministic diff、accept/reject CAS。
- 接受时只事务提交 ProgramVersion 和 future Actions。
- 显式同步回 PlanDocument，处理 head/file hash conflict。

验收：过期 Proposal 返回 409；历史 Action 不变；Markdown 冲突不破坏执行版本。

### Slice 4：按真实证据选择扩展

以下能力逐项选择，不整包实施：

- durable compilation job；
- 工具 Run/Research 关联；
- Goal reminder/outbox；
- Program SSE；
- 自动复盘建议；
- 90 天以上、重复规则或滚动物化；
- 隐私导出和彻底擦除。

## 14. 测试与验收矩阵

### 14.1 MVP 必测

- 固定 PlanDocumentVersion/hash 后，编辑原 Markdown 不改变 ProgramVersion 和 Action。
- preview 相同幂等键返回同一 DRAFT；不同 payload 复用 key 返回 409。
- 编译失败不创建 Action；重试成功只产生一个 initial ProgramVersion。
- 重复 activate 只产生一组 Action；未 READY 或来源无效时不能激活。
- illegal Program/Action transition 被拒绝；CAS 冲突返回 current safe snapshot。
- complete/skip/defer 崩溃前后重放只产生一次结果和一组事件。
- defer 只创建一个替代 Action，源 Action 永久为 `DEFERRED`。
- required/optional/skipped/deferred/superseded 的完成率分母符合唯一算法。
- feedback 与 Action 完成分开并发，不使用完成前旧 version；敏感正文不进入 event。
- Program timezone 固定；系统 timezone 改变不移动 scheduled local date。
- paused/tombstoned Program 不进入 Today；恢复保持原日期；取消不删除来源计划。
- 旧数据库升级、全新数据库创建和迁移重复执行均通过。
- 所有 Program/Today/mutation 查询验证 owner；伪造 action_id 或 thread_id 无法越权。
- 模型越界日期、空标题、重复 key、未知字段、超 120 项和修复失败均被拒绝。
- 390px、键盘、焦点、reduced motion、loading、冲突恢复可用。

### 14.2 能力进入后再加

- compile worker lease generation、旧 owner fencing 和启动恢复；
- Proposal 并发接受、STALE 与 PlanDocument 投影冲突恢复；
- Action-linked Turn、工具方向确认、WRITE approval 与 Checkpoint；
- reminder occurrence/outbox、停机补发、DST 和 provider 重复边界；
- Program SSE `after_seq` 断线补齐与不重复；
- episode/proposal 的记忆边界和敏感信息过滤；
- purge/export 的审计与隐私语义。

### 14.3 端到端场景

#### 场景 A：桂林 7 天旅行攻略

用户已有一份 7 天桂林计划，选择出发日、`Asia/Shanghai` 和每日可用时间。编译器将住宿确认、交通预订、景点日程和每日注意事项生成预览；用户确认后一次性创建七天 Action。Today 只显示当天行程和此前逾期准备项。用户因天气延期漓江行程时，原 Action 保留为 `DEFERRED`，新日期创建替代 Action；原 Markdown 不被静默改写。

#### 场景 B：一周力扣训练

用户激活每日 60 分钟计划，完成第 1 天后立即持久化，再补充难度反馈。第 2 天刷新与重启后进度一致；连续困难后只显示“考虑调整”的入口，不自动调用模型或修改未来计划。

#### 场景 C：用户主动调整

用户请求降低第三周训练量。模型基于固定 ProgramVersion 生成 diff；用户接受后未来 Action 原子切换到新版本，历史保持；同步 Markdown 时若用户已编辑原文，显示冲突并允许放弃同步。

#### 场景 D：Agent 工具协助

用户在 Action 中请求帮助，先进入 Conversation。解释类请求直接回答；写外部日历时才由 Turn 提出 execution direction，用户确认后进入现有审批和 Checkpoint。点击求助本身不创建 Run。

## 15. 文件改动规划

### Slice 1 必需

```text
backend/app/db.py
backend/app/goal_programs.py
backend/app/goal_program_compiler.py
backend/app/api.py
backend/app/main.py

backend/tests/test_goal_programs.py
backend/tests/test_goal_program_compiler.py
backend/tests/test_goal_program_api.py
backend/tests/test_goal_program_migration.py

frontend/src/pages/TodayPage.tsx
frontend/src/components/DailyActionCard.tsx
frontend/src/App.tsx
frontend/src/api.ts
frontend/src/types.ts
frontend/src/components/WorkspaceSidebar.tsx
frontend/src/pages/PlanPage.tsx
frontend/src/styles.css
frontend/src/__tests__/TodayPage.test.tsx
```

### 后续按切片增加

```text
backend/app/goal_context.py
backend/app/goal_adjustments.py
backend/app/goal_reminders.py
backend/app/events.py
backend/app/notifications.py
backend/app/conversation.py

frontend/src/components/AdjustmentProposalCard.tsx
frontend/src/pages/ChatPage.tsx
frontend/src/pages/TrajectoryPage.tsx
```

不要预先创建空的 worker、scheduler、transport interface 或通用 workflow 文件；只有对应切片开始时才添加。

## 16. 升级触发条件

| 能力 | 至少满足一项再增加 |
|---|---|
| Compile Worker | p95 超过 HTTP/代理容忍时间；需要关闭页面后继续；出现排队需求 |
| claim/lease/heartbeat | 后台 job 已存在并可能被两个进程竞争 |
| Program SSE | durable 后台状态、多窗口一致或 refetch 出现实测问题 |
| template/instance | 存在真实 recurrence rule，单一逻辑动作生成多个 occurrence |
| 滚动物化 | 周期超过 90 天、无结束日、动态外部输入或预生成出现实测瓶颈 |
| 自动 Review Worker | 手动复盘使用率高，触发 precision/recall 有基线，用户愿意接收主动建议 |
| 外部提醒 | Today 回访不足且用户明确授权；静默时段和隐私要求已确定 |
| 通用调度/通知抽象 | 至少三个独立领域共享同一稳定语义 |
| Action 与 Run 关联 | 已落地第一个必须使用审批/Checkpoint 的 Action 场景 |
| PlanDocument 自动回写 | 用户无法理解双版本，且愿意接受同步冲突与恢复交互 |
| 硬删除/导出 | 有明确隐私擦除或迁移需求，并定义与 append-only audit 的关系 |

## 17. 完成定义

本阶段只有同时满足以下条件才算完成：

- 核心闭环在 fake compiler、真实模型和浏览器中均通过；
- 现有 PlanDocument、Conversation、Runtime、Memory、Research、Schedule 和 Notification 回归无破坏；
- 每个已实现高风险能力都有对应崩溃、重放、并发和升级测试；
- 文档不把未实现的 Worker、提醒、SSE、Run 关联或自动调整描述为已完成；
- 没有新增数据库、ORM、Agent 框架、消息队列、Redis、Celery、微服务或通用 workflow engine；
- 用户能够清楚区分“原计划文档”“当前执行版本”“今天的行动”，且任何自动建议都不会绕过确认修改未来安排。

最终产品心智模型保持为：

> 计划文档是你可编辑的完整方案；执行项目记住你已经确认的安排；Today 只告诉你现在该做什么。现实发生变化时，Agent 先解释并提出调整，只有你确认后才改变未来，已经发生的历史永远不会被改写。
