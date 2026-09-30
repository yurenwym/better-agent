# HarnessExecutionContext 接入说明（第一阶段）

- 面向：在本仓库新增或修改「会产生模型调用 / 工具调用」的代码路径的人。
- 需求与验收基线：`docs/HarnessExecutionContext-第一阶段开发任务与验收-2026-09-30.md`
- 验收证据：`docs/acceptance/harness-context-phase1/`

## 1. 一句话模型

一次对话 Turn 有一个**持久化的 trace 根**；它下面的每一次**逻辑**模型调用和每一次工具调用各自派生一个
**新 span**（业务身份原样继承）。身份只能由服务端可信入口铸造，写进数据库后**不可被覆盖**。

```
turn 根（turns.execution_context_*）
├── span: LLM route_and_respond      → model_invocations.execution_context_*
├── span: LLM route_and_respond      → 同上（第二次逻辑调用 = 新 span）
├── span: tool modify_plan_document  → turn_tool_calls.execution_context_*
│   └── span: LLM compile_goal_program → model_invocations.execution_context_*
└── span: LLM route_and_respond      → 工具结果回到对话后的下一次调用
```

## 2. API 速查

`app/execution_context.py`

| 名称 | 语义 |
|---|---|
| `create_root_context(*, owner_id, thread_id=None, turn_id=None, run_id=None, task_id=None, project_id=None, root_budget_id=None, runtime_bundle_id=None)` | 铸造 trace。**只在可信入口调用**，keyword-only，无 `**overrides` |
| `create_child_context(parent)` | 派生 span。业务身份与 `trace_id` 原样继承，只换 `span_id` 并把 `parent_span_id` 指向父 |
| `context_envelope(ctx)` / `canonical_json(envelope)` | 版本化 envelope（`schema_version: harness-execution-context-v1`）与规范化 JSON |
| `serialize_context(ctx)` / `deserialize_context(payload)` | 存取。反序列化会校验 schema 与不变量 |
| `execution_context_digest(payload)` | `sha256(canonical_json)`；**先校验 schema**，未知版本直接抛 `UnknownSchemaVersion` |
| `validate_context(ctx)` / `check_context_alignment(obj)` / `rebind(obj, **updates)` | 字段校验、双份字段严格一致性校验、仅更新非身份元数据；携带 harness 时禁止重绑公共身份，`None` 也必须一致。无 harness 的旧入口保持原行为 |

适配（不产生 span、不接受身份覆盖参数）

| 名称 | 位置 |
|---|---|
| `ModelCallContext.from_harness(harness, *, role, purpose, invocation_id=None, idempotency_key=None, ...)` | `app/model_control.py` |
| `ToolExecutionContext.from_harness(harness, *, tool_call_id)` | `app/tools.py` |

持久化 `app/harness_context_store.py`

| 方法 | 说明 |
|---|---|
| `load_or_create_turn_context(turn_id, *, owner_id, thread_id, project_id=None, run_id=None, root_budget_id=None, runtime_bundle_id=None)` | turn 根的**唯一**创建入口；已存在则读回，不重新铸造 trace。`run_id` 缺省为 `chat-turn:<turn_id>` |
| `save_turn_context(connection, turn_id, ctx)` / `load_turn_context(turn_id)` | 必须传入当前事务的 `connection` |
| `save_tool_call_context(connection, call_id, ctx)` / `load_tool_call_context(call_id)` | 同上 |
| `save_invocation_context(connection, invocation_id, ctx)` / `load_invocation_context(invocation_id)` | 同上 |

异常都带 `code`：

| 异常 | code | 含义 |
|---|---|---|
| `HarnessContextError` | `HARNESS_CONTEXT_INVALID` | 字段非法、类型不对、未知表 |
| `UnknownSchemaVersion` | `UNKNOWN_SCHEMA_VERSION` | envelope 的 `schema_version` 不认识 |
| `ContextStoreConflict`（继承 `ContextIdentityConflict`） | `CONTEXT_IDENTITY_CONFLICT` | 与行/关联绑定冲突、半写、digest 不符、试图覆盖 |
| `LegacyContextMissing` | `LEGACY_CONTEXT_MISSING` | 两列皆空（本特性之前的记录） |

## 3. 持久化契约

三张表各加两列（历史记录允许为 NULL）：

| 表 | 列 |
|---|---|
| `turns` | `execution_context_json` / `execution_context_digest` |
| `turn_tool_calls` | 同上 |
| `model_invocations` | 同上（另有 `root_budget_id`） |

绑定矩阵（写入与读取都检查，冲突即拒，**不自动修复、不覆盖数据库列**）：

| 表 | 必须一致 |
|---|---|
| `model_invocations` | `owner_id`、`run_id`、`thread_id`、`turn_id`、`runtime_bundle_id`、`root_budget_id` 分别等于同名列；`task_id` ↔ `agent_task_id` |
| `turns` | `turn_id` == 本行 `id`；`thread_id` == 本行 `thread_id`；`owner_id`/`project_id` 从所属 thread 校验；`runtime_bundle_id`/`root_budget_id` 与本行一致；`run_id` 非空时必须等于 `chat-turn:<id>` |
| `turn_tool_calls` | `turn_id`/`thread_id` 等于同名列；`run_id` == **原 turn** 的 `chat-turn:<turn_id>`；`owner_id`/`project_id` 由 thread 校验；`root_budget_id`/`runtime_bundle_id` 由**原 turn** 校验 |

三条硬规则：

1. **JSON 与 digest 同事务写入**，一空一非空视为损坏（`ContextStoreConflict`）。
2. **不可覆盖**：重复写入只有规范化内容完全相同时才幂等；不同 Context 一律拒绝（无最后写入者胜出）。
3. `execution_context_digest`（执行身份）**不是** `context_snapshot_digest`（模型输入），不得互相替代。

## 4. 接入一条新调用路径的检查清单

1. **根从哪来**：该路径是否属于某个 turn？是 → 读 `harness_context.load_or_create_turn_context(turn.id, ...)`，
   不要自己 `create_root_context`。不是（独立 Runtime）→ 本阶段**不要迁移**，保持原行为并登记到第 6 节。
2. **派生 span**：每次**逻辑**调用前 `create_child_context(parent)`。同一逻辑调用只派生一次，
   在 `context()` / `_request_approval()` / `execute()` / `resume()` 之间**复用同一个对象**。
3. **落库**：在**同一事务**里用 `save_*_context(connection, id, ctx)` 写身份。
   **先写行与身份，再发起外部动作或审批**——失败时不得留下可执行的孤立审批。
4. **传给下游**：用 `from_harness` 生成 `ModelCallContext` / `ToolExecutionContext`，
   不要用当前环境值（continuation turn 的 budget/bundle/turn 一律不算数）。
5. **ContextVar**：必须 `try/finally` reset，异常与取消都要覆盖。
6. **不要新建预算根**：工具内部编译计划时沿用工具 Context 的 `root_budget_id`，不得建 `goal_operation` 替代根。
7. **不要回落 stable**：内部调用的 `runtime_bundle_id` 用原绑定；原绑定不可用时按原有准入规则**停止**，不降级。

对照实现（可直接抄）：

| 场景 | 位置 |
|---|---|
| turn 根 + 每次逻辑 LLM 调用 | `app/conversation.py` `_process`（`load_or_create_turn_context` → `create_child_context` → `ModelCallContext.from_harness`） |
| 模型发出的工具调用 | `app/chat_tools.py` `ChatToolRunner.tool_harness()` / `execution_context()` |
| 直接执行的工具 | `app/chat_tools.py` `execute()`（先 `store.create(..., execution_context=harness)` 再执行） |
| 需审批的工具 | `app/chat_tools.py` `_request_approval()` |
| artifact 驱动的挂起 | `app/conversation.py` `_turn_tool_harness()` + `_suspend_artifact_modification()` |
| 工具内部 LLM | `app/goal_programs.py` `_model_context()` |
| 恢复 | `app/chat_tools.py` `ChatToolRunner.resume()` |

## 5. 反模式

- ❌ 手工拼 `HarnessExecutionContext(...)`（`HarnessExecutionContext(` 在 `app/execution_context.py` 之外应为零匹配）。
- ❌ 给 `from_harness` 传 `owner_id` / `root_budget_id` / `runtime_bundle_id` / `trace_id`（API 不接受）。
- ❌ 每经过一层 gateway 包装就建一个 span；gateway 只消费已创建的 Context。
- ❌ 用 `trace_id` 当幂等键，或用 `span_id` 当 `tool_call_id` / `invocation_id`。
- ❌ 在 `resume()` 里用 continuation turn 的预算、版本或身份补原记录。
- ❌ 把 `execution_context_digest` 当授权凭证（它只是审计/幂等用的一致性摘要）。
- ❌ 为了凑字段而人为创建 AgentTask，或把 `agent_runs` 的 ID 混进 `task_id`。

## 6. 审批恢复契约

顺序（`ChatToolRunner.resume()` 已实现，改这条链时不要改顺序）：

1. 有 `result` → 直接复用，不重跑。
2. `REJECTED` → 拒绝；非 `APPROVED` → `ACTION_NOT_ELIGIBLE`。
3. `store.load_execution_context(call.id)`；`LegacyContextMissing` → `LEGACY_CONTEXT_MISSING`，
   其他 `HarnessContextError` → 用它的 `code`。
4. owner / thread / turn 绑定校验 → `params_hash` 校验 → **当前** `allowed_names()` 复核
   （撤销的权限在这里被挡住，恢复 Context ≠ 恢复旧授权）。
5. `ToolExecutionContext.from_harness(harness, tool_call_id=call.id)`。
6. 记 `chat_tool.context_resumed` 事件（关联原 `call_id` / `trace_id` / `span_id` / `parent_span_id`），
   **不改原 JSON 与 digest**。
7. 进入既有幂等执行路径；`ToolReconciliationRequired` → 返回 `TOOL_RECONCILIATION_REQUIRED`，
   **不写 `record_result`**、不自动重试，chat 行保持 `APPROVED`。

**历史待审批记录**：两列皆 NULL 的旧待审批调用**默认保守拒绝**（`LEGACY_CONTEXT_MISSING`），
提示用户重新发起；不通过临时拼装 Context 兼容。旧完成记录仍可正常查询。

## 7. 明确未迁移（不要当成已覆盖）

- Research / Learning / Goal Review 等独立 Runtime。
- 独立发起的非工具计划编译入口（`goal_tools → preview` 之外的调用者）。
- 通用 Sub-Agent Tool、DAG trace、统一 Event Envelope、完整 `ContextSnapshot`、
  OpenTelemetry 接入与计时 span ↔ 逻辑 span 映射。

这些入口维持原行为；新增路径请勿顺手迁移，以免超出本阶段验证范围。

## 8. 本地验证

```bash
cd backend
python -m pytest tests/test_execution_context.py tests/test_harness_context_flow.py \
  tests/test_harness_context_approval.py -q
TEST_DATABASE_URL="postgresql://<user>:<pwd>@127.0.0.1:5432/<name>_test" \
  python -m pytest tests/integration/test_harness_context_postgres.py -q
```

`TEST_DATABASE_URL` 的库名必须以 `_test` / `_tests` / `_pgtest` 结尾且主机为回环地址，
否则 `tests/integration/db_target_guard.py` 会 fail-closed。**绝不能指向开发库**：
autouse 的 `isolated_postgres_test` 会在每个用例前后 `TRUNCATE` 所有应用表。
