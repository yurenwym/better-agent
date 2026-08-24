# Better Agent 多 Agent 协同与受控自进化开发方案

> 状态：设计提案，供确认后进入实施计划
>
> 日期：2026-08-24
>
> 适用阶段：Better Agent V1.2 / V2 演进
>
> 核心原则：一个面向用户的 Better Agent；多个受约束的专家执行者；所有改进必须经过证据验证、受控发布并可回滚

## 1. 结论

Better Agent 下一阶段可以建设异步多 Agent 协同和自进化，但二者不能被实现为“多个模型自由群聊”和“Agent 直接重写自己”。正式架构采用：

```text
多 Agent：持久任务 + 只读上下文快照 + 类型化 Artifact + 中心化提交

自进化：经验观察 + 候选版本 + 隔离评测 + 审批 + 灰度 + 晋升/回滚
```

面向用户始终只有一个 Better Agent。专家模式是主 Agent 在复杂任务中按需启用的内部工作方式；自进化是系统在真实证据支持下提出的改进候选。子 Agent 和候选生成器都不能直接修改正式计划、长期记忆、在线 Skill、Prompt、权限策略或生产代码。

本方案首先复用当前 SQLite WAL、原生 `sqlite3`、Conversation/Research/Goal Review Worker、事件、SSE、Checkpoint、工具审批、Skill allowlist、记忆提案与版本机制。第一阶段不引入 Redis、Celery、消息中间件、微服务或通用工作流框架。

## 2. 背景

Better Agent 的使命不是回答一次问题，而是长期帮助用户设定目标、制定计划、采取行动、处理困难、复盘并持续改善。当前项目已经形成了对话、计划、每日行动、研究、工具审批、记忆和目标复盘等纵向闭环。

随着任务复杂度提高，单一顺序 ReAct 会出现四类瓶颈：

1. 研究、规划、风险检查和个性化适配串行执行，用户等待时间过长；
2. 单一模型视角容易遗漏事实、约束或反例；
3. 成功和失败轨迹被保存，但尚未稳定转化为可复用能力；
4. Prompt、Skill 和策略仍主要依赖人工修改，缺少证据驱动的改进闭环。

与此同时，直接加入多 Agent 和自动改 Prompt 会扩大竞态、重复副作用、权限越界、成本失控、评测污染和行为漂移风险。因此需要在现有可靠性内核之上增加两个受控领域，而不是替换当前 Runtime。

## 3. 当前代码基线与真实边界

### 3.1 已经存在，可复用

- SQLite WAL、`BEGIN IMMEDIATE` 短事务、显式 migration 和数据库约束；
- Conversation `turn_jobs` 的持久排队、claim、lease、取消和失败恢复；
- Research Job 的 claim、heartbeat、attempt、恢复分节、取消、重试和终态提交；
- Goal Daily Review 的持久队列、lease、重试和调整 Proposal；
- Run Checkpoint、工具调用回执、WRITE 审批及重复副作用防护；
- Thread/Run/Goal Program append-only 事件及单调 `seq`；
- SSE 流式消息和轨迹展示；
- PlanDocument、Program、Memory 的版本/CAS/提案机制；
- Skill Catalog 和运行时工具白名单交集；
- 确定性 eval、模型网关指标和脱敏导出测试。

### 3.2 尚未存在，本方案需要新增

- 统一的父任务/子任务树和专家角色协议；
- 类型化 Agent Artifact 与不可变 Context Snapshot；
- fan-out/fan-in、join condition、迟到结果 fencing 和树级取消；
- 多 Agent 的分层预算预留与成本归因；
- 经验记录、失败聚类和改进机会识别；
- Skill、Policy、Prompt 的统一版本包和候选状态机；
- 候选与基线的隔离对照评测、证明清单和晋升门禁；
- canary 路由、在线监控和自动回滚；
- 用户可理解的专家轨迹、统一待审批和成长管理页面。

因此，当前项目具备实现基础，但不能称为已经实现多 Agent 或自进化。

## 4. 产品目的

### 4.1 多 Agent 协同目标

- 对可并行的复杂任务缩短总等待时间；
- 用独立专家提高事实覆盖、方案质量和风险发现能力；
- 每个专家的输入、权限、预算、产出和失败都可追踪、可恢复；
- 保持一个最终负责人，避免多个 Agent 给用户互相冲突的答案；
- 不因并行而放松 WRITE 审批、幂等和状态机约束。

### 4.2 自进化目标

- 从多次真实任务而非单次印象中识别稳定改进机会；
- 将经验转化为 Memory、Skill、Policy、Prompt 或代码候选；
- 使用确定性测试、历史回放、对照评测和线上指标验证收益；
- 让用户或开发者看见变更、证据、风险和影响范围；
- 只发布通过门禁的版本，并允许快速回滚；
- 防止模型修改安全边界、评测规则或自身晋升条件。

### 4.3 非目标

第一阶段不建设：

- 多 Agent 群聊或角色扮演展示；
- 子 Agent 共享可变内存对象；
- 子 Agent 之间传递隐藏思维链；
- 并行 WRITE 工具调用；
- 子 Agent 自行创建无限后代；
- 在线自动重写核心 System Prompt；
- 生产 Agent 直接修改和部署自身代码；
- Redis、Celery、Kafka、微服务或 Kubernetes 调度；
- 以 LLM Judge 取代安全、状态机和幂等测试。

## 5. 总体架构

```text
用户
  |
  v
Coordinator / 主 Agent
  |-- 判断普通模式或深入处理
  |-- 固定 Context Snapshot 与 Runtime Bundle
  |-- 创建有界父子任务树
  |-- 合并、裁决、请求审批、提交正式状态
  |
  +--> SQLite Agent Task Queue
          |-- lease + lease_epoch + heartbeat
          |-- retry + cancel propagation
          v
       Expert Workers
          |-- 只读上下文
          |-- READ/纯计算工具
          |-- 类型化 Artifact
          v
       Artifact Store
          |
          v
       Join + Validator + 可选 Critic
          |
          v
       Coordinator Commit Gate
          |-- Plan / Goal / Memory / 用户回复
          |-- WRITE 工具审批与顺序执行
          v
       Append-only Events -> SSE -> 用户可读轨迹

真实任务轨迹/反馈
  -> Experience Records
  -> Evolution Candidate
  -> Offline Evaluation
  -> Approval
  -> Canary
  -> Promote or Rollback
```

架构口诀：

> 共享数据库，但不共享可变状态；消息协调任务，Artifact 携带结果，快照固定输入，事件展示进展，主 Agent 提交权威状态。

## 6. 多 Agent 协同设计

### 6.1 角色

首版只定义少量通用执行角色，角色是受约束的任务模板，不是常驻人格：

| 角色 | 职责 | 默认工具 |
|---|---|---|
| Coordinator | 拆分、调度、合并、最终承诺 | 读取全部必要 Artifact；唯一正式提交者 |
| Researcher | 搜索、证据提取、来源核验 | Web/本地只读 |
| Planner | 将目标、约束和证据转成候选方案 | 纯模型/只读上下文 |
| Domain Specialist | 针对健康、学习、旅行等领域检查 | 领域 Skill 允许的 READ 工具 |
| Critic | 按 rubric 检查候选结果 | 只读 Artifact，不读取隐藏推理 |

不建立“所有角色都能互相发消息”的总线。Coordinator 创建任务，专家返回 Artifact；只有在明确依赖时，后续任务才读取前置 Artifact。

### 6.2 启用条件

默认使用单 Agent。满足以下任意两项，或用户明确要求“深入处理/多角度/交叉验证”时，Coordinator 可以建议专家模式：

- 能拆分为至少两个相对独立的子问题；
- 涉及多个专业领域；
- 高风险决策需要独立核验；
- 需要大量搜索、比较或证据验证；
- 当前置信度较低或同类任务曾失败；
- 并行后的预期收益高于额外成本。

简单问答、单步修改、严格串行任务、重复视角和低预算场景禁止自动 fan-out。

纯本地只读且在预授权预算内可以自动启用，同时给出可停止提示。涉及额外付费、外部数据、权限扩大或 WRITE 行为时必须先征得同意。

### 6.3 父子通信协议

Coordinator 发送的是持久化任务信封：

```json
{
  "task_id": "agent_task_01",
  "parent_task_id": null,
  "role": "researcher",
  "objective": "核验三个候选训练方案的依据和风险",
  "context_snapshot_id": "context_17",
  "input_artifact_ids": [],
  "allowed_tools": ["web_search", "read_note"],
  "output_schema": "research_brief.v1",
  "budget": {
    "max_model_calls": 4,
    "max_tool_calls": 8,
    "max_tokens": 24000,
    "deadline_seconds": 180
  }
}
```

子 Agent 返回的是不可变、类型化 Artifact：

```json
{
  "artifact_type": "research_brief",
  "schema_version": 1,
  "summary": "……",
  "claims": [
    {
      "text": "……",
      "source_refs": ["source_1"],
      "confidence": 0.86
    }
  ],
  "risks": [],
  "open_questions": [],
  "recommended_next_action": "……"
}
```

不得传递：完整数据库快照、无界对话历史、API Key、父 Agent 审批票据、内部 System Prompt、原始 chain-of-thought。

### 6.4 Context Snapshot

每个任务固定一个只读快照，至少包含：

- owner/project/thread/run 标识；
- 用户目标与明确约束；
- PlanDocumentVersion / ProgramVersion；
- Memory revision IDs 与 episode IDs；
- 已选择 Skill 的名称、版本和 digest；
- Policy/Prompt/模型配置版本；
- 输入内容 Hash 和创建时间。

快照创建后不可修改。新信息需要生成新快照和新任务/attempt，不能静默改变运行中的子 Agent 上下文。

### 6.5 Task 状态机

```text
QUEUED -> RUNNING -> SUCCEEDED
           |  |
           |  +-> WAITING_CHILDREN -> QUEUED
           |                         （join 满足后由父任务继续合并）
           +-> FAILED

QUEUED/RUNNING/WAITING_CHILDREN -- cancel --> CANCELLED
RUNNING -- lease 过期且 attempts 未耗尽 --> QUEUED
RUNNING -- lease 过期且 attempts 耗尽 --> FAILED
```

父任务通过确定性 `child_key` 幂等创建直接子任务，然后进入 `WAITING_CHILDREN` 并释放 lease。最后一个满足 join 条件的子任务在同一事务中将父任务重新置为 `QUEUED`。首版不建设任意 DAG；需要前后依赖时，由父任务在下一次 claim 后根据已提交 Artifact 创建下一层子任务。

### 6.6 Lease、Heartbeat 与 fencing

每次领取任务都原子增加 `lease_epoch`。Worker 后续 heartbeat、Checkpoint、Artifact 提交和终态提交必须携带：

```text
task_id + lease_owner + lease_epoch + 未过期 lease_until
```

即使旧 Worker 在 lease 过期后继续运行，其迟到结果也会被数据库拒绝。只比较 `lease_until` 不足以阻止已经恢复执行的旧进程提交。

### 6.7 Fan-out / Fan-in

Coordinator 必须在一次事务中使用确定性 `child_key` 持久化当前层的全部子任务和 join policy：

```text
ALL_SUCCESS：所有子任务成功；最终失败时父任务失败或明确请求用户决定
ALL_DONE：所有子任务进入终态；父任务可以合并成功结果并说明缺口
```

首版只实现这两种策略，不实现 QUORUM、任意 DAG 或工作流 DSL。`ALL_DONE` 用于显式 best-effort 场景。合并顺序按 `(priority, child_key, task_id)` 固定，不能依赖完成先后。Critic 读取“合并候选 + rubric + 来源 Artifact”，不读取其他 Agent 的隐藏推理。

### 6.8 失败、重试与取消

- Retryable failure：在 attempts 和预算范围内重新 QUEUED；
- Non-retryable failure：直接 FAILED，并写安全错误码；
- `ALL_SUCCESS` 中任一子任务最终失败：父任务失败或明确请求用户决定；
- `ALL_DONE` 中存在失败：允许合并成功结果，但最终回复必须说明缺口；
- 取消父任务：事务内将整个后代树置为 `CANCELLED`，递增 lease epoch、清除 lease，并写入 `cancel_requested_at/cancel_reason`；运行中 Worker 通过轮询取消事件尽快停止；
- 迟到结果：不得创建 Artifact，只追加 `agent.task.late_result_rejected`，事件仅保留 attempt、lease epoch、Schema 和内容 Hash 等安全元数据；
- 已完成 Artifact：取消时不删除，可作为用户可见的部分成果。

### 6.9 权威写入边界

子 Agent 只能：

- 读取固定快照；
- 调用被授权的 READ 或纯计算工具；
- 写自己的 Checkpoint、事件和 Artifact；
- 提出工具/计划/记忆/改进候选。

Coordinator Commit Gate 可以生成最终回复和候选，但不能代替用户作出产品决策：

- 生成用户可见的最终回复；
- 创建 Plan/Goal/Adjustment/Memory/Evolution Proposal；
- 发起 WRITE 或版本晋升审批请求；
- 在收到真实用户决策后，调用现有领域 Service，并携带 owner、expected version、Idempotency-Key 和 command receipt 提交正式变更。

Coordinator 不得以 `actor=model/coordinator` 自动接受 Memory Proposal、修改正式 Program、消费 WRITE 审批或启用 Skill/Policy/Prompt 新版本。

WRITE 工具首版保持全局顺序执行。并行专家不得直接产生外部副作用。

### 6.10 预算

预算按三级管理：

```text
Run 总预算
  -> Agent Task 预算
       -> Model/Tool Call 预算
```

创建任务或发起调用前，必须在 SQLite 事务中原子预留最坏预算；调用完成后以真实使用结算并归还余额。重试、Critic、Judge、失败调用和取消前已经发生的消耗全部计费。达到任一硬上限后停止整棵任务树，不允许子 Agent 自行追加预算。

## 7. 多 Agent 数据模型

### 7.1 `agent_runs`

```text
id                      TEXT PRIMARY KEY
owner_id                TEXT NOT NULL
thread_id               TEXT
source_run_id           TEXT
coordinator_task_id     TEXT
mode                    TEXT CHECK(single|expert)
status                  TEXT
context_snapshot_id     TEXT NOT NULL
runtime_bundle_id       TEXT NOT NULL
join_policy_json        TEXT NOT NULL
budget_json             TEXT NOT NULL
reserved_budget_json    TEXT NOT NULL
version                 INTEGER NOT NULL DEFAULT 0
cancel_requested_at     TEXT
created_at / updated_at / finished_at
```

### 7.2 `agent_tasks`

```text
id                      TEXT PRIMARY KEY
agent_run_id            TEXT NOT NULL
parent_task_id          TEXT
root_task_id            TEXT NOT NULL
child_key               TEXT
role                    TEXT NOT NULL
objective               TEXT NOT NULL
context_snapshot_id     TEXT NOT NULL
output_schema           TEXT NOT NULL
status                  TEXT NOT NULL
required                INTEGER NOT NULL
priority                INTEGER NOT NULL
join_policy             TEXT CHECK(join_policy IN ('ALL_SUCCESS','ALL_DONE'))
children_closed_at      TEXT
attempts / max_attempts INTEGER NOT NULL
lease_owner             TEXT
lease_epoch             INTEGER NOT NULL DEFAULT 0
lease_until             TEXT
available_at            TEXT NOT NULL
budget_json             TEXT NOT NULL
reserved_budget_json    TEXT NOT NULL
result_artifact_id      TEXT
error_code              TEXT
cancel_requested_at     TEXT
cancel_reason           TEXT
version                 INTEGER NOT NULL DEFAULT 0
created_at / updated_at / finished_at
```

### 7.3 `agent_task_attempts`

```text
id                      TEXT PRIMARY KEY
task_id                 TEXT NOT NULL
attempt_no              INTEGER NOT NULL
lease_owner             TEXT NOT NULL
lease_epoch             INTEGER NOT NULL
status                  TEXT CHECK(RUNNING|SUCCEEDED|FAILED|LEASE_LOST|CANCELLED)
started_at / heartbeat_at / finished_at
error_json              TEXT
UNIQUE(task_id, attempt_no)
```

`agent_tasks` 约束 `UNIQUE(parent_task_id, child_key)`。父任务必须在同一事务创建全部直接子任务、设置 `children_closed_at`，然后进入 `WAITING_CHILDREN`。只有 `children_closed_at` 非空且直接子任务满足 `join_policy` 时才能唤醒或终结父任务。父任务只能创建深度和数量均在硬上限内的直接子任务，因此首版不需要通用依赖表和环检测器。

### 7.4 `agent_context_snapshots`

```text
id                      TEXT PRIMARY KEY
owner_id                TEXT NOT NULL
scope_json              TEXT NOT NULL
plan_version_id         TEXT
program_version_id      TEXT
memory_revision_ids_json TEXT NOT NULL
episode_ids_json        TEXT NOT NULL
skill_digests_json      TEXT NOT NULL
runtime_bundle_id       TEXT NOT NULL
content_json            TEXT NOT NULL
content_hash            TEXT NOT NULL UNIQUE
created_at              TEXT NOT NULL
```

### 7.5 `agent_artifacts`

```text
id                      TEXT PRIMARY KEY
task_id                 TEXT NOT NULL
attempt                 INTEGER NOT NULL
lease_epoch             INTEGER NOT NULL
artifact_type           TEXT NOT NULL
schema_version          INTEGER NOT NULL
content_json            TEXT NOT NULL
source_refs_json        TEXT NOT NULL
content_hash            TEXT NOT NULL
redaction_status        TEXT NOT NULL
created_at              TEXT NOT NULL
UNIQUE(task_id, attempt, artifact_type)
```

### 7.6 `agent_events`

可以复用现有 Thread event 作为用户流，但任务域还需要自己的 append-only 审计流：

```text
row_id                  INTEGER PRIMARY KEY AUTOINCREMENT
event_id                TEXT UNIQUE NOT NULL
agent_run_id            TEXT NOT NULL
seq                     INTEGER NOT NULL
task_id                 TEXT
type / actor / data_json / occurred_at
UNIQUE(agent_run_id, seq)
```

状态变更、预算结算、Artifact 指针和事件必须在同一事务提交。

### 7.7 `tool_execution_claims`

现有 `tool_calls` 的“先查询、再执行、后写结果”不足以支持多执行者竞争。Phase 0 增加持久 claim：

```text
logical_action_key      TEXT PRIMARY KEY
run_id / task_id / agent_id / tool_call_id
params_hash / target_hash / skill_digest / policy_version
status                  TEXT CHECK(PREPARED|RUNNING|COMPLETED|RECONCILIATION_REQUIRED|FAILED)
lease_owner / lease_epoch / lease_until
result_json / external_idempotency_key
created_at / updated_at / completed_at
```

外部调用前必须原子 claim。若工具支持幂等键则透传；若外部调用可能成功但内部未确认，状态进入 `RECONCILIATION_REQUIRED`，禁止自动重放。

## 8. 受控自进化设计

### 8.1 自进化的定义

成熟自进化不是 Agent 可以随意改写自己，而是：

> 系统能够从真实证据中提出改进，用独立评测验证改进，并在受控发布体系中采用或回滚改进。

完整循环：

```text
Observe -> Reflect -> Learn -> Propose -> Evaluate
        -> Approve -> Canary -> Promote / Rollback
```

Reflection 只解释一次任务；Learning 要从多条证据中提炼规律；Evolution 产生不可变候选版本；Promotion 才改变后续任务使用的 active bundle。

### 8.2 五层进化权限

| 层级 | 进化对象 | 自动化边界 | 发布权限 |
|---|---|---|---|
| Memory | 用户偏好、约束、稳定事实、经验 | 明确“记住”可直接确认；行为推断只能提案 | 用户可查看、编辑、停用、删除 |
| Skill | 某类任务的步骤、模板、工具范围 | 可自动生成候选和测试，不可静默覆盖 | 必须展示 diff、评测和权限变化 |
| Policy | 何时 Ask、研究、专家协同、结束循环 | 可生成候选；安全和状态机规则不可修改 | 人工审批后灰度 |
| Prompt | 任务模板和认知步骤提示词 | 可优化非核心模板；核心 System/Safety Prompt 冻结 | 评测、审批、灰度后启用 |
| Code | Runtime、工具、调度和 UI | 只允许隔离 worktree 生成 patch/PR | 测试、代码审查和人工合并 |

核心安全规则、权限计算、审批语义、预算上限、评测门禁和发布规则永远不是自进化对象。

### 8.3 Experience Record

每次任务终态后，Observer 从已提交事件和用户反馈投影结构化经验：

```json
{
  "task_type": "long_term_plan",
  "outcome": "completed_with_revision",
  "signals": {
    "ask_count": 3,
    "user_corrections": 2,
    "cancelled": false,
    "ttft_seconds": 1.2,
    "total_tokens": 8200
  },
  "failure_tags": ["unnecessary_clarification"],
  "evidence_refs": ["event_12", "message_8"],
  "bundle_id": "bundle_4"
}
```

Experience 不是长期用户记忆；它属于系统质量数据，必须 owner 隔离、默认脱敏，并遵守保留和删除策略。

### 8.4 候选生成条件

不能根据一次失败立即改全局行为。默认满足以下条件之一才生成候选：

- 同一 failure tag 在独立任务中至少出现 3 次；
- 用户明确重复纠正同一行为；
- 确定性安全测试发现系统性缺陷；
- 某 Skill 在足够样本上显著低于稳定基线；
- 开发者手动发起候选。

候选生成器必须输出：基线版本、变更 diff、适用范围、证据、假设收益、潜在回归、所需评测套件。候选只能处于不可执行存储区。

### 8.5 候选状态机

```text
DRAFT -> READY_FOR_EVAL -> EVALUATING -> EVALUATED
   |                           |             |
   +-> REJECTED                +-> FAILED    +-> PENDING_APPROVAL
                                                |          |
                                                |          +-> REJECTED
                                                v
                                             APPROVED -> CANARY
                                             |    |
                                             |    +-> ROLLED_BACK
                                             v
                                           PROMOTED
                                             |
                                             +-> ROLLED_BACK
```

任何状态跃迁都要携带 expected version、actor 和证据 Hash。`approve/start-canary/promote/rollback` 分别只接受 `PENDING_APPROVAL/APPROVED/CANARY/PROMOTED|CANARY`。FAILED、REJECTED、ROLLED_BACK 不删除记录。候选进入 `READY_FOR_EVAL` 后冻结；任何内容修改都创建新的子候选版本，并使旧审批失效。

### 8.6 Runtime Bundle

一次模型调用和任务必须固定不可变版本包：

```json
{
  "bundle_id": "runtime_bundle_9",
  "code_commit": "...",
  "code_artifact_hash": "sha256:...",
  "model_profile": "agent-model@3",
  "provider_and_decoding_hash": "sha256:...",
  "skill_versions": ["goal-planning@4"],
  "policy_versions": ["interaction-policy@5"],
  "prompt_versions": ["daily-review@3"],
  "tool_schema_digest": "sha256:...",
  "memory_revision_ids": ["memory_revision_12"],
  "episode_ids": ["episode_4"],
  "context_renderer_version": "context@2",
  "tokenizer_version": "tokenizer@1",
  "config_hash": "sha256:..."
}
```

晋升只原子切换新任务使用的 `active_bundle_id`。每次模型调用和 Checkpoint 都绑定 bundle。运行中任务继续使用原 bundle；不得静默漂移。回滚由稳定代码切回上一 bundle，不依赖候选代码能够正常工作。

### 8.7 评测金字塔

候选评测按以下顺序执行，前一层失败即停止：

1. Schema、静态安全和权限 diff；
2. 状态机、审批、幂等、取消、预算、脱敏等确定性测试；
3. 历史轨迹 replay；
4. 独立 holdout 场景；
5. 候选与 baseline 的成对对照；
6. LLM Judge 软质量评分；
7. 小流量 canary 在线指标。

LLM Judge 只能评价清晰度、帮助程度、方案质量等软指标，不能覆盖任何确定性失败。

### 8.8 评测隔离与证明

- 候选生成器与 evaluator 使用不同进程、工作目录和数据库凭据；生成器没有 holdout、Judge Prompt、结果目录或任意网络读取权限；
- evaluator 只读候选和密封数据集，只能写专用结果通道，被测代码不能写评测结果；
- dev/replay/holdout/canary 数据严格分组；
- 同一用户或同一目标派生案例必须落在同一分区，避免泄漏；
- 每份报告绑定 code commit、candidate/base digest、bundle、model、eval set、evaluator、随机种子和轨迹 Hash；
- 评测结果及门禁为 append-only，候选不得修改评测器或门槛。

### 8.9 晋升门禁

通用门禁：

```text
所有 P0 安全测试通过
任务成功率不得下降超过预设容差
目标指标达到最小改善幅度
TTFT、总延迟、Token 和工具成本不越过硬阈值
没有新增未审批权限
证据清单完整
```

这些条件由版本化 `GatePolicy` 表达，不允许留作运行时自由判断。每项指标必须声明：`metric_name`、`min_n`、观察窗口、CI95 方法与下界、允许退化、最低改善、关键任务/风险/语言切片和缺样本行为。样本不足时保持 `CANARY`，禁止自动晋升。首版阈值在 writing-plans 阶段根据冻结 eval 基线确定并作为版本化配置提交；任何 GatePolicy 变更视为高风险 Policy 候选。

各候选还需领域指标。例如 Ask Policy：必要询问准确率、平均回答轮数、直接生成指令遵循率和任务采用率；Goal Skill：行动完成率、延期率、用户修改次数；Research Prompt：主题覆盖、有效引用和未知引用率。

首版所有 Skill、Policy、Prompt 和 Code 候选必须人工批准。只有用户明确记忆和后续被证明低风险的 owner-scoped 偏好可以自动生效。

### 8.10 Canary 与回滚

- canary 只影响新任务，不改变在途任务；
- 初始只对本地用户显式选择的任务生效；
- 分流采用稳定 Hash，避免同一目标在版本间跳动；
- 每次部署固定 allocation、HMAC salt digest、assignment unit、Champion/Challenger bundle、窗口和停止条件；每个任务在执行前写入 cohort 和实际 bundle 曝光记录；
- 监控成功率、取消率、返工、TTFT、成本、安全失败和用户撤销；
- 达到最小样本前不得宣称提升；
- 任一安全回归立即自动回滚；
- 质量或成本越界触发暂停并回到上一 bundle；
- 回滚后保留候选、评测、canary 和决策审计。
- Canary 禁止使用破坏性 migration；必须保证 N/N-1 运行数据兼容，并在晋升前完成旧 bundle 恢复演练。指针回滚不能撤销已经发生的外部副作用，相关动作进入 reconciliation。

## 9. 自进化数据模型

### 9.1 `experience_records`

```text
id / owner_id / project_id / source_run_id
task_type / outcome / signals_json / failure_tags_json
evidence_refs_json / runtime_bundle_id
origin / cohort / generation_cycle_id / eligible_after
dataset_partition / lineage_group_hash / source_content_hash
split_policy_version / candidate_eligibility
redaction_status / created_at / expires_at
```

同一 owner、目标及其派生样本使用稳定 `lineage_group_hash` 原子分配到 `DISCOVERY/DEV/HOLDOUT/SAFETY`，禁止跨分区复制。Eval、Judge、Shadow 和同一候选的 Canary 轨迹不能用于修改当前候选；只有当前周期结束、数据冻结并审计后，才能进入下一周期的 DISCOVERY。

### 9.2 `evolution_candidates`

```text
id / candidate_type(memory|skill|policy|prompt|code)
scope_type / scope_id / base_version_id
proposed_content_ref / proposed_digest
reason / evidence_refs_json / expected_benefit_json / risk_json
status / version / created_by / created_at / updated_at
```

### 9.3 `evolution_evaluations`

```text
id / candidate_id / baseline_bundle_id / candidate_bundle_id
eval_suite_id / eval_set_digest / evaluator_digest
status / deterministic_pass / metrics_json / regressions_json
cost_json / trace_hash / created_at / finished_at
```

证明字段不得只藏在自由 JSON 中，至少单列 `code_artifact_hash`、`candidate_digest`、`base_digest`、`bundle_digest`、`model_profile_hash`、`eval_set_digest`、`evaluator_digest`、`tool_schema_digest`、`isolation_policy_version`、`interpreter_or_image_digest`、`seed` 和 `trace_hash`。

### 9.4 `runtime_bundles` 与 `runtime_bundle_items`

保存不可变 bundle 及其 Memory pins、Skill、Policy、Prompt、模型/provider/decoding、工具 Schema、Context renderer/tokenizer、代码制品和配置版本。`runtime_channels` 只保存 `stable/canary -> bundle_id` 原子指针。

### 9.5 `evolution_decisions`

```text
id / candidate_id / decision(approve|reject|promote|rollback)
from_bundle_id / to_bundle_id / actor / reason
candidate_digest / evaluation_report_digest / permission_diff_digest
target_bundle_digest / evidence_hash / expires_at
idempotency_key / created_at
```

### 9.6 `canary_deployments` 与 `canary_exposures`

```text
canary_deployments:
id / candidate_id / champion_bundle_id / challenger_bundle_id
allocation_percent / assignment_unit / salt_digest
gate_policy_version / window_started_at / window_ends_at
status / stop_reason / created_at / updated_at

canary_exposures:
deployment_id / run_id / assignment_hash / cohort / bundle_id / exposed_at
PRIMARY KEY(deployment_id, run_id)
```

指标只能统计已记录实际曝光的任务；不得根据事后结果改变 cohort。

## 10. 安全与可靠性不变量

1. 子 Agent 有效能力必须等于 `父能力 ∩ 全局策略 ∩ Skill allowlist ∩ 当前任务范围`；
2. 用户、网页、工具结果、Artifact 和 Skill 文本均为不可信数据，不能进入安全策略通道；
3. 状态变更、预算、Checkpoint、Artifact 指针和事件必须原子提交；
4. 所有异步提交都必须通过 `expected_version + lease_epoch` fencing；
5. 同一逻辑工具动作跨重试保持相同 idempotency key；
6. 外部系统不支持幂等且结果不确定时进入 `RECONCILIATION_REQUIRED`，禁止盲重放；
7. WRITE 审批绑定 run/task/agent/tool/规范化参数 Hash/目标/Skill digest/Policy version/expiry；任一变化必须重新审批；
8. Skill 只能缩小权限，不能注册工具、读取凭据、关闭脱敏或调用晋升接口；
9. 候选生成者、评测者和发布者权限分离；
10. 默认导出对消息、Artifact、Checkpoint、模型请求、错误栈和评测结果统一 fail-closed 脱敏；
11. 用户取消父任务后，所有后代 lease 失效，迟到结果不得提交；
12. 预算、深度、任务总数、attempt、Token、工具次数和 wall time 都有硬上限。

## 11. 事件与 SSE

新增事件建议：

```text
agent.run.created
agent.mode.selected
agent.task.created / queued / started
agent.task.completed / failed / retrying / cancelled
agent.artifact.committed
agent.join.ready / partial / failed
agent.merge.started / completed
agent.critic.completed
agent.run.completed / cancelled

evolution.experience.recorded
evolution.candidate.created
evolution.evaluation.started / completed / failed
evolution.approval.requested / decided
evolution.canary.started / stopped
evolution.promoted / rolled_back
```

SSE 只发送已提交事件，支持 `Last-Event-ID`。客户端如果收到 `seq > cursor + 1`，必须暂停投影并补拉缺口，不能直接跨过缺失事件。

默认 UI 不显示原始 JSON 或思维过程，而显示：

- 正在做什么；
- 为什么调用专家；
- 哪些专家完成了什么；
- 有什么证据和未决问题；
- 主 Agent 最终采用/舍弃了什么；
- 当前是否需要用户决定。

技术事件作为二级入口保留。

## 12. 用户体验

### 12.1 用户心智

用户看到的是一个 Better Agent，而不是角色聊天室。入口命名为“深入处理”，不使用“启动 4 个 Agent”作为主要文案。

建议卡示例：

> 这个目标涉及训练基础、时间安排和风险控制。我可以让三个专家并行检查，预计增加少量等待和模型消耗。
>
> `深入处理`　`按普通方式继续`

### 12.2 专家进度卡

每个专家卡只展示：任务、方法类别、状态、结论、证据数、不确定项。禁止展示原始 chain-of-thought、内部 Prompt 和完整上下文。

### 12.3 统一“需要你决定”

计划批准、WRITE 工具、记忆提案、Skill/Policy/Prompt 候选进入同一决策中心。审批卡必须展示具体动作、目标对象、影响范围、参数差异、权限变化、风险和撤销方式，而不是只显示 ID。

### 12.4 成长页面

增加“成长”页面，包含：

- 待确认：记忆和能力改进候选；
- 记忆：内容、范围、来源、应用记录、编辑/停用/删除；
- 能力：Skill/Policy/Prompt 版本、diff、评测、当前状态和回滚；
- 历史：被拒绝、晋升和回滚记录。

用户无需输入版本号即可比较和恢复旧版本。

## 13. API 草案

### 13.1 多 Agent

```text
POST   /api/threads/{thread_id}/expert-runs
GET    /api/agent-runs/{run_id}
GET    /api/agent-runs/{run_id}/tasks
GET    /api/agent-runs/{run_id}/artifacts
POST   /api/agent-runs/{run_id}/cancel
POST   /api/agent-runs/{run_id}/continue-partial
GET    /api/agent-runs/{run_id}/events?after_seq=
GET    /api/agent-runs/{run_id}/events/stream
```

`events/stream` 支持 `Last-Event-ID`。若 Chat 继续订阅 Thread SSE，则用户可见 Agent 事件投影到 `thread_events`，且非终态 `agent_runs` 必须纳入现有 Thread Stream 的活动判断，防止 Turn 结束后提前关闭连接。Worker claim/heartbeat/commit 为进程内 Service API，首版不开放公共 HTTP 管理端点。

### 13.2 自进化

```text
GET    /api/evolution/candidates
GET    /api/evolution/candidates/{id}
POST   /api/evolution/candidates/{id}/evaluate
POST   /api/evolution/candidates/{id}/approve
POST   /api/evolution/candidates/{id}/reject
POST   /api/evolution/candidates/{id}/start-canary
POST   /api/evolution/candidates/{id}/promote
POST   /api/evolution/candidates/{id}/rollback
GET    /api/evolution/bundles
GET    /api/evolution/history
```

所有 mutation 使用 owner scope、CSRF、expected version 和 Idempotency-Key。

## 14. 分阶段实施

### Phase 0：安全地基与契约

- 冻结 task/artifact/context/bundle Schema；
- 建立最小不可变 Runtime Bundle：code commit/artifact Hash、模型/provider/decoding Hash、Skill 文件 digest、现有 Prompt/协议 digest、工具 Schema、Context renderer/tokenizer 和 config Hash；
- 为异步提交增加 `lease_epoch` fencing；
- 修正 SSE gap 检测；
- 扩展审批绑定和 UI；
- 增加 durable tool execution claim 与 `RECONCILIATION_REQUIRED`；
- 建立树级预算预留、取消和错误码；
- 先写并发、迟到结果、重复副作用和脱敏失败测试。

完成标准：P0 不变量全绿；尚不启用多个专家。

### Phase 1：单进程只读专家协同

- 新增 `agent_runs/tasks/task_attempts/context_snapshots/artifacts/events`；
- 一个 ManagedAgentWorker 池从 SQLite 领取任务；
- 只开放 Researcher、Planner、Critic；
- 只允许 READ/纯计算工具；
- 实现 ALL_SUCCESS、ALL_DONE、固定顺序 merge；
- 对话内展示专家进度和取消。

完成标准：旅行、训练和技术调研三个场景可重启恢复、可取消、无重复提交。

### Phase 2：与 Goal 闭环集成

- 专家结果可形成 Plan/Adjustment/Review 候选；
- Coordinator 通过现有 Proposal/审批机制提交正式状态；
- Goal Daily Review 可以按证据选择领域专家；
- 专家失败时允许用户使用部分成果。

完成标准：专家只能提出候选，未审批不能改变正式计划和行动。

### Phase 3：经验库与手动自进化

- 新增 Experience Record 和失败标签；
- 支持手动创建 Skill/Policy/Prompt 候选；
- 在 Phase 0 最小 Runtime Bundle 上增加统一资产版本、候选、channel 和 promotion；
- 建立 deterministic + replay + holdout 对照报告；
- 成长页面展示 diff、证据和评测；
- 只允许人工批准和手动回滚。

完成标准：能够从一组失败轨迹生成候选，在不接触 holdout 的条件下评测并安全启用。

### Phase 4：自动发现候选与 Canary

- 自动聚合同类失败并建议候选；
- 建立稳定 canary 分流和在线指标；
- 安全回归自动回滚；
- 低风险 owner-scoped Memory 候选可按规则自动采用；
- Skill/Policy/Prompt 仍保留人工审批。

完成标准：候选改善有完整证明，回滚不影响在途任务。

### Phase 5：隔离代码改进实验

- 候选生成器只能在独立 worktree 生成 patch；
- 必须新增或修改相关测试；
- 运行全量测试、类型检查、构建和 eval；
- 输出 PR 和证明清单，由开发者审查合并；
- 不允许生产 Agent 直接部署。

该阶段不是前四阶段完成的必要条件。

## 15. 必须先写的失败测试

### 15.1 多 Agent P0

1. 两个 Worker 不能提交同一 task version；
2. lease 过期并重新分配后，旧 Worker 被 `lease_epoch` 拒绝；
3. 重复 fan-out 只能得到同一组 `child_key` 子任务，且深度/数量超过上限时必须拒绝；
4. Artifact Schema 无效时不能完成任务；
5. 相同 task/attempt 不能产生冲突 Artifact；
6. 父任务取消后，所有后代停止且迟到结果不能进入 join；
7. 剩余预算只够一次调用时，并发 N 个任务最多一个预留成功；
8. 子任务失败严格遵循 ALL_SUCCESS/ALL_DONE join policy；
9. 合并顺序不受完成顺序影响；
10. 子 Agent 权限严格小于等于父权限；
11. 子 Agent 不能使用父审批票据或 WRITE 工具；
12. SSE 缺 seq 时必须补拉，不能跳过。

### 15.2 工具与恢复 P0

1. 同一 `tool_call_id` 被两个执行者消费时最多产生一次副作用；
2. 参数、目标、Agent、Skill digest 或 Policy version 变化后旧审批失效；
3. 外部成功、内部提交前崩溃时不得盲目重放；
4. Checkpoint 在新 bundle 晋升后仍固定旧 bundle；
5. Skill 文件 digest 变化后旧任务和审批不能继续；
6. 取消、重试和 spawn 循环受全局硬上限约束。

### 15.3 自进化 P0

1. 单条经验不能自动生成全局生效版本；
2. 候选不能读取或修改 holdout、Judge Prompt 和评测结果；
3. 缺少任一 attestation 字段不能晋升；
4. 确定性安全失败不能被 LLM Judge 覆盖；
5. 候选不能扩大工具权限或修改晋升门槛；
6. canary 只影响新任务，不改变在途 bundle；
7. 晋升过程中任意崩溃后 active bundle 只能完整指向旧版或新版；
8. 指标越界或安全失败会自动回滚；
9. Canary migration 必须保持 N/N-1 双向运行兼容；旧 bundle 恢复演练失败时禁止晋升；
10. 默认导出必须从 Agent 消息、Artifact、Checkpoint、错误和 eval 中脱敏，脱敏失败时禁止导出。
11. 候选审批绑定候选、评测、权限 diff 和目标 bundle digest；任一变化或过期后审批失效；
12. 同一周期的 Eval/Shadow/Canary 数据不能回流修改当前候选；
13. 样本不足或关键切片缺失时 GatePolicy 必须阻止晋升；
14. Canary 指标只能使用实际 exposure 记录，cohort 分配稳定且不可事后更改。

## 16. 验收场景

### 场景 A：长期骑行训练计划

1. 用户提出长期训练目标；
2. Better 判断需要训练规划和安全检查，建议深入处理；
3. Researcher 核验通用训练原则，Planner 生成安排，Critic 检查递增负荷和约束；
4. 用户看到各专家可读进展但不看到内部推理；
5. Coordinator 合并并生成 PlanDocument 候选；
6. 用户确认后保存和开始执行；
7. 后续完成/跳过反馈进入 Daily Review；
8. 多次证据显示安排过密时生成 Skill/Policy 改进候选；
9. 评测通过并由用户批准后，只对新计划启用。

### 场景 B：七天内蒙古旅行研究

1. Researcher 分别核验交通、天气/季节、景点和预算；
2. 某个专家失败，系统按 ALL_DONE 返回其他成果并说明缺口；
3. 服务重启后未完成任务通过 lease 恢复，不重复已完成 Artifact；
4. 最终报告主题覆盖和引用通过确定性审计；
5. 用户取消时停止整个后代树并保留已完成成果。

### 场景 C：自进化 Ask 策略

1. 系统累计多条“用户要求直接生成但仍被追问”的经验；
2. 生成 `interaction-policy` 候选，显示前后 diff；
3. 在历史 replay 和 holdout 上比较 Ask 准确率、轮数、TTFT、成功率和成本；
4. 安全测试全部通过后请求用户/开发者批准；
5. canary 仅用于选定新任务；
6. 指标达标后晋升，否则回滚；
7. 每个历史 Run 仍可追溯原 Policy/Prompt/Skill 版本。

## 17. 指标

### 17.1 多 Agent

- 复杂任务端到端耗时和首个有效成果时间；
- task 成功/重试/lease lost/取消率；
- Artifact Schema 失败率；
- required/optional 缺失率；
- 并行效率与单 Agent 基线差值；
- Token、工具次数和费用；
- 重复副作用计数，目标必须为 0；
- 用户采用率、返工次数和取消率。

### 17.2 自进化

- 候选产生、评测、批准、晋升和回滚数量；
- 候选相对 baseline 的成功率、质量、延迟和成本变化；
- holdout 回归数量；
- canary 用户撤销/纠正率；
- 版本生效后的长期目标完成率变化；
- 错误归因完整率；
- 自动回滚时间；
- 未经审批生效次数，目标必须为 0。

## 18. 文件规划

建议在确认规格后通过 writing-plans 进一步拆分，预计新增：

```text
backend/app/agents/models.py
backend/app/agents/service.py
backend/app/agents/worker.py
backend/app/agents/coordinator.py
backend/app/agents/artifacts.py
backend/app/agents/policy.py

backend/app/evolution/models.py
backend/app/evolution/observer.py
backend/app/evolution/candidates.py
backend/app/evolution/evaluator.py
backend/app/evolution/bundles.py
backend/app/evolution/promotion.py

frontend/src/components/ExpertRunCard.tsx
frontend/src/components/ExpertTaskCard.tsx
frontend/src/components/DecisionCard.tsx
frontend/src/components/EvolutionCandidateCard.tsx
frontend/src/pages/GrowthPage.tsx
```

修改范围主要包括 `db.py` migration、`startup.py` managed workers、`api.py`、事件/SSE、工具审批、Context/Skill pin、Chat/Trajectory/Memory 导航和测试。实际实施应优先提取现有 Worker 公共的极少量 lease helper，避免复制三套逻辑，也避免提前建设通用调度框架。

## 19. 实施决策摘要

1. 多 Agent 采用“任务消息 + 不可变 Artifact + 版本化快照”，不采用共享可变状态；
2. SQLite 是事实来源，队列只负责唤醒；首版直接轮询 SQLite；
3. Coordinator 是唯一正式提交者；子 Agent 只提交候选 Artifact；
4. READ/纯计算可并行，WRITE 首版全局顺序执行并继续审批；
5. `lease_epoch`、CAS、预算预留和树级取消是上线前提；
6. 用户只面对一个 Better Agent，专家过程显示可读工作证明，不显示原始思维；
7. 自进化首先覆盖 Memory、Skill 和交互 Policy，再覆盖任务 Prompt，代码改进最后实施；
8. 核心 System Prompt、安全策略、权限和发布规则冻结，不允许自进化；
9. 所有改进都必须形成不可变候选，经隔离评测、审批、canary 后晋升；
10. 每个任务固定 runtime bundle，发布和回滚不改变在途运行；
11. 确定性安全测试拥有否决权，LLM Judge 只评软质量；
12. 不引入 Redis、Celery、微服务和通用 workflow，直到 SQLite claim 出现可测量瓶颈。

## 20. 完成定义

只有同时满足以下条件，才能称为完成“多 Agent 协同与受控自进化”：

- 专家任务在重启、并发、lease 过期和取消下不重复提交、不丢失权威状态；
- 用户能看见真实进度、部分成果、专家结论和需要自己决定的事项；
- 子 Agent 无法越权、无法写正式状态、无法绕过审批；
- 多 Agent 相对单 Agent 在预设复杂任务集上产生可测量收益；
- 真实经验可以形成版本化候选，并有完整来源证据；
- 候选在隔离环境完成确定性、replay、holdout 和对照评测；
- 发布具有审批、canary、指标门禁、原子晋升和自动回滚；
- 任一历史任务可以追溯 code/model/Skill/Policy/Prompt/config bundle；
- 核心安全边界不能由 Agent 修改；
- 测试、类型检查、前端构建和端到端场景全部通过。

达到以上定义后，Better Agent 才能准确描述为：

> 一个以中心化安全提交为核心、支持持久异步专家协作，并通过真实轨迹、隔离评测、受控发布和自动回滚持续改进的个人成长 Agent。
