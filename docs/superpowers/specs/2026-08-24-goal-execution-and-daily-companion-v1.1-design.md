# Better Agent 目标执行与每日陪伴 V1.1 设计

> 状态：设计提案，待用户确认后制定实施计划
>
> 日期：2026-08-24
>
> 适用项目：Better Agent
>
> 核心目标：把“生成一份计划”升级为“陪用户按计划行动、反馈并持续调整”

## 1. 一句话结论

Better Agent V1 已经能够理解目标、询问必要信息、生成并保存计划，也具备对话、记忆、工具、审批、轨迹、研究、定时和通知能力；但用户拿到计划后，系统仍然主要停留在“交付文档”。

V1.1 应补齐下面这条闭环：

```text
提出目标
  → 生成并保存计划
  → 用户确认开始执行
  → 系统每天只展示今天该做什么
  → 用户完成、跳过、延期或反馈困难
  → Agent 复盘执行结果
  → 必要时提出调整方案
  → 用户确认后生成新版本
  → 继续下一天，直到目标结束
```

这不是新增一个聊天功能，而是让 Better Agent 从“计划生成器”变成真正帮助用户实现目标的个人 Agent。

## 2. 背景

### 2.1 Better Agent 的产品方向

Better Agent 的核心定位不是替用户展示复杂的 Agent 技术，而是：

> 帮助用户推进真实目标，让用户逐步变成更好的自己。

这类目标通常具有以下特点：

- 需要持续多天或多周；
- 用户无法一次完成；
- 执行过程中会受到时间、身体状态、工作安排和现实反馈影响；
- 原始计划很少能够完全照搬到最后；
- 用户真正需要的不是更多文字，而是今天应该做什么、做完后下一步是什么。

典型场景包括：

- 一周力扣训练；
- 长期骑行训练；
- 30 天英语学习；
- 七天旅行准备；
- 求职投递与面试准备；
- 健身、阅读、作息改善；
- 一个需要持续推进的个人项目。

### 2.2 V1 已有基础

当前系统已经具备本阶段可以复用的能力：

- `PlanDocument`：保存用户可查看、编辑和删除的 Markdown 计划资产；
- `PlanDocumentVersion`：保留计划历史版本；
- `PlanExecutionCompiler`：把固定计划版本编译为结构化步骤；
- `AgentRuntime`：显式状态机、计划版本、审批、顺序 ReAct、预算和恢复；
- `Checkpoint`：防止崩溃恢复后重复执行副作用；
- `ThreadEvent` / `EventStore`：append-only 轨迹；
- SSE：实时展示对话和执行进度；
- Ask 工具：由模型决定缺少的上下文并向用户提问；
- 三层记忆：短期对话、经历摘要和长期稳定信息；
- Schedule / Notification：定时触发和通知渠道；
- 深度研究：长任务、租约、取消和恢复的实现经验。

因此，V1.1 不需要替换现有 Runtime，也不需要引入新的基础设施。真正缺少的是一个位于“计划文档”和“一次 Agent Run”之间的长期目标执行层。

## 3. 当前问题

### 3.1 计划保存后缺少明确的下一步

用户可以在计划页面查看和修改计划，但保存完成不等于开始执行。当前系统没有清楚表达：

- 计划从哪一天开始；
- 今天应该完成哪一部分；
- 每项任务需要多长时间；
- 怎样才算完成；
- 计划目前完成了多少；
- 用户今天没完成时应该怎么办。

用户需要反复打开整篇 Markdown，自行寻找当天内容并自行记住进度。

### 3.2 静态计划与现实执行脱节

现实中的执行会发生变化：

- 今天只有 30 分钟，而计划安排了 90 分钟；
- 用户连续两天没有完成；
- 任务难度明显过高；
- 用户提前完成，需要提高强度；
- 身体不适或临时出差；
- 原定目标发生改变。

当前计划文档可以手工编辑，但 Agent 没有基于执行反馈主动提出调整，也无法说明调整了什么、为什么调整。

### 3.3 Agent 的交互仍然偏“回答”，不是“陪伴”

当前对话往往结束于一份完整答案。对于长期目标，理想交互应该是：

- 今天只呈现必要的信息；
- 用户通过按钮即可完成主要反馈；
- 遇到困难时能够立即求助；
- Agent 能记住本计划内的执行经历；
- 每周给出简短总结，而不是再次生成一篇长报告。

### 3.4 现有 Run 不适合直接代表跨天计划

现有 `Run` 表达的是一次有界 Agent 执行：计划经过审批后进入顺序 ReAct，使用预算、工具审批和 Checkpoint，最后到达终态。

一个持续 30 天的学习计划不能让同一个 Run 保持 30 天 `EXECUTING`，否则会混淆：

- 一次模型/工具执行和长期生活项目；
- Run 的预算与整个计划周期预算；
- Checkpoint 恢复与“明天继续”；
- 执行失败与用户某天主动跳过；
- Runtime 终态与计划暂停状态。

因此，不能简单地给现有 Run 增加日期字段后长期挂起。

### 3.5 执行记录不能混入长期记忆

“8 月 24 日完成两道数组题”“今天训练感觉较难”是计划执行记录，不是长期记忆。若把这些内容都写入长期记忆，会重新造成记忆系统混乱，并污染后续无关对话。

正确边界应为：

| 内容 | 所属领域 |
|---|---|
| 用户稳定偏好和长期约束 | 长期记忆 |
| 当前对话 | 短期记忆 |
| 历史对话摘要 | 经历记忆 |
| 计划正文 | PlanDocument |
| 每日任务、完成、跳过、延期 | 目标执行记录 |
| Agent 使用工具完成一次动作 | Run / Tool / Checkpoint |
| 计划修改历史 | PlanDocumentVersion + ProgramVersion |

## 4. 开发目标

V1.1 要实现以下产品结果：

1. 用户能从一份已保存计划明确地“开始执行”；
2. 系统把计划转为有日期、有预计用时、有完成标准的结构化安排；
3. 用户打开应用首先能看到今天该做什么；
4. 用户可以完成、跳过、延期、恢复或取消任务；
5. 用户可以用最少交互反馈难度、耗时和问题；
6. Agent 能根据反馈判断是否需要调整后续计划；
7. 任何计划调整都先展示变更摘要，得到用户确认后才生效；
8. 刷新、关闭页面和重启应用后，进度不会丢失或重复；
9. 用户可以暂停、恢复、完成或取消整个目标计划；
10. 轨迹页面可以用自然语言展示目标推进过程，而不是只展示内部事件 JSON。

## 5. 非目标与边界

V1.1 不做：

- 多用户协作、团队任务分配；
- 看板、甘特图或完整项目管理系统；
- 游戏化积分、排行榜和社交打卡；
- 健康设备、日历或第三方 Todo 的双向同步；
- Agent 在未确认时自动重写计划；
- 让一个 ReAct Run 跨天常驻；
- 自动执行涉及付费、发消息、修改外部数据的动作；
- 为每日计划引入 Redis、Celery、独立微服务或消息队列；
- 把全部执行日志写进长期记忆；
- 隐藏思维链或模型原始 JSON 展示。

首版聚焦本地单用户、单机 SQLite 和有限周期的个人目标。建议支持 1–90 天计划；无结束日期的长期目标按 28 天为一个可续期周期，避免一次生成无限任务。

## 6. 核心产品模型

V1.1 增加一个 `GoalProgram`（目标项目）领域：

```text
PlanDocument
  用户维护的计划正文，回答“总体准备怎么做”
       ↓ 固定一个已提交版本
GoalProgram
  一次真实开始的目标周期，回答“现在执行到哪里”
       ↓ 每天物化
DailyAction
  今天具体要做的任务，回答“今天做什么”
       ↓ 需要模型或工具时
Agent Run
  一次有界执行，回答“现在帮用户完成这个动作”
```

### 6.1 PlanDocument

继续作为用户可编辑的计划资产：

- 一份计划可以尚未开始；
- 一份计划可以被多次重新开始；
- 编辑原计划不会静默修改已开始的 GoalProgram；
- GoalProgram 必须固定来源 `PlanDocumentVersion` 和内容 Hash。

### 6.2 GoalProgram

表示用户实际开始的一次目标周期：

- 开始日期、时区和结束日期；
- 当前生效版本；
- 当前状态；
- 总体进度和下一次复盘日期；
- 来源计划版本；
- 用户确认的执行偏好。

### 6.3 ProgramVersion

GoalProgram 的不可变结构化版本，包含：

- 目标摘要；
- 周期和里程碑；
- 行动模板；
- 调整原因；
- 来源版本；
- 创建者（模型、用户或系统）；
- 与上一版本的 diff 摘要。

### 6.4 DailyAction

由 ProgramVersion 物化出的某日具体任务：

- 日期；
- 标题和说明；
- 预计用时；
- 完成标准；
- 当前状态；
- 实际耗时和用户反馈；
- 来源模板和 ProgramVersion；
- 若调用 Agent Run，保存对应 `run_id`。

已完成或已跳过的历史 Action 不因计划改版被重写。新版本只影响尚未开始的未来任务。

## 7. 用户流程

### 7.1 开始执行

计划页面在已保存计划详情中增加“开始执行”：

```text
点击开始执行
  → 选择开始日期、时区和每日可用时间
  → LLM 从固定 PlanDocumentVersion 编译执行预览
  → 展示周期、每周节奏、前 7 天任务和假设
  → 用户确认
  → 创建 GoalProgram 并激活
```

如果原计划已经明确包含开始日期和时间预算，系统直接预填，不重复询问。若存在真正阻塞的缺失信息，使用现有 Ask 卡片询问最少问题。

用户确认前不创建正式每日任务，不发送提醒，也不占用 Agent Run 预算。

### 7.2 今日执行

侧栏增加“今天”，并把它作为存在活动 GoalProgram 时的默认入口。

“今天”页面按目标分组展示：

- 目标名称和当前第几天；
- 今日总预计用时；
- 今日行动卡；
- 延期到今天的任务；
- 今日完成进度；
- 快速复盘入口。

每张行动卡只提供必要操作：

- 开始；
- 完成；
- 跳过；
- 延期；
- 遇到困难；
- 让 Agent 帮我。

### 7.3 完成与反馈

点击“完成”应立即持久化，不等待 LLM。随后弹出可跳过的轻量反馈：

- 实际用时；
- 难度：轻松 / 合适 / 困难；
- 一句补充说明。

点击“跳过”或“延期”时只询问一个原因，默认提供：

- 没有时间；
- 难度过高；
- 身体或状态不适；
- 优先级改变；
- 其他。

反馈保存完成后，页面立即更新。模型分析在后台进行，不能阻塞用户的完成操作。

### 7.4 遇到困难

点击“遇到困难”打开当前目标对话，并自动附带受信任的结构化上下文：

- 当前 GoalProgram；
- 当前行动；
- 最近相关执行记录；
- 当前生效计划版本。

模型可以：

- 直接解释或鼓励；
- 使用 Ask 获取一个必要信息；
- 建议拆小当前任务；
- 提出计划调整；
- 在用户明确要求时使用工具。

不能把内部 JSON 暴露给用户。

### 7.5 每日复盘

当日有任务结束，或到用户设定的复盘时间时，系统提供一张简短卡片：

```text
今天完成 2/3 项，用时 55 分钟。
数组基础已完成，滑动窗口延期到明天。

[按原计划继续] [调整明天] [补充感受]
```

没有异常时不强迫用户填写长问卷。

### 7.6 计划调整

系统使用“代码触发 + LLM 判断”的双层策略。

代码只判断是否值得评估，例如：

- 连续两次跳过或延期；
- 两次实际用时明显超过预计；
- 两次反馈为困难；
- 连续多天提前完成；
- 用户明确要求调整；
- 到达周复盘节点。

LLM 决定是否确实需要调整以及怎样调整，但只能生成 `AdjustmentProposal`，不能直接生效。

UI 展示：

- 为什么建议调整；
- 哪些未来任务被修改；
- 修改前后对比；
- 对目标周期和强度的影响；
- 接受、继续原计划或要求重新生成。

用户接受后创建新的 ProgramVersion，并同步生成新的 PlanDocumentVersion，使计划页面仍然是用户可理解的完整计划。拒绝后原版本继续有效。

### 7.7 暂停、恢复、完成和取消

- 暂停：停止生成新任务和发送提醒，保留历史；
- 恢复：用户选择从今天顺延或保持原日期；
- 完成：所有必做行动完成，或用户主动提前结束并确认目标已达成；
- 取消：未来行动取消，历史保留，可重新开始为新的 GoalProgram；
- 删除：只有终态项目可删除；删除前明确说明将删除执行记录，不删除原计划文档。

## 8. 状态机

### 8.1 GoalProgram 状态

```text
PREVIEW
  → ACTIVE
  → PAUSED
  → ACTIVE
  → COMPLETED
  → CANCELLED

PREVIEW → CANCELLED
ACTIVE  → CANCELLED
PAUSED  → CANCELLED
```

约束：

- `PREVIEW` 不能产生提醒和完成记录；
- 只有用户确认可以 `PREVIEW -> ACTIVE`；
- `COMPLETED` 和 `CANCELLED` 为终态；
- ProgramVersion 更新不改变 Program 状态；
- 状态转换使用 `expected_version` 乐观锁和幂等键。

### 8.2 DailyAction 状态

```text
SCHEDULED → IN_PROGRESS → COMPLETED
SCHEDULED → COMPLETED
SCHEDULED → SKIPPED
SCHEDULED → DEFERRED → SCHEDULED（新日期实例）
IN_PROGRESS → SKIPPED
SCHEDULED / IN_PROGRESS → CANCELLED（项目改版或取消）
```

延期不直接覆盖原日期：原实例进入 `DEFERRED`，创建一个带 `deferred_from_action_id` 的新实例，从而保留真实历史。

## 9. LLM 与代码的职责边界

### 9.1 由代码确定

- 权限、状态转换和终态；
- 日期、时区和下一次触发时间计算；
- 完成率、连续跳过次数、实际用时等统计；
- 幂等、租约、版本冲突和事务；
- 哪些已完成记录不可被改写；
- 是否需要用户批准；
- 通知是否允许发送；
- 安全规则和工具权限。

### 9.2 由 LLM 决定

- 从计划正文提取合理的行动节奏；
- 哪些个人上下文是真正必要的；
- 每日任务的自然语言描述和完成标准；
- 反馈是否意味着计划不合理；
- 如何调整未来任务；
- 每日和每周复盘的简短表达；
- 用户遇到困难时如何帮助。

### 9.3 LLM 不得决定

- 未经用户确认直接激活计划；
- 未经用户确认修改生效版本；
- 自行扩大工具权限；
- 把跳过解释为完成；
- 修改已完成的历史记录；
- 静默创建外部日历或发送消息；
- 把原始结构化输出展示给用户。

## 10. 编译协议

新增 `GoalProgramCompiler`，输入一个固定的 PlanDocumentVersion、开始日期、时区和执行约束，输出严格结构化结果：

```json
{
  "summary": "string",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "review_cadence": "daily|weekly",
  "assumptions": ["string"],
  "milestones": [
    {"id": "string", "title": "string", "target_date": "YYYY-MM-DD"}
  ],
  "actions": [
    {
      "logical_key": "string",
      "scheduled_date": "YYYY-MM-DD",
      "title": "string",
      "description": "string",
      "estimated_minutes": 30,
      "completion_criteria": "string",
      "required": true
    }
  ]
}
```

首版规则：

- 最多 90 天；
- 每天最多 8 项；
- 单项预计 5–480 分钟；
- 日期必须单调落在周期内；
- `logical_key` 在一个版本内唯一；
- 标题、说明和完成标准有长度上限；
- 空任务、未知字段和非法日期必须拒绝；
- 结构修复最多一次，不跨供应商 fallback；
- 编译失败保留预览错误，可重试，不创建半成品活动项目。

无结束日期的目标只编译首个 28 天周期，周期结束时由用户决定续期或重新规划。

## 11. 数据模型

SQLite 继续作为唯一权威数据源。建议新增：

```text
goal_programs
  id, owner_id, goal_id, thread_id
  source_plan_document_id
  source_plan_document_version_id
  source_plan_content_hash
  status, version
  timezone, start_date, end_date
  current_program_version_id
  next_review_at
  created_at, updated_at, completed_at, cancelled_at

goal_program_versions
  id, program_id, version
  source_plan_document_version_id
  summary, assumptions_json, milestones_json
  change_summary, base_version_id
  actor, status
  created_at
  UNIQUE(program_id, version)

goal_action_templates
  id, program_version_id, logical_key
  scheduled_date
  title, description
  estimated_minutes, completion_criteria
  required, position
  UNIQUE(program_version_id, logical_key)

goal_action_instances
  id, program_id, program_version_id, template_id
  scheduled_date, status, version
  started_at, completed_at, skipped_at, deferred_at
  actual_minutes, difficulty, note, skip_reason
  deferred_from_action_id
  linked_run_id
  idempotency_key
  created_at, updated_at

goal_adjustment_proposals
  id, program_id
  base_program_version_id
  trigger_kind, evidence_json
  proposed_structure_json, diff_json, reason
  status, version
  decision_idempotency_key
  accepted_program_version_id
  created_at, decided_at

goal_program_events
  row_id, event_id, program_id, seq
  action_id, type, actor, occurred_at, data_json
  UNIQUE(program_id, seq)
```

约束：

- 一个 ProgramVersion 只写一次；
- 同一 Program 同时只有一个 current version；
- 同一行动状态写入使用 CAS；
- 同一个延期请求只能产生一个新 Action；
- 同一个 Proposal 只能接受一次，并返回相同的新版本；
- Event append-only，不能更新或删除；
- Event 只保存摘要和资源 ID，不保存长反馈正文或模型原始输出。

## 12. 与现有 Agent Runtime 的关系

### 12.1 不让 GoalProgram 替代 Run

GoalProgram 是长期状态和日历，Run 是一次有界 Agent 执行。两者关系为：

```text
DailyAction
  ├─ 用户自行完成 → 直接记录结果，不创建 Run
  └─ 点击“让 Agent 帮我”
       → 创建一次 Run
       → 正常走计划审批、工具审批、预算、Checkpoint
       → Run 终态写回 linked_run_id 和结果摘要
```

例如：

- “今天完成 3 道力扣题”由用户打卡，不需要 Run；
- “帮我解释这道题”通常由对话直接回答；
- “帮我把训练安排写入外部日历”需要 Run、WRITE 工具和审批；
- “帮我检索最新面试高频题”可以启动一次深度研究 Job。

### 12.2 复用现有可靠性能力

- 需要工具的行动继续复用 ToolRegistry、ApprovalService 和 Checkpoint；
- Program 编译和调整生成复用模型网关，不建立第二个供应商配置；
- Worker 的 claim、lease、heartbeat、cancel 模式参考现有 Turn/Research Worker；
- 可见进度继续通过 SSE；
- 不在 GoalProgram 中复制 ReAct 主循环。

## 13. 记忆边界

### 13.1 进入执行上下文的内容

`GoalContextProvider` 为对话和模型生成一个有界数据块：

- 当前 GoalProgram 摘要；
- 当前 ProgramVersion；
- 今天和最近未完成行动；
- 最近 7 天聚合统计；
- 最近最多 5 条相关反馈；
- 当前 AdjustmentProposal。

这些内容是执行数据，不写入 MemoryContextProvider，也不计作长期记忆。

### 13.2 可以进入经历记忆的内容

一个自然周期结束后，可以形成经历摘要：

> 用户完成一周力扣计划的 16/20 项；滑动窗口难度较高，后续把单日题量从 3 道调整为 2 道。

经历摘要仍然是派生叙述，不替代 Action 记录。

### 13.3 可以提出长期记忆建议的内容

只有稳定、可跨目标复用的信息才可进入长期建议，例如：

- 用户工作日通常只能投入 45 分钟；
- 用户更适合小步递进的训练节奏；
- 用户明确要求以后都不要安排早于九点的活动。

一次没完成、某天状态不好、一道题太难不能自动成为长期记忆。

## 14. API 设计

建议新增：

```text
POST   /api/plans/{plan_id}/program-preview
POST   /api/programs/{program_id}/activate
GET    /api/programs
GET    /api/programs/{program_id}
POST   /api/programs/{program_id}/pause
POST   /api/programs/{program_id}/resume
POST   /api/programs/{program_id}/complete
POST   /api/programs/{program_id}/cancel
DELETE /api/programs/{program_id}

GET    /api/today?date=YYYY-MM-DD
POST   /api/actions/{action_id}/start
POST   /api/actions/{action_id}/complete
POST   /api/actions/{action_id}/skip
POST   /api/actions/{action_id}/defer
POST   /api/actions/{action_id}/feedback
POST   /api/actions/{action_id}/request-help

POST   /api/programs/{program_id}/review
GET    /api/programs/{program_id}/adjustments/pending
POST   /api/adjustments/{proposal_id}/accept
POST   /api/adjustments/{proposal_id}/reject

GET    /api/programs/{program_id}/events
GET    /api/programs/{program_id}/events/stream
```

所有 mutation：

- 要求 CSRF；
- 要求 `expected_version`；
- 要求客户端幂等键；
- 冲突返回 409 和当前安全快照；
- 不在错误信息中返回模型原始输出。

## 15. 事件与轨迹

建议事件：

```text
program.preview_created
program.activated
program.paused
program.resumed
program.completed
program.cancelled
program.version_created

action.scheduled
action.started
action.completed
action.skipped
action.deferred
action.feedback_added
action.help_requested
action.run_linked

review.started
review.completed
adjustment.proposed
adjustment.accepted
adjustment.rejected
reminder.sent
reminder.failed
```

轨迹 UI 不显示事件 JSON，而展示可理解的时间线：

```text
09:00  开始“数组基础训练”
09:42  完成任务，用时 42 分钟，难度合适
20:30  “滑动窗口练习”延期到明天
20:31  Agent 建议明天减少 1 道题，等待你的确认
```

详情抽屉可以展示安全字段、版本来源和关联 Run，但默认折叠技术元数据。

## 16. 通知与调度

复用现有 NotificationService，但目标提醒与研究 Schedule 分开建模，不能把训练计划伪装成研究任务。

提醒类型：

- 今日任务准备好；
- 用户指定的开始时间；
- 当日尚未完成的温和提醒；
- 每日复盘；
- 每周总结；
- 计划即将结束或需要续期。

规则：

- 默认不开启外部通知，用户明确启用；
- 暂停项目后停止通知；
- 同一 Program、日期和提醒类型最多投递一次；
- 通知失败不改变 Action 或 Program 状态；
- 通知正文不包含敏感反馈、完整计划或本地路径；
- 使用 IANA timezone 计算，数据库统一保存 UTC；
- 停机错过多个提醒时最多补发一条摘要，不连续轰炸用户。

## 17. 前端方案

### 17.1 侧栏

新增“今天”：

```text
对话
今天
计划
轨迹
研究
定时
记忆
```

存在今日未完成任务时显示数量徽标。没有活动目标时，“今天”页面展示“从一份计划开始执行”，并链接到计划页。

### 17.2 今天页面

桌面端：

```text
┌──────────────────────────────────────────────┐
│ 8 月 24 日 · 今天共 3 项 · 预计 60 分钟       │
├───────────────────────────┬──────────────────┤
│ 今日行动                  │ 当前目标概览      │
│ [任务卡]                  │ 进度 / 连续天数   │
│ [任务卡]                  │ 下次复盘          │
│ [任务卡]                  │ 待确认调整        │
└───────────────────────────┴──────────────────┘
```

移动端按“今日摘要 → 行动卡 → 调整建议 → 目标进度”单列排列，不出现横向滚动。

### 17.3 计划页面

保留现有计划列表和渲染后编辑，在详情顶部增加：

- 开始执行；
- 查看执行进度；
- 暂停/恢复；
- 当前执行版本；
- 查看计划版本与执行版本差异。

“编辑计划正文”与“调整正在执行的计划”必须区分：

- 编辑正文：只生成 PlanDocumentVersion；
- 调整执行：生成 Proposal，确认后同时生成 ProgramVersion 和新的 PlanDocumentVersion。

### 17.4 对话页面

当对话关联当前行动时，在消息区顶部显示一条可关闭的轻量上下文条：

> 正在推进：第 3 天 · 滑动窗口练习

模型回答后，相关操作以卡片显示，例如“拆成 2 个更小任务”“调整明天计划”，不要求用户输入内部命令。

## 18. 调整策略

首版保持简单、透明：

### 18.1 代码产生评估信号

```text
two_consecutive_skips
two_high_difficulty_feedbacks
repeated_time_overrun
repeated_early_completion
weekly_review_due
explicit_user_request
schedule_conflict
```

### 18.2 LLM 返回结构化提案

```json
{
  "needs_adjustment": true,
  "reason": "string",
  "summary": "string",
  "changes": [
    {
      "logical_key": "string",
      "operation": "update|move|remove|add",
      "before": {},
      "after": {}
    }
  ]
}
```

### 18.3 确定性校验

- 只能修改未来未开始任务；
- 不能删除已完成历史；
- 新日期不能超出允许周期，除非明确展示延期；
- 必做任务减少、结束日期变化必须突出显示；
- Proposal 基于固定 ProgramVersion；
- base version 已变化时接受操作返回冲突，不能静默重放；
- 修复失败则展示“暂时无法生成调整建议”，原计划继续有效。

## 19. 可靠性与恢复

高风险模块必须先写失败测试：

- Program 状态转换；
- Action 完成、跳过和延期幂等；
- 延期防重复创建；
- Proposal 接受时的版本冲突；
- 新版本不得改写已完成 Action；
- 编译中断恢复；
- 提醒重复投递；
- Action 关联 WRITE Run 后的 Checkpoint 防重复副作用；
- SSE 事件 seq 单调和断线补偿；
- 调整接受后 ProgramVersion 与 PlanDocumentVersion 一致提交。

恢复原则：

- SQLite transaction 只提交短小状态变更；
- 模型调用在 transaction 外；
- 编译、复盘和调整任务使用 claim + lease + heartbeat；
- 旧 Worker 丢失 lease 后不能提交；
- 模型结果先校验，再在单 transaction 内写入版本和事件；
- 应用重启扫描过期任务并恢复；
- 用户完成 Action 的写入不依赖 Worker 和模型。

## 20. 安全与隐私

- GoalProgram 上下文只作为数据，不能修改系统指令和工具权限；
- 用户反馈可能包含健康、工作或位置等敏感信息，事件只保存安全摘要；
- 健康训练计划遇到伤病、胸痛等风险表达时，Agent 应停止普通强度建议并提示寻求专业帮助；
- 外部 WRITE 行为继续使用显式审批和 Checkpoint；
- 通知不包含敏感备注；
- 删除 GoalProgram 时清理 Action、Proposal 和 Program 投影，但不自动删除原计划和长期记忆；
- 导出默认脱敏，不包含密钥、模型原始结构和通知 secret；
- 不允许模型把完成率作为人格评价或对用户进行羞辱性表达。

## 21. 可观测性与指标

产品指标用于判断功能是否真的帮助用户，而不是评价用户：

- 计划激活率：保存计划后开始执行的比例；
- 首日行动率；
- 次日回访率；
- Action 完成、跳过和延期比例；
- 反馈提交率；
- Proposal 接受/拒绝比例；
- 调整后 7 天完成率变化；
- 通知点击和关闭比例；
- 编译时间、TTFT、失败率和恢复次数。

本地 V1.1 只在 SQLite 保存聚合统计，不上传远程分析平台。UI 不展示带有压力的“失败天数”，而使用中性表达，如“本周完成 4/6 项”。

## 22. 分阶段开发方案

### Phase 0：场景与失败测试

- 固化 7 天力扣、长期骑行、30 天英语、旅行准备四个验收场景；
- 先写 Program/Action 状态机和幂等失败测试；
- 建立固定 fake model 编译输出；
- 确认 PlanDocument、Memory 和 Run 边界。

完成标准：核心状态和数据边界形成可执行测试，不依赖前端。

### Phase 1：GoalProgram 内核

- 新增 schema、dataclass 和 service；
- ProgramVersion、Action 实例和 append-only 事件；
- 开始、暂停、恢复、完成、取消；
- 完成、跳过、延期和反馈；
- 乐观锁和幂等。

完成标准：纯后端测试能完整执行 7 天计划并在重启后保持一致。

### Phase 2：计划编译与激活

- `GoalProgramCompiler`；
- 固定 PlanDocumentVersion；
- 预览、结构修复、用户确认；
- 编译 Worker、租约和恢复；
- 激活后物化 Action。

完成标准：从现有 Markdown 计划生成合法预览，确认后只创建一次活动项目。

### Phase 3：今天页面

- `/api/today` 聚合；
- TodayPage 和行动卡；
- 完成、跳过、延期、轻量反馈；
- 自适应桌面和移动端；
- 刷新与 SSE 恢复。

完成标准：用户不打开整篇计划也能完成当天所有操作。

### Phase 4：困难求助与关联 Run

- GoalContextProvider；
- 对话关联 Action；
- “让 Agent 帮我”创建有界 Run；
- WRITE 审批和 Checkpoint 回归；
- 结果写回行动摘要。

完成标准：自行打卡不创建 Run；需要工具时完整走现有安全执行链。

### Phase 5：复盘与受控调整

- 确定性评估信号；
- 每日/每周 Review Worker；
- AdjustmentProposal；
- diff UI；
- 接受后原子创建 ProgramVersion 和 PlanDocumentVersion；
- 只更新未来 Action。

完成标准：连续困难场景能提出合理调整，拒绝不改变计划，接受后历史保持不变。

### Phase 6：提醒与完整轨迹

- 目标提醒调度；
- NotificationService 接入；
- Program SSE；
- 自然语言轨迹和统计；
- 删除与脱敏导出。

完成标准：重启、重复扫描和网络失败不造成重复通知或进度回滚。

### Phase 7：真实模型评测与体验收尾

- 使用指定 `LLM_API.txt` 做真实编译与调整回归；
- 评估不同计划类型的日期准确性和可执行性；
- 浏览器测试桌面和移动端；
- 检查 Ask 是否最小化、回复是否流式可见；
- 检查所有错误只显示用户可理解文案。

完成标准：四个真实场景均能从计划开始执行，完成至少一天，调整、暂停、恢复和删除均正常。

## 23. 测试与验收

### 23.1 后端关键测试

- 非法 Program 状态转换被拒绝；
- 重复激活返回同一结果；
- 同一 Action 重复完成不产生两条结果；
- 同一延期请求只创建一个目标实例；
- 完成后的 Action 不被新版本修改；
- 接受过期 Proposal 返回 409；
- ProgramVersion 与 PlanDocumentVersion 不发生单边提交；
- LLM 日期越界、重复 logical key、空任务被拒绝；
- 结构修复最多一次；
- 编译 Worker 丢失 lease 后不能提交；
- 暂停后不再物化任务和发送提醒；
- 恢复时顺延策略确定且可重放；
- 删除终态项目不删除原 PlanDocument；
- 反馈不进入长期记忆表；
- linked Run 的 WRITE 审批和 Checkpoint 仍防重复；
- 事件 seq 单调，重复请求不重复事件；
- 导出不包含 secret、内部 Prompt 和模型原始 JSON。

### 23.2 前端关键测试

- 没有活动项目时显示开始入口；
- 多个目标按日期正确聚合；
- 完成按钮立即更新且重复点击被禁用；
- 延期后原卡显示已延期，新卡日期正确；
- 暂停项目不显示为今日待完成；
- Adjustment diff 正确显示 before/after；
- 拒绝 Proposal 后页面恢复原计划；
- SSE 断线后通过 seq 补齐且不重复；
- 桌面和 390px 移动端无横向滚动；
- 键盘、焦点、按钮标签和 reduced-motion 可用；
- UI 不显示内部状态 JSON、reason code 或原始模型结构。

### 23.3 端到端验收场景

#### 场景 A：一周力扣计划

用户生成并保存一周计划，点击开始执行，选择今天开始、每天 60 分钟。系统展示每日题型、预计时间和完成标准。用户完成第一天后，第二天刷新仍显示正确进度。

#### 场景 B：长期骑行计划

用户开始 28 天周期，连续两次反馈难度过高。系统提出降低第三周训练量的 Proposal，用户接受后未来任务变化，已完成记录不变。

#### 场景 C：临时延期

用户把今天的任务延期到明天。重复点击或网络重试只产生一个新任务，今日记录保留“已延期”。

#### 场景 D：Agent 工具协助

用户要求把训练安排写入外部系统。系统创建一次 Run，展示计划和 WRITE 审批；重启后 Checkpoint 防止重复写入。

#### 场景 E：暂停与恢复

用户暂停一周，期间无提醒。恢复时选择“从今天顺延”，未来任务日期统一调整并产生新版本。

#### 场景 F：隐私与删除

用户删除已取消的 GoalProgram，执行记录和投影被删除，原计划文档仍存在；脱敏导出中没有反馈敏感正文和任何 Key。

## 24. 文件改动规划

### 24.1 后端新增

```text
backend/app/goal_programs.py
backend/app/goal_program_compiler.py
backend/app/goal_program_worker.py
backend/app/goal_context.py
backend/app/goal_reviews.py
backend/app/goal_reminders.py

backend/tests/test_goal_programs.py
backend/tests/test_goal_program_compiler.py
backend/tests/test_goal_program_worker.py
backend/tests/test_goal_program_api.py
backend/tests/test_goal_reviews.py
backend/tests/test_goal_reminders.py
```

首版保持少量领域文件。若单文件超过清晰边界，再按 service/models 拆分，不提前建设插件体系。

### 24.2 后端修改

```text
backend/app/db.py
backend/app/api.py
backend/app/runtime.py
backend/app/startup.py
backend/app/main.py
backend/app/plan_documents.py
backend/app/conversation.py
backend/app/live_model.py
backend/app/notifications.py
backend/app/stats.py
```

### 24.3 前端新增与修改

```text
frontend/src/pages/TodayPage.tsx
frontend/src/components/DailyActionCard.tsx
frontend/src/components/GoalProgressCard.tsx
frontend/src/components/AdjustmentProposalCard.tsx
frontend/src/hooks/useProgramTelemetry.ts

frontend/src/App.tsx
frontend/src/api.ts
frontend/src/types.ts
frontend/src/components/WorkspaceSidebar.tsx
frontend/src/pages/PlanPage.tsx
frontend/src/pages/TrajectoryPage.tsx
frontend/src/pages/ChatPage.tsx
frontend/src/styles.css
```

## 25. 评审清单

- [ ] GoalProgram 与一次 Run 的职责没有混淆；
- [ ] PlanDocument 仍然是用户可维护的计划资产；
- [ ] 已开始项目固定来源版本和 Hash；
- [ ] 计划编辑不会静默改变活动项目；
- [ ] 调整必须先 Proposal、后用户确认；
- [ ] 已完成历史不会被新版本重写；
- [ ] 完成、跳过和延期不依赖 LLM；
- [ ] LLM 只负责语义判断和内容生成；
- [ ] 执行记录不被当作长期记忆；
- [ ] 需要工具时复用现有审批与 Checkpoint；
- [ ] 提醒使用显式用户授权和幂等投递；
- [ ] SSE、轨迹和错误文案不暴露内部 JSON；
- [ ] 所有高风险模块先有失败测试；
- [ ] 未引入多 Agent、MCP、Shell、Redis、Celery或微服务；
- [ ] 真实模型和浏览器完整场景测试通过后才表述为完成。

## 26. 最终决策摘要

V1.1 的核心不是继续增加 Agent 能做多少事，而是让 Agent 对一个目标负责得更久、更清楚。

本设计保留现有 PlanDocument、Agent Runtime、工具审批、Checkpoint、记忆、通知和 SSE，只新增一个轻量的长期目标执行领域：GoalProgram 管理周期，DailyAction 管理今天的行动，Run 只处理真正需要模型或工具的一次任务。

用户看到的体验将从“这里有一份完整计划”变为：

> 今天先完成这两件事。做完告诉我；如果不合适，我会说明原因并提出调整，得到你确认后再改变后续计划。

这才符合 Better Agent“帮助用户推进目标、让用户变成更好的自己”的产品方向。
