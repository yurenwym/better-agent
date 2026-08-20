# 对话计划文档持久化与持续协作：V2 开发设计

> 文档状态：待评审
>
> 创建日期：2026-08-20
>
> 适用分支：`codex/personal-agent-v1`
>
> 评审对象：`2026-08-20-plan-persistence-design.md`

## 1. 结论

推荐把“计划内容”和“计划执行”拆成两个有明确边界的领域：

1. `PlanDocument` 属于 Conversation Runtime，是用户可查看、编辑、版本化的 Markdown 文档；
2. `PlanVersion` 属于 Agent Runtime，是批准执行时从某个固定文档版本生成的结构化执行快照；
3. 用户明确要求“创建、保存或修改计划”时，模型仍以 `policy=answer` 流式输出正文；正文完成后，同一份 Markdown 原文自动写入计划文档；
4. 保存计划文档不会创建 Goal、Run、审批、ReAct 预算或调用工具；
5. 只有用户明确要求提醒、持续跟踪、更新外部状态或执行计划时，才进入现有 `propose_execution -> continue_execution -> AgentRuntime`；
6. 后续对话自动读取当前线程的最新计划文档版本，并记录所读的 `plan_id + version + content_hash`；
7. 计划页编辑和对话修改都创建不可变新版本，使用 `expected_version + content_hash` 防止覆盖并发修改；
8. SQLite 中已提交的完整 Markdown 修订是权威版本账本；磁盘 `plan.md` 是真实存在、可恢复的工作副本；
9. 执行中的 Run 固定引用具体文档版本，后续文档修改不能静默改变已经批准的执行计划。

这份设计取代原草案中“显式保存计划就创建 `AWAITING_APPROVAL` Run”的默认做法。原因是保存一份可编辑内容与授权系统执行不是同一件事。

## 2. 用户承诺

系统对用户作出五项可验证承诺：

1. 聊天里看到的计划正文，就是计划页和 Markdown 文件中的正文，不重新概括、不从步骤反向渲染；
2. UI 显示“已保存 vN”时，对应版本已经进入 SQLite 版本账本，磁盘文件也已通过 hash 校验；
3. 用户在计划页修改后，下一轮对话读取的是新版本，而不是旧聊天消息；
4. 冲突时保留双方内容，绝不最后写入者静默覆盖；
5. 保存计划不等于批准执行，执行边界保持清晰。

## 3. 需求解释

### 3.1 “创建计划”与“给出计划”

以下请求只交付内容，不自动写入计划页：

- “给我一份桂林 7 天攻略。”
- “给我一个 6 周 Python 学习计划模板。”
- “有哪些骑行训练方法？”

以下请求明确要求持久化计划文档：

- “创建一个桂林 7 天旅行计划。”
- “把上面的攻略保存为我的旅行计划。”
- “把第 3 天改成龙脊梯田，并更新计划。”
- “将这份训练安排写入计划页面。”

不增加通用“转为目标”或“保存方案”按钮。模型通过受限控制协议声明本轮是否生成计划文档；前端只在保存成功后展示该计划专属的“查看 / 编辑计划”引用卡片。

### 3.2 读取与写入分离

线程存在计划文档后：

- 每轮对话默认读取最新已提交版本；
- “第 3 天穿什么？”只读取并回答，不修改文档；
- “把第 3 天的交通和餐饮补充进计划”读取并生成新文档版本；
- “每天早上提醒我当天行程”读取文档并进入执行预览，不直接执行。

### 3.3 V1 范围

V1 每个 Thread 最多关联一份活动计划文档。用户要创建第二份独立计划时，应新建 Thread。这个限制用数据库唯一约束表达，不预先建设多文档选择器。

## 4. 当前代码诊断

### 4.1 对话正文只进入消息表

当前链路为：

```text
POST /api/threads/{thread_id}/turns
  -> ConversationService.accept_turn()
  -> turn_jobs
  -> ManagedTurnWorker
  -> LiveConversationModel.route_and_respond()
  -> thread_messages.content
  -> turn COMPLETED
```

证据：

- `backend/app/api.py:144-170` 接收 Turn 并返回 `202`；
- `backend/app/conversation.py:488-567` 原子写入 Turn、用户消息、Job 和事件；
- `backend/app/conversation.py:776-853` 解码控制头并流式保存 Markdown；
- `backend/app/conversation.py:1003-1032` 将 `answer` 结束为 `COMPLETED`；
- `backend/app/live_model.py:226-240` 明确把 plans-as-deliverables 归为 `answer`。

该链路没有创建任何计划实体。聊天里看到的计划只是 Assistant Message。

### 4.2 执行链路会重新规划

当前 `propose_execution` 路径为：

```text
Turn AWAITING_DIRECTION
  -> 用户 continue_execution
  -> ExecutionMaterializer 创建 Goal / Session / Run
  -> AgentRuntime.handle_message(original_user_content)
  -> model.plan()
  -> PlanVersionService.create()
```

证据：

- `backend/app/conversation.py:426-445` 在方向确认后调用 Runtime；
- `backend/app/conversation.py:570-666` 创建 Goal、Session 和 Run；
- `backend/app/runtime.py:283-296` 再次调用规划模型并创建结构化 PlanVersion。

因此，执行计划不保证等于用户刚在聊天中看到的 Markdown。这是“看起来已经有计划，计划页却没有同一份计划”的直接原因。

### 4.3 当前计划页依赖 Run

- `backend/app/api.py:309-317` 只提供 `/api/runs/{run_id}/plans`；
- `frontend/src/pages/PlanPage.tsx:20-31` 没有 Run 就显示空态；
- `frontend/src/pages/PlanPage.tsx:57-68` 只展示摘要与步骤；
- `frontend/src/pages/PlanPage.tsx:11-12` 只允许编辑未完成步骤标题；
- `frontend/src/api.ts:200-210` 修订请求不提交 Markdown、步骤描述或原摘要。

当前 `plan_versions` 也只有 `summary + plan_steps`，没有完整 Markdown、标题、hash、来源消息或文档路径（`backend/app/db.py:137-161`）。

### 4.4 后续对话不会读取计划页修改

`backend/app/conversation.py:1184-1222` 的 `_history()` 只读取历史消息与 Ask 结果，不读取计划页的当前版本。因此即使结构化 PlanVersion 被修改，后续回答仍可能依据旧聊天正文。

### 4.5 现有 Memory 文件逻辑不能直接复制

`MemoryService` 提供了可借鉴的 hash、原子 `os.replace` 和版本记录，但其数据库更新、文件写入和事件追加并非同一故障协议：

- `backend/app/memory.py:110-128` 先更新数据库，再重写文件；
- `backend/app/memory.py:284-315` 先存文件版本记录，再替换文件；
- `backend/app/memory.py:234-261` 的手工编辑同步没有接入启动路径。

计划文档需要独立的 prepared/finalized 写入意图和恢复逻辑。

## 5. 子智能体评审与分歧

本轮创建了三个只读子智能体：

| 角色 | 关注范围 | 主要结论 |
|---|---|---|
| 代码链路审计 | 后端、前端、事务、恢复 | 普通计划只进入消息；执行确认后会重新规划；计划页、刷新恢复和后续聊天均有断点 |
| 架构红队 | 事实源、双写、并发、崩溃 | Markdown 修订账本、文件工作副本和执行快照必须分层，不能复制 Memory 的松散双写 |
| 交互与开源研究 | Artifact UX、版本、共享状态 | 计划应有稳定 URL、消息级引用、全文编辑、版本历史和明确的文档上下文 |

代码审计提出了一个反对意见：如果产品允许“保存计划”创建一个不执行的 `AWAITING_APPROVAL` Run，那么扩展现有 PlanVersion 会是更小的 V1。

本设计不采用该意见，理由是：

1. `AWAITING_APPROVAL` 会把“保存内容”和“授权执行”混在一起；
2. 当前前端在该状态禁用聊天，不符合持续细化计划的需求；
3. fast-first V2 已要求内容计划不得产生 `direct_run_leaks`；
4. 用户可能长期编辑旅行、学习或训练文档，但永远不要求系统执行；
5. 一个空 Run 仍会带来预算、状态、审批和恢复语义，成本大于一张窄的文档表。

## 6. 方案比较

### 6.1 方案 A：扩展现有 PlanVersion，并为保存创建 Run

做法：显式创建计划时生成 Goal、Run、PlanVersion，在 PlanVersion 增加 Markdown 字段。

优点：复用现有计划 API 和页面入口，短期文件数量较少。

缺点：

- 保存即产生 Run，违反会话与执行分层；
- 计划页继续依赖 Run；
- 后续聊天会被执行状态影响；
- 文档修改和执行步骤修改仍共用一个版本号；
- 用户尚未要求执行，却看到“批准计划”。

结论：不采用。

### 6.2 方案 B：PlanDocument + 执行快照

做法：Conversation Runtime 保存完整 Markdown 文档；用户确认执行时，从固定文档版本生成现有结构化 PlanVersion。

优点：

- 保存、编辑、对话和执行语义清晰；
- 聊天正文、计划页正文和文件正文可保持字节级一致；
- 后续对话可稳定加载最新版本；
- 已批准 Run 不会被后续文档编辑暗中改变；
- 不需要引入工作流框架或通用 Artifact 平台。

代价：新增两张核心表、一张恢复表和一组 thread/plan API。

结论：推荐。

### 6.3 方案 C：Markdown 文件为唯一事实源

做法：直接把 `plan.md` 当权威数据，SQLite 只保存路径和索引。

优点：用户可用任意编辑器修改，概念直观。

缺点：

- 文件写入不具备 SQLite 事务、唯一约束和事件顺序；
- 半写、非法 UTF-8、路径替换和并发编辑会污染当前事实；
- 很难证明模型读取了哪个版本；
- 不能安全固定执行 Run 的输入。

结论：不采用。文件是受控工作副本，不是版本账本。

## 7. 领域模型

```mermaid
flowchart LR
    T["Conversation Thread"] --> D["PlanDocument"]
    D --> DV1["DocumentVersion v1 / Markdown"]
    D --> DV2["DocumentVersion v2 / Markdown"]
    DV2 --> F["plans/{plan_id}/plan.md"]
    DV2 --> C["Conversation Context"]
    DV2 --> M["Execution Materializer"]
    M --> R["Goal / Run"]
    R --> PV["PlanVersion / structured steps"]
    PV --> X["Approve and execute"]
```

### 7.1 PlanDocument

稳定身份，归属于 Thread。它表示“用户正在持续完善的计划内容”，不表示执行状态。

### 7.2 PlanDocumentVersion

不可变的完整 Markdown 快照。每次模型保存、计划页保存、文件导入或历史恢复都创建新版本。

恢复历史版本的语义是“把旧内容复制成新的 vN+1”，不删除中间版本，不移动历史指针。

### 7.3 PlanVersion

保留现有含义：Agent Runtime 使用的结构化步骤和状态。新增来源引用后，它表示“文档版本 X 的执行投影”。

文档版本与执行版本使用独立版本号：

```text
PlanDocument v5  --compile-->  PlanVersion v2
```

二者不能假设数字相等。

## 8. 对话响应协议

### 8.1 控制头 V2

扩展现有单行控制头，不新增通用模型工具，也不解析 Markdown 结构：

```json
{
  "v": 2,
  "policy": "answer",
  "content_shape": "travel_plan",
  "reason_code": "explicit_plan_create",
  "artifact": {
    "kind": "plan_document",
    "operation": "upsert",
    "title": "桂林 7 天旅行计划"
  }
}
```

控制头之后的全部内容是用户可见 Markdown，也是真正保存的文档正文：

```markdown
# 桂林 7 天旅行计划

## 第 1 天：抵达桂林
上午抵达后入住酒店，下午游览两江四湖。
```

### 8.2 协议约束

- V1 控制头仍兼容读取；V2 才允许 `artifact`；
- `artifact` 只允许出现在 `policy=answer`；
- `artifact.kind` V1 只接受 `plan_document`；
- `artifact.operation` V1 只接受 `upsert`；
- 标题长度 1–120，正文 UTF-8 编码后不超过 1 MiB；
- 一个 Turn 最多提交一个 Artifact；
- 未知字段、未知 kind、正文为空或控制头非法都不能写计划；
- `clarify`、`ask_user`、`propose_execution` 不得同时创建文档；
- 模型不能提供 plan ID、版本号、路径、hash 或数据库字段；
- 不把控制头保存为消息或文档正文。

### 8.3 为什么不是 propose_plan 工具

原草案的 `propose_plan` 需要工具调用、后端响应和第二次模型回合，并产生两份可能漂移的内容：结构化步骤与最终 Markdown。

本方案只把控制头作为“是否保存正文”的权限描述。正文仍能立即流式展示；完成时直接版本化已经展示的同一份内容。执行结构化在用户真正要求执行之后再做。

### 8.4 写入授权

`answer` 的安全不变量收窄为：

- 普通 `answer` 无副作用；
- 只有包含合法 `plan_document` 描述、且模型基于明确创建/保存/修改意图生成的 `answer`，可创建本地、可逆、版本化的计划文档；
- 该例外不得扩展到任意文件路径、外部服务或 Agent Run。

路由质量通过评测约束，不使用 Python 关键词硬编码用户意图。

## 9. 数据模型

### 9.1 `plan_documents`

```text
id                       TEXT PRIMARY KEY
thread_id                TEXT NOT NULL UNIQUE
title                    TEXT NOT NULL
current_version_id       TEXT
projected_version_id     TEXT
file_status              TEXT NOT NULL
created_at               TEXT NOT NULL
updated_at               TEXT NOT NULL
```

`file_status` 取值：`pending | ready | conflict | failed`。

V1 的 `UNIQUE(thread_id)` 明确每个 Thread 一份活动计划，不设计多文档选择逻辑。

### 9.2 `plan_document_versions`

```text
id                       TEXT PRIMARY KEY
plan_document_id         TEXT NOT NULL
version                  INTEGER NOT NULL
base_version_id          TEXT
title                    TEXT NOT NULL
markdown_content         TEXT NOT NULL
content_hash             TEXT NOT NULL
source_turn_id           TEXT
source_message_id        TEXT
actor                    TEXT NOT NULL
change_summary           TEXT NOT NULL DEFAULT ''
status                   TEXT NOT NULL
created_at               TEXT NOT NULL
committed_at             TEXT
UNIQUE(plan_document_id, version)
UNIQUE(source_turn_id)
UNIQUE(source_message_id)
```

`actor` 取值：`model | user | filesystem | restore`。

`status` 取值：`prepared | committed | abandoned`。只有 `committed` 版本可进入对话上下文或执行物化。

### 9.3 `plan_write_intents`

```text
id                       TEXT PRIMARY KEY
plan_document_id         TEXT NOT NULL
version_id               TEXT NOT NULL UNIQUE
expected_head_version_id TEXT
expected_file_hash       TEXT
target_file_hash         TEXT NOT NULL
status                   TEXT NOT NULL
attempts                 INTEGER NOT NULL DEFAULT 0
last_error_json          TEXT
created_at               TEXT NOT NULL
finished_at              TEXT
```

`status` 取值：`PREPARED | FILE_WRITTEN | COMMITTED | CONFLICT | FAILED`。

### 9.4 现有表变更

`turns` 增加：

```text
artifact_kind            TEXT
artifact_operation       TEXT
artifact_title           TEXT
```

`thread_messages` 增加：

```text
plan_document_version_id TEXT
```

`plan_versions` 增加：

```text
source_document_version_id TEXT
```

并为 `runs.source_turn_id` 增加部分唯一索引：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_source_turn
ON runs(source_turn_id)
WHERE source_turn_id IS NOT NULL;
```

模型调用 ID 和 Message ID 只用于审计，不作为计划创建的事实幂等键。Conversation Artifact 的幂等键是 `source_turn_id`；执行物化的幂等键仍由执行预览 Turn 的 `source_turn_id` 和方向选择键保证。SQLite 的 UNIQUE 允许多行 `NULL`，因此计划页、文件导入和历史恢复版本可以没有 source turn。

## 10. Markdown 文件

### 10.1 路径

服务端只用 UUID 生成固定路径：

```text
<data_root>/plans/<plan_document_id>/plan.md
```

标题、用户文本和模型输出不得参与文件名。所有路径解析后必须位于 `<data_root>/plans`，并拒绝符号链接、junction 和 reparse point 跳转。

`data/plans/` 必须加入 `.gitignore`，不得提交用户计划内容。

### 10.2 正文

`plan.md` 的正文等于当前已提交版本的 `markdown_content`。不添加模型不可见的 JSON 块，不用 HTML 注释保存结构化步骤，也不从 PlanStep 重新渲染正文。

计划元数据由 SQLite 管理。计划页 API 返回 ID、版本、hash、来源和文件相对路径。

写入前统一执行最小规范化：把 `CRLF` 和孤立 `CR` 转为 `LF`，拒绝 NUL，不做 Unicode NFC/NFKC 转换，不裁剪行尾空格，也不擅自增加或删除末尾换行。消息表、版本表和文件都保存规范化后的同一个字符串；hash 定义为其无 BOM UTF-8 字节的 SHA-256，格式为 `sha256:<hex>`。文件写入必须显式使用 `newline="\n"`，避免 Windows 文本模式把 LF 再转换为 CRLF。

### 10.3 外部编辑

V1 允许用户直接编辑 `plan.md`，但文件只作为新修订的输入通道：

1. 计划页读取、显式“重新载入文件”和模型上下文装配前检查文件 hash；
2. 文件 hash 与当前已投影版本相同：无变化；
3. 文件不同、且数据库 head 仍等于投影版本：稳定读取 UTF-8 内容，导入为新版本；
4. 文件和数据库 head 都已变化：标记 `conflict`，保存双方内容，不自动选择；
5. 非 UTF-8、超过 1 MiB、读取过程中大小/mtime 变化或路径异常：拒绝导入并显示原因。

外部编辑器不遵守应用锁，因此系统不宣称跨任意编辑器的绝对原子性。系统保证的是：检测到第三方 hash 时不静默覆盖，并保留 SQLite 中所有已提交版本。

## 11. 写入与崩溃恢复

### 11.1 模型回答保存流程

```text
模型完成 Markdown
  -> 校验控制头和完整正文
  -> SQLite PREPARE revision + write_intent
  -> 校验当前文件仍是 expected_file_hash
  -> 同目录临时文件写入 + flush + fsync
  -> os.replace(temp, plan.md)
  -> 回读并校验 target hash
  -> SQLite FINALIZE head + message link + events
  -> turn.completed
```

SQLite PREPARE 事务包含：

- 创建或读取当前 Thread 的 PlanDocument；
- 基于当前 head 分配下一个版本；
- 写入完整 Markdown、hash 和来源；
- 写入 `plan_write_intents`；
- 依赖 `UNIQUE(source_turn_id)` 保证模型重试、generation 重置和 Message ID 变化都不会生成两个版本；`source_message_id` 负责把最终可见正文与版本精确关联。

SQLite FINALIZE 事务包含：

- CAS 检查 `current_version_id == expected_head_version_id`；
- 把版本置为 `committed`；
- 推进 `current_version_id` 和 `projected_version_id`；
- 把 `thread_messages.plan_document_version_id` 指向该版本；
- 依次追加 `plan.document_version_created`、`plan.document_ready`；
- 最后追加 `message.completed` 与 `turn.completed`；
- 完成 Turn Job。

`plan.document_ready` 必须排在 `turn.completed` 前，因为前端在终态后可能关闭 SSE。

### 11.2 API 编辑

计划页提交：

```json
{
  "expected_version": 3,
  "expected_content_hash": "sha256:<64-hex>",
  "title": "桂林 7 天旅行计划",
  "markdown": "# 桂林 7 天旅行计划\n\n## 第 1 天：抵达桂林",
  "change_summary": "调整第 3 天路线"
}
```

保存使用同一个 PREPARE / FILE / FINALIZE 协议。版本或 hash 不匹配返回 `409 Conflict`，响应包含当前版本元数据；服务器不接收客户端提供的 plan ID、路径或新版本号。

### 11.3 恢复流程

启动时和 Managed Worker 领取任务前扫描未完成 intent：

| 数据库状态 | 文件 hash | 恢复动作 |
|---|---|---|
| `PREPARED` | expected hash / 文件不存在 | 重新执行原子文件写入 |
| `PREPARED` | target hash | 直接进入 FINALIZE |
| `FILE_WRITTEN` | target hash | 重试 FINALIZE |
| 任意未完成 | 第三个 hash | 标记 `CONFLICT`，不覆盖文件 |
| 已 `COMMITTED` | 文件缺失 | 从 committed Markdown 重建文件 |
| 已 `COMMITTED` | 非当前 hash | 进入外部编辑检测，不直接覆盖 |

重试不重新调用模型。经过校验的 Markdown 已在 prepared revision 中，可以原样恢复。

### 11.4 SQLite 耐久性

当前连接使用 WAL 和 `synchronous=NORMAL`。计划文档 head 推进、版本提交和写入意图应使用专用 `durable_transaction()`，在事务前设置 `PRAGMA synchronous=FULL`；高频消息 delta 仍可使用现有普通事务。

## 12. 状态与事件

### 12.1 文档状态不是 Turn 状态

Turn 继续使用：

```text
ACCEPTED -> ROUTING -> STREAMING -> COMPLETED
```

文档保存状态通过 PlanDocument 和事件表达，不增加 `SAVING_PLAN` 顶层 Turn 状态。

### 12.2 语义事件

新增 Thread 事件：

```text
plan.document_prepared
plan.document_version_created
plan.document_ready
plan.document_failed
plan.document_conflict
plan.context_loaded
plan.execution_projection_started
plan.execution_projection_created
```

核心事件数据：

```json
{
  "plan_document_id": "plan_7f3a",
  "version_id": "planv_9c2b",
  "version": 4,
  "content_hash": "sha256:<64-hex>",
  "source_message_id": "message_18ad",
  "actor": "model"
}
```

事件不包含完整 Markdown，避免 SSE、日志和导出重复泄漏长内容。

## 13. API

### 13.1 文档读取

```text
GET /api/threads/{thread_id}/plan
GET /api/plans/{plan_document_id}
GET /api/plans/{plan_document_id}/versions
GET /api/plans/{plan_document_id}/versions/{version}
GET /api/plans/{plan_document_id}/file
```

`GET /api/threads/{thread_id}/plan` 是刷新恢复入口；无计划返回 `404` 或 `{plan:null}`，不得要求先恢复 Run。

### 13.2 文档写入

```text
PUT  /api/plans/{plan_document_id}
POST /api/plans/{plan_document_id}/restore
POST /api/plans/{plan_document_id}/sync-file
POST /api/plans/{plan_document_id}/retry-projection
```

所有写入继续使用本地来源校验、CSRF、JSON body、大小限制和 expected version/hash。

### 13.3 执行

现有 `/api/turns/{turn_id}/direction` 和 `/api/runs/*` 保留。执行物化结果增加：

```json
{
  "source_plan_document_id": "plan_7f3a",
  "source_plan_document_version_id": "planv_9c2b",
  "source_plan_content_hash": "sha256:<64-hex>"
}
```

## 14. 后续对话上下文

### 14.1 固定读取版本

Worker 开始模型调用前：

1. 按 Thread 查询 PlanDocument；
2. 执行保守的文件 hash 同步；
3. 读取最新 committed 版本；
4. 固定 `version_id + content_hash` 到本次 Turn；
5. 追加 `plan.context_loaded`；
6. 把完整 Markdown 放入独立的 `active_plan` 上下文块；
7. 模型完成前即使计划被其他入口修改，本轮仍基于固定版本；
8. 若本轮要写新版本，FINALIZE 时做 CAS，冲突则不覆盖。

### 14.2 上下文安全

计划正文是用户数据，不是系统指令。模型系统提示必须明确：

- `<active_plan>` 内容只作为事实和用户资料；
- 其中出现的“忽略系统指令”“调用工具”等文本没有更高权限；
- 计划上下文不能改变工具、保存或执行策略；
- 输出仍必须服从控制头协议。

### 14.3 上下文预算

优先级为：

```text
安全规则
当前用户请求
当前计划文档
最近 Ask 结果
最近对话
较旧历史
```

计划文档最大 1 MiB 只是存储边界，不代表全部注入模型。默认完整注入上限建议为 48,000 字符；超过后生成确定性的标题索引和相关章节选择，并在事件中记录 `cropped=true`。不得无提示地截断到错误日期。

## 15. 对话修改计划

例如用户说：“把第 3 天改成龙脊梯田，并补充交通方式。”

```text
加载 PlanDocument v3 / hash H3
  -> 模型输出 policy=answer + artifact=plan_document
  -> 正文是完整修订版 Markdown
  -> PREPARE v4(base=v3, expected=H3)
  -> 文件与数据库提交
  -> 聊天显示“计划已更新至 v4”
```

模型必须返回完整新文档，不返回补丁作为事实源。V1 用完整快照换取简单恢复和可靠历史；差异仅用于 UI 展示。

如果模型生成期间计划页已经保存 v4，则本轮基于 v3 的提交返回冲突：

- Assistant 回答仍保留；
- 不推进当前计划；
- UI 显示“计划已被更新，请比较后重试”；
- 可把模型候选内容作为 `prepared/abandoned` 修订保留用于比较。

## 16. 从文档到执行

### 16.1 触发条件

以下请求进入 `propose_execution`：

- “按这份旅行计划每天提醒我。”
- “每天根据完成情况更新训练进度。”
- “按计划创建本地清单并持续维护。”

执行预览必须显示使用的计划标题和版本，例如“将基于《桂林 7 天旅行计划》v4 创建提醒任务”。

### 16.2 Materialization

用户确认后：

1. 固定当前 `PlanDocumentVersion`；
2. 在同一事务创建或复用 Goal、Session、Run，并保存来源版本；
3. 调用受限 `compile_plan_document(markdown, previous_plan?)` 生成 `PlanDraft`；
4. 校验非空步骤、长度、重复 ID 和允许状态；
5. 创建现有结构化 PlanVersion；
6. 更新 Run 指针并写入投影事件；
7. 进入 `AWAITING_APPROVAL`；
8. 用户批准后才执行。

编译调用发生在用户确认执行之后，可以使用额外模型调用；它不阻塞最初的计划内容流式回答。

### 16.3 运行中修订

文档 v5 不会自动替换基于 v4 的 Run。计划页显示：

```text
文档当前版本：v5
执行计划来源：文档 v4
状态：有未同步修改
```

用户选择“将 v5 应用到执行计划”后，才生成结构化 PlanVersion v2，并重新审批。完成步骤保持不可变；未完成步骤基于旧结构化计划和新文档共同编译。

## 17. 前端交互

### 17.1 聊天页

收到 `plan.document_ready` 后，在来源消息下显示：

```text
已保存到计划 · v1
[查看 / 编辑计划]
```

该卡片是已存在计划的引用，不是通用保存按钮。保存失败时保留完整回答，显示“计划文件写入失败 · 重试”，重试不调用模型。

### 17.2 计划页

路由使用稳定 ID：

```text
/plans/{plan_document_id}
```

页面包含：

- 标题、版本、保存状态和文件相对路径；
- Markdown 编辑区与预览区；
- 保存、撤销本地改动、重新载入文件；
- 历史版本列表、查看差异、恢复为新版本；
- 冲突比较与“保留当前 / 导入文件 / 合并后保存”；
- 若存在 Run，显示执行来源版本和是否过期；
- “批准并执行”只出现在真实结构化 PlanVersion 上。

V1 使用原生 `textarea`、现有 Markdown renderer 和简单行级 diff，不新增富文本编辑器或 CRDT 依赖。

### 17.3 刷新恢复

`plan_id` 和 `thread_id` 进入 URL。应用启动时按 URL 获取快照，不依赖 React 内存中的 `run`。ChatPage 后续消息始终走 Conversation Turn；Run 作为关联执行上下文，不改变消息入口，也不在 `AWAITING_APPROVAL` 时禁用计划讨论。

### 17.4 未来分屏

热门 Artifact 产品常采用聊天 + 文档分屏。V1 先实现稳定独立计划页和消息引用；数据模型稳定后可把同一页面嵌入右侧面板，不为分屏提前复制状态管理。

## 18. 事务与服务边界

新增最小组件：

| 组件 | 职责 |
|---|---|
| `PlanDocumentService` | 校验、PREPARE、FINALIZE、版本读取、CAS、历史恢复 |
| `PlanFileProjector` | 固定路径、原子写入、hash 校验、恢复 intent、外部编辑检测 |
| `PlanContextProvider` | 为 Turn 固定并装配当前计划版本 |
| `PlanExecutionCompiler` | 明确执行后把固定 Markdown 版本投影为 PlanDraft |

不新增通用 Artifact 框架、后台队列依赖、Redis、文件 watcher、CRDT 或微服务。

现有 `PlanVersionService.create/revise()` 应支持调用方传入 connection，或提供明确的事务内方法。不能在 Materializer 的大事务中再次自行打开连接。

## 19. 错误语义

| 场景 | 用户可见结果 | 系统行为 |
|---|---|---|
| 模型协议非法 | 回答失败，可重试 | 不创建 prepared revision |
| 正文生成中取消 | 保留取消语义 | 不保存部分 Markdown |
| SQLite PREPARE 失败 | 回答保留，计划未保存 | 不写文件 |
| 文件写入失败 | 回答保留，显示重试 | intent 保持可恢复 |
| 文件第三方修改 | 显示冲突 | 不覆盖文件或 head |
| FINALIZE 前崩溃 | 重启后恢复 | 按 intent 和 hash 决定重放/完成 |
| API 版本冲突 | 保留编辑草稿 | 返回当前版本和 409 |
| 执行编译失败 | 文档仍有效 | 不创建半成品 PlanVersion |
| 文档被修改而 Run 在执行 | 显示“未同步” | 已批准 Run 固定旧版本 |

## 20. 安全不变量

1. 普通 `answer` 不创建 PlanDocument、Goal、Run、PlanVersion 或工具调用；
2. PlanDocument 保存不创建 Goal、Run、PlanVersion、审批或 ReAct 预算；
3. 未执行 `continue_execution` 前不存在该文档对应的 Agent Run；
4. 未批准结构化 PlanVersion 前不执行工具；
5. LLM 不能选择文件路径、ID、版本号、hash 或 SQL；
6. 文件路径必须留在固定 root，拒绝 traversal、绝对路径和链接跳转；
7. SSE、日志和事件不携带完整 Markdown；
8. 只有 committed 文档版本可进入上下文或执行；
9. 已批准 Run 的来源文档版本不可变；
10. 冲突不得通过 last-write-wins 静默解决；
11. 重试保存不得重新调用模型或创建第二个版本；
12. 文件删除可以从 SQLite committed revision 恢复。

## 21. 测试策略

### 21.1 协议单元测试

- V1 / V2 控制头兼容；
- Artifact 仅允许 `answer + plan_document + upsert`；
- 未知字段、非法标题、空正文和超限正文被拒绝；
- 控制头永不进入消息和 Markdown 文件；
- Ask 与 Artifact 不可混合；
- 模型重置 generation 时只保存最终 ready 消息。

### 21.2 文档服务测试

- 第一次保存创建 v1；
- 同一 source message 重试返回相同版本；
- 计划页保存创建 v2；
- history restore 创建 v3，不删除 v2；
- expected version/hash 冲突不推进 head；
- 标题和正文 Unicode round-trip；
- committed DB Markdown、消息正文和文件正文 hash 相同；
- 删除文件后可重建；
- 外部文件编辑导入为新版本；
- DB 和文件并发修改进入 conflict；
- 非 UTF-8、超限、symlink/reparse point 被拒绝。

### 21.3 故障注入

在以下边界逐点崩溃并重启：

1. revision PREPARE 前；
2. revision PREPARE 后；
3. 临时文件写入后；
4. `os.replace` 后；
5. 文件 hash 校验后；
6. head FINALIZE 前；
7. plan 事件后、turn.completed 前。

每个边界最终只能得到一个 committed 版本，或一个明确 conflict/failed intent，不能丢失双方内容。

### 21.4 Conversation 集成测试

- “给我桂林攻略”只产生消息；
- “创建桂林 7 天计划”产生消息和 PlanDocument v1，Agent 表记录为零；
- Ask 完成后的计划只保存一次；
- 后续“细化第 3 天”读取 v1 并生成 v2；
- 后续纯问答读取 v2 但不生成 v3；
- 模型调用审计中的 plan version/hash 与开始调用时一致；
- 页面编辑期间模型基于旧版提交会冲突；
- `plan.document_ready` 排在 `turn.completed` 前；
- Worker lease 重试、重复 POST、SSE 重连和服务重启不重复版本。

### 21.5 执行集成测试

- 仅保存文档不创建 Run；
- 明确提醒/跟踪请求才出现执行预览；
- 双击继续执行只创建一个 Run；
- Run 引用准确的 document version/hash；
- 编译失败不留下孤立 PlanVersion；
- 未批准前工具调用数为零；
- 文档新版本不改变执行中的 Run；
- 应用新文档版本会创建 PlanVersion v2 并重新审批。

### 21.6 前端测试

- `plan.document_ready` 显示版本卡片；
- 直接访问 `/plans/{id}` 和刷新可恢复；
- Markdown 编辑、预览、保存和历史恢复；
- 409 时保留本地草稿并显示比较；
- 文件失败时回答不丢失且可重试；
- 等待执行审批时仍可继续对话；
- 计划页没有 Run 时也正常工作；
- 文档版本与执行版本过期提示准确。

## 22. 指标

| 指标 | 目标 |
|---|---:|
| 明确创建计划后 `plan.document_ready` p95 | ≤ 500 ms（模型完成后） |
| 显示“已保存”后计划页空白 | 0 |
| 消息 / DB / 文件 hash 不一致 | 0 |
| 同一 source message 重复版本 | 0 |
| stale write 静默覆盖 | 0 |
| 后续模型错读旧版本 | 0 |
| 保存计划导致 Run 泄漏 | 0 |
| 未批准前工具调用 | 0 |
| 冲突时用户草稿丢失 | 0 |
| 重启后 committed 文档恢复率 | 100% |

另记录：

- `plan_artifact_false_positive_rate`：普通回答被误保存比例；
- `plan_artifact_false_negative_rate`：明确创建计划但未保存比例；
- `plan_context_crop_rate`：计划上下文被裁剪比例；
- `plan_file_conflict_rate`：外部编辑冲突比例；
- `document_to_execution_drift`：执行快照未引用明确文档版本的次数，目标为 0。

## 23. 分阶段实施

### 阶段 0：修正现有计划页数据损失

- 修订 API 不再清空旧 summary/description；
- 增加缺失的 PlanPage 测试；
- 为 `runs.source_turn_id` 添加唯一约束。

### 阶段 1：SQLite PlanDocument 账本

- 新增三张表和 `PlanDocumentService`；
- 完成版本、CAS、幂等、历史恢复和 durable transaction 测试；
- 暂不接模型和文件。

### 阶段 2：Markdown 文件投影与恢复

- 实现固定路径、原子写、hash、write intent 和启动恢复；
- 实现保守外部编辑检测与冲突；
- 将 `data/plans/` 加入忽略规则。

### 阶段 3：对话协议与自动保存

- 控制头升级 V2；
- Worker 在成功正文完成时 PREPARE / PROJECT / FINALIZE；
- 添加 plan 语义事件和失败重试；
- 真实模型评测普通攻略、明确创建、Ask 后创建和修改计划。

### 阶段 4：计划页与 URL 恢复

- 新增 thread/plan API；
- 实现稳定路由、Markdown 编辑/预览、历史和冲突 UI；
- Chat 引用卡片跳转计划页。

### 阶段 5：计划上下文

- 新增 `PlanContextProvider`；
- 每轮固定版本/hash并记录事件；
- 实现后续读取、全文修订和并发冲突测试。

### 阶段 6：执行投影

- ExecutionMaterializer 固定文档版本；
- 新增编译器和 transaction-aware PlanVersionService；
- 实现文档/执行版本漂移提示和重新审批。

## 24. 验收场景

### 24.1 创建桂林计划

用户：

> 创建一个桂林 7 天旅行计划，预算 6000 元，节奏轻松。

系统：

1. Router 选择 `policy=answer` 和 `artifact=plan_document`；
2. Markdown 立即流式输出；
3. 完成后写入 PlanDocument v1 和 `plan.md`；
4. 聊天显示“已保存到计划 · v1”；
5. 计划页展示与聊天完全相同的 Markdown；
6. Goal、Run、PlanVersion、Approval 和 ToolCall 数量均为 0。

### 24.2 细化某一天

用户：

> 把第 3 天的交通、午餐和下雨备选方案补充进计划。

系统：

1. 固定读取 v1/hash；
2. 模型输出完整修订文档；
3. CAS 创建 v2；
4. 计划页立即显示 v2，v1 可查看；
5. 其他日期内容保持不变。

### 24.3 只询问，不写入

用户：

> 第 3 天大概需要走多少路？

系统读取 v2 后回答，但不创建 v3。

### 24.4 外部编辑

用户直接修改 `plan.md` 后继续聊天。系统在装配上下文前检测 hash，若数据库没有并发修改，导入为 v3 并基于 v3 回答；若计划页同时保存了 v3，则进入冲突页，不覆盖任何一方。

### 24.5 持续执行

用户：

> 旅行前 7 天开始，每天提醒我检查当天的准备事项。

系统展示基于文档 v3 的执行预览。用户确认后创建 Run，编译结构化 PlanVersion，等待批准；文档本身保持可编辑。

## 25. 明确不做

- 不建设通用 Artifact 平台；
- 不为 V1 支持一个 Thread 多份计划；
- 不用正则或 Markdown 标题解析生成执行步骤；
- 不在计划正文中嵌入隐藏 JSON；
- 不新增 Redis、Celery、文件 watcher、CRDT 或协同编辑服务；
- 不自动合并并发 Markdown；
- 不让文件路径由模型或标题决定；
- 不让文档修改自动改变执行中的 Run；
- 不把“创建计划文档”恢复成通用“添加目标”按钮。

## 26. 开源实践参考

以下参考只用于验证交互与状态模式，不引入其框架：

| 项目 / 规范 | 借鉴点 | 不照搬 |
|---|---|---|
| [Open WebUI Notes](https://docs.openwebui.com/features/notes/) | 文档是独立产物，AI 围绕文档继续协作 | 不在每轮无限注入全文 |
| [Vercel AI Chatbot Artifact](https://github.com/vercel/ai-chatbot/blob/main/components/chat/artifact.tsx) | 聊天消息到稳定 Artifact 面板 | 不复制其整套 Next.js 架构 |
| [Vercel Document Preview](https://github.com/vercel/ai-chatbot/blob/main/components/chat/document-preview.tsx) | 消息级文档引用和预览 | 不允许覆盖式无 CAS 编辑 |
| [Vercel Version Footer](https://github.com/vercel/ai-chatbot/blob/main/components/chat/version-footer.tsx) | 版本浏览和回退交互 | 恢复不删除后续历史 |
| [AI SDK 消息持久化](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-message-persistence) | 稳定 ID、刷新加载、持久化前校验 | 不引入 SDK 依赖 |
| [AG-UI State](https://docs.ag-ui.com/concepts/state) | Snapshot + delta、缺口后重取快照 | V1 不持久化 JSON Patch |
| [CopilotKit Shared State](https://docs.copilotkit.ai/langgraph-python/shared-state) | UI 与 Agent 围绕共享状态协作 | 不引入 LangGraph/CopilotKit Runtime |
| [LangGraph Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers) | latest/history/parent/restore 模型 | 不替换现有薄 Runtime |
| [Dify Version Control](https://docs.dify.ai/en/cloud/use-dify/build/version-control) | Current/Previous 与变更说明 | 不建设 workflow 发布系统 |
| [RFC 9110 If-Match](https://www.rfc-editor.org/rfc/rfc9110.html#name-if-match) | 条件写入与 lost-update 防护 | API 可继续使用等价 expected version/hash |

## 27. 最终设计判断

本问题不是“把聊天 Markdown 再复制一份”这么简单。真正需要建立的是一个稳定协作对象：

```text
明确创建计划
  -> 流式生成同一份 Markdown
  -> 版本化计划文档
  -> 计划页查看和修改
  -> 后续对话固定读取最新版本
  -> 明确执行时生成结构化执行快照
```

最关键的边界是：

```text
PlanDocument = 用户与模型共同维护的内容事实
PlanVersion  = Agent Runtime 获得授权后使用的执行事实
```

这样既满足“计划真实进入计划页并形成 Markdown 文件”，也延续 fast-first 的会话优先原则：内容先成为可编辑文档，执行仍然需要明确授权。
