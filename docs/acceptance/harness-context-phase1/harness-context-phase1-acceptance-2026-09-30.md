# HarnessExecutionContext 第一阶段验收报告

> 复审修订：原验收遗漏了首次写入竞争、学习策略导致版本漂移、双份身份字段校验不完整三个问题。现已修复，最新结果见 [修复与复测记录](harness-context-phase1-repair-2026-09-30.md)：267 项相关回归及 30 项隔离 PostgreSQL 测试通过。下文及原 evidence JSON 保留为初次验收记录，其中“全部门禁通过”仅代表当时用例，不应理解为覆盖了上述缺陷。

- 日期：2026-09-30
- 需求文档：`docs/HarnessExecutionContext-第一阶段开发任务与验收-2026-09-30.md`
- 代码基线 commit：`733866b69b93fae4d868541ad1a86d1e9e7ce3cb`
- 数据库 schema head：`POSTGRES_SCHEMA_HEAD = "20260930_0024"`（SQLite migration 46 与之等价）
- 解释器：`D:\pycharm\python.exe`

## 0. 结论

第一阶段最小链路已打通并通过全部自动化门禁：

`对话 Turn 的可信执行上下文 → LLM 调用 Context → Tool 调用 Context（可能等待审批）→ Tool 内部 LLM 调用 Context`

U01～U10、I01～I06、A01～A09、P01～P04 共 **36 个新用例全部通过**，且既有 Chat / Goal Tool / 模型路由 / 预算继承回归无新增失败。

**本阶段未宣称**：全项目统一 Context、完整 Trace 平台、全链路 ContextSnapshot、PolicyEngine / TaskRuntime / ToolRegistry 职责拆分、其他 Runtime 迁移。剩余范围见第 6 节。

## 1. 交付物

新增：

| 文件 | 职责 |
|---|---|
| `backend/app/execution_context.py` | 不可变公共 Context、root/child 工厂、版本化 envelope、规范化 digest、字段与继承校验 |
| `backend/app/harness_context_store.py` | turns / turn_tool_calls / model_invocations 三表的读写、绑定复核、不可覆盖约束 |
| `backend/alembic/versions/20260930_0024_harness_execution_context.py` | 增量迁移（`down_revision = 20260921_0023`，`downgrade()` 主动抛错） |
| `backend/tests/test_execution_context.py` | U01～U10（10 项） |
| `backend/tests/test_harness_context_flow.py` | I01～I06（6 项） |
| `backend/tests/test_harness_context_approval.py` | A01～A09（13 项） |
| `backend/tests/integration/test_harness_context_postgres.py` | P01～P04（7 项） |

修改：

| 文件 | 改动 |
|---|---|
| `backend/app/db.py` | SQLite migration 46 加 `execution_context_json` / `execution_context_digest` 到三张表，补 `model_invocations.root_budget_id`；`POSTGRES_SCHEMA_HEAD` 提到 `20260930_0024` |
| `backend/app/conversation.py` | turn 根 Context 的创建与持久化；每次逻辑 LLM 调用派生 span；`ContextVar` 以 `try/finally` reset；artifact 驱动的待审批调用（`_suspend_artifact_modification`）同样写身份，且先落行再申请审批 |
| `backend/app/live_model.py` | 逻辑 LLM 调用派生 child span 并 `ModelCallContext.from_harness` |
| `backend/app/chat_tools.py` | 工具 Context 在执行/申请审批前落库；`resume()` 从持久化记录恢复并重新校验当前授权 |
| `backend/app/tools.py` | `ToolExecutionContext.from_harness` 适配 |
| `backend/app/model_control.py` | `ModelCallContext.from_harness` 适配与旧字段一致性校验 |
| `backend/app/goal_programs.py` / `goal_tools.py` | 工具内部 LLM 传递 harness，不建 `goal_operation` 替代预算根、不回落 stable bundle |

## 2. 环境与证据基线

- 本机 `docker compose` 子命令不可用（`unknown command: docker compose`），因此集成测试通过仓库自带的逃生口 `TEST_DATABASE_URL` 指向隔离库：
  `postgresql://better_agent:***@127.0.0.1:5432/better_agent_v4_test`（库名以 `_test` 结尾，经 `tests/integration/db_target_guard.py` 校验；**未**指向开发库 `better_agent`）。
- `pytest --basetemp` 全部落在 `C:` 盘的新目录，符合仓库既有纪律（`D:` 小文件 I/O 慢 9.2 倍会让预算敏感用例成假失败）。

## 3. 用例对照表

### 3.1 U01～U10（`tests/test_execution_context.py`，10 passed）

| ID | 用例 | 结果 |
|---|---|---|
| U01 | `test_u01_two_roots_have_distinct_trace_and_span_and_no_parent` | PASS |
| U02 | `test_u02_child_and_grandchild_inherit_business_identity` | PASS |
| U03 | `test_u03_plain_conversation_has_no_task_and_creates_none` | PASS |
| U04 | `test_u04_root_task_is_the_root_and_children_keep_the_task_tree` | PASS |
| U05 | `test_u05_empty_owner_overrides_and_malformed_ids_are_rejected` | PASS |
| U06 | `test_u06_serialisation_round_trip_and_digest_sensitivity` | PASS |
| U07 | `test_u07_conversion_keeps_span_and_specialised_fields` | PASS |
| U08 | `test_u08_conversion_refuses_identity_overrides` | PASS |
| U09 | `test_u09_independent_calls_get_new_spans_and_retries_do_not` | PASS |
| U10 | `test_u10_gateway_wrappers_keep_the_public_identity` | PASS |

### 3.2 I01～I06（`tests/test_harness_context_flow.py`，6 passed）

驱动真实生产链路（`ManagedTurnWorker → LiveConversationModel → ChatToolRunner → ToolRegistry → GoalProgramService`），模型侧为固定响应的 routed gateway，断言全部从数据库读回，未使用伪造链路。

| ID | 用例 | 关键断言 | 结果 |
|---|---|---|---|
| I01 | `test_i01_read_tool_chain_shares_one_trace` | turn 根 span 无父无 task；LLM 为 turn 直接子 span；tool span 的父是产出它的 LLM span；trace 唯一 | PASS |
| I02 | `test_i02_parallel_tools_share_one_parent_and_next_call_is_new` | 同轮两工具 span 不同、共享同一父；后续 LLM 为新 span；预算根一致 | PASS |
| I03 | `test_i03_tool_internal_llm_call_is_a_child_of_the_tool` | 真实 `GoalProgramCompiler` preview；planner span 的父为 tool span；turn/budget/bundle 与根一致；无替代预算根 | PASS |
| I04 | `test_i04_two_owners_never_share_identity_or_version` | `ManagedTurnWorkerPool(concurrency=2)` 真并发：两侧 owner/trace/bundle/turn 不串，`model_invocations.runtime_bundle_id` 集合等于各自 bundle | PASS |
| I05 | `test_i05_failed_turn_does_not_leak_its_context` | 首 turn 抛 `GatewayError` 后 `_call_context.get() is None`；第二 turn trace 全新 | PASS |
| I06 | `test_i06_internal_call_keeps_the_original_bundle_after_a_stable_switch` | stable 切到新 bundle 后，内部调用仍用原 planner profile 与**原 policy digest**（实际请求路由，而非只记旧 ID） | PASS |

### 3.3 A01～A09（`tests/test_harness_context_approval.py`，13 passed）

| ID | 用例 | 关键断言 | 结果 |
|---|---|---|---|
| A01 | `test_a01_restart_restores_the_original_context_and_span` | 重建 runtime 后批准恢复：原 JSON/digest 逐字节不变；`chat_tool.context_resumed` 关联原 call/trace/span/parent span；内部 LLM 为 tool span 子节点；业务副作用一次 | PASS |
| A02 | `test_a02_continuation_budget_bundle_and_trace_do_not_replace_the_original` | continuation 换 bundle/budget/trace 后，原调用仍读原持久化值；continuation 自身的 turn 根确实带新值 | PASS |
| A03 | `test_a03_revoked_tool_is_refused_without_side_effects` | 权限撤销 → `TOOL_NOT_ALLOWED`，handler 与内部 LLM 均未调用，identity 不变 | PASS |
| A04 | `test_a04_tampered_call_is_refused[params\|association\|binding]` | 改 params → `ACTION_NOT_ELIGIBLE`；改关联 → `CONTEXT_IDENTITY_CONFLICT`；改绑定 → `CONTEXT_IDENTITY_CONFLICT`；零副作用 | PASS ×3 |
| A05 | `test_a05_another_owner_cannot_resume_the_call` | 另一 owner 读不到该 turn；以该 owner 的 runner 恢复 → `CONTEXT_IDENTITY_CONFLICT` | PASS |
| A06 | `test_a06_unusable_original_bundle_does_not_fall_back_to_stable` | 原 planner profile 被禁用后编译失败，未回落新 stable bundle（`goal_program_versions == 0`） | PASS |
| A07 | `test_a07_legacy_missing_context_is_refused_and_stays_readable` / `test_a07_corrupt_and_unknown_context_fail_closed` | legacy（两列 NULL）→ `LEGACY_CONTEXT_MISSING` 且旧记录仍可读；digest 篡改 → `CONTEXT_IDENTITY_CONFLICT`；未知 schema → `UNKNOWN_SCHEMA_VERSION` | PASS ×2 |
| A08 | `test_a08_repeated_resume_reuses_the_result_without_rewriting` / `test_a08_crash_before_result_persistence_recovers_one_side_effect` | 重复恢复复用同一 continuation、不重复写；结果持久化前崩溃后恢复，副作用仍为 1 | PASS ×2 |
| A09 | `test_a09_unclear_write_outcome_is_not_retried` | `ToolReconciliationRequired` 路径只执行一次、chat 行保持 `APPROVED` 且 `result is None`、事件 error 为 `TOOL_RECONCILIATION_REQUIRED`、无自动重试 | PASS |

### 3.4 P01～P04（`tests/integration/test_harness_context_postgres.py`，7 passed）

| ID | 用例 | 关键断言 | 结果 |
|---|---|---|---|
| P01 | `test_p01_empty_database_upgrade_matches_the_declared_head` | 空库升级后 `alembic_version == 20260930_0024`，三表均有新列 | PASS |
| P01 | `test_p01_upgrade_from_the_previous_head_keeps_legacy_rows_readable` | 从 `20260921_0023` 升级：legacy 行保留、新列全 NULL；`load_*` 抛 `LegacyContextMissing`；`ConversationService.turn()` 仍可读 | PASS |
| P02 | `test_p02_round_trip_and_legacy_and_half_written` | 三表 round-trip、重启重读同一 root、`run_id` 由 turn 派生、digest 可重算、单边为空 → `ContextStoreConflict`、两列皆空 → `LegacyContextMissing` | PASS |
| P03 | `test_p03_conflicting_bindings_are_refused_on_every_table` | 已绑定行不可被派生 child 重指向；三表逐字段（owner/thread/turn/run/bundle/budget/task_id）冲突全拒；原记录未被覆盖 | PASS |
| P03 | `test_p03_concurrent_writers_of_the_same_context_are_idempotent` | `Barrier(4)` + `ThreadPoolExecutor`：同 Context 全 `ok`；不同 Context 全 `ContextStoreConflict` | PASS |
| P04 | `test_p04_failed_write_leaves_no_partial_row_and_tampering_is_refused` | 写 digest 时注入异常不留半行；篡改 JSON 拒；**JSON 与 digest 自洽但与绑定冲突仍拒**；信任源（`threads.owner_id`）被改仍拒 | PASS |
| P04 | `test_p04_approval_context_is_written_before_the_approval_exists` | `ChatToolCallStore.create(..., execution_context=...)` 在审批存在前即写入可读 Context | PASS |

## 4. 执行命令与结果

### 4.1 新增用例

```
python -m pytest tests/test_execution_context.py tests/test_harness_context_flow.py tests/test_harness_context_approval.py -q
→ 29 passed in 26.74s   (exit 0)

python -m pytest tests/integration/test_harness_context_postgres.py -q   # TEST_DATABASE_URL 指向隔离库
→ 7 passed in 43.70s    (exit 0)
```

### 4.2 既有回归（文档第 8 节命令）

```
python -m pytest tests/test_model_control.py tests/test_model_gateway.py tests/test_routed_model_gateway.py -q
→ 54 passed in 13.58s   (exit 0)

python -m pytest tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py -q
→ 47 passed in 30.64s   (exit 0)

python -m pytest tests/test_conversation.py tests/test_conversation_worker.py tests/test_conversation_protocol_v2.py -q
→ 83 passed in 51.48s   (exit 0)

python -m pytest tests/integration/test_harness_context_postgres.py tests/integration/test_chat_goal_tools_postgres.py \
  tests/integration/test_goal_tool_recovery_postgres.py tests/integration/test_root_task_budgets.py -q
→ 24 passed in 117.24s  (exit 0)
```

原始输出保存在 `outputs/_t07_regression.txt`、`outputs/_t07_regression_pg.txt`、`outputs/_p_series_run4.txt`。

### 4.3 全量套件与失败归因

```
TEST_DATABASE_URL=<isolated> python -m pytest -q -rfE
```

| 轮次 | 结果 | 说明 |
|---|---|---|
| 修复前 | 13 failed, 1905 passed, 4 skipped, 0 errors in 1532.94s | `outputs/_full_suite.txt` |
| 修复后 | **12 failed, 1906 passed, 4 skipped, 0 errors in 1494.98s** | `outputs/_full_suite_after_fix.txt` |

对比 2026-09-22 基线 `19 failed, 1714 passed, 4 skipped, 100 errors`：errors 从 100 降到 0（集成测试第一次真正跑起来），passed 从 1714 涨到 1905。

**全量回归抓到一个真实回归（已修）**：`tests/test_v2_acceptance.py::test_explicit_plan_modification_becomes_an_approval_gated_tool_call`。
artifact 驱动的 `modify_plan_document` 待审批调用走的是 `ConversationService._suspend_artifact_modification`，**不是** `ChatToolRunner._request_approval`——这是本阶段漏掉的第二个工具调用创建点。它既没有写 `execution_context`（恢复时被判 `LEGACY_CONTEXT_MISSING`，写入静默失败、计划版本停在 1），也是先 `approvals.request` 再 `store.create`（违反第 5.1 节"不得留下可执行的孤立审批"）。修复内容：

- 新增 `ManagedTurnWorker._turn_tool_harness(turn)`：从持久化的 turn 根读回并派生工具 span；
- `store.create(..., execution_context=tool_harness)` 先写行与身份，再 `approvals.request`，最后 `attach_approval`。

**其余 12 个失败的归因（均非本阶段引入）**：

| 用例 | 归因 |
|---|---|
| `test_plan_security.py` ×3 | Windows 符号链接用例，2026-09-22 基线即在列 |
| `test_golden_journey.py` ×1 | 需要真实 DNS 解析 `sqlite.org`，本机解析不到 |
| `test_evaluation_api.py` ×1、`test_m5_release_gates.py` ×2、`test_real_evaluation.py` ×2 | 未声明 `BETTER_AGENT_COST_MODE=enforce`，限额默认 `observe` 不拦截；即 2026-09-22 已定性的那批 |
| `tests/integration/test_learning_v2_postgres.py::test_postgres_independent_learning_budget_and_unknown_recovery` | 同上（`aggregate budget` 断言依赖 enforce 模式）；此前是 E，从未真正跑过 |
| `tests/integration/test_db_target_guard.py::test_alembic_never_runs_against_a_rejected_target` | 既有测试隔离缺陷：`postgres_url` 是 **session 作用域**，该用例靠 `monkeypatch.setenv` 换目标，但只要本会话有更早的用例先取过该 fixture，改写环境变量就不再生效（单跑通过、全量失败）。此前是 E，从未真正跑过 |
| `tests/integration/test_m1_live_harness.py::test_live_harness_exercises_one_complete_round_without_network` | 测试桩过期：`scripts/m1_live_acceptance.py` 的 `CapturingMemoryContext`（09-12）缺少 `memory_v2.py`（09-19）新增的 `load_continuation`。此前是 E，从未真正跑过 |

以上 3 个"此前是 E"的用例属**新暴露**而非新回归：基线里它们根本没执行。它们暴露的是既有测试自身的问题（成本模式声明缺失、fixture 作用域、桩过期），本阶段未改动其被测代码，也未调整任何测试库安全限制。

## 5. 门禁逐条核对（文档第 9 节）

| 门禁 | 结论 | 依据 |
|---|---|---|
| U01～U10、I01～I06、A01～A09、P01～P04 均有自动化用例且通过 | 满足 | 第 3 节，36/36 PASS |
| 新链路每层公共身份可从持久化记录验证；JSON/digest 成对、重算一致、与已有列及关联一致 | 满足 | I01～I03、A01、P02、P03 |
| trace 内 span 无重复、无自指；每个非根 span 父节点可定位 | 满足 | I01、I02 的 span 去重与父子断言 |
| 多用户并发无串上下文；外部参数不能影响可信身份 | 满足 | I04（真并发）、U05/U08、A04 |
| 审批重启恢复不换 owner、原 turn/run、budget、bundle 或 trace lineage | 满足 | A01、A02 |
| 当前权限撤销仍能阻止执行，恢复 Context ≠ 恢复旧授权 | 满足 | A03（并有 A05 跨 owner 拒绝） |
| WRITE 幂等、预算继承、超时 reconciliation、模型路由无新增回归 | 满足 | A08、A09、I02/I03 预算一致、I06 路由绑定；回归 184 passed |
| 迁移在隔离 PostgreSQL 验证；历史缺 Context 的待审批记录按明确策略处理 | 满足 | P01（两条升级路径）、P02/A07（`LEGACY_CONTEXT_MISSING` fail-closed） |
| 未以本阶段宣称全项目统一 Context / Trace 平台 / 全链路 ContextSnapshot 完成 | 满足 | 第 0、6 节 |

## 6. 剩余范围与兼容策略（T08）

**范围复核证据**

- 公共身份只在 `app/execution_context.py` 内构造：`grep -rn "HarnessExecutionContext(" app/` 在 `execution_context.py` 之外**无匹配**。
- `create_root_context` 唯一调用点是 `harness_context_store.load_or_create_turn_context`（可信入口），业务代码无法自带 trace/owner。
- 最小链路上**两个**工具调用创建点都已带身份：模型发出的工具调用（`ChatToolRunner._request_approval` / 直接执行）与 artifact 驱动的挂起（`_suspend_artifact_modification`）。`resume()` 只从持久化记录重建，不再有第二个身份来源。
- 未新增 `PolicyEngine` / `TaskRuntime`（`grep -rn "class PolicyEngine\|class TaskRuntime" app/` 无匹配）；ToolRegistry 的鉴权、执行与 reconciliation 未被重写，仅在入口增加 Context 参数与校验。

**历史待审批兼容策略**

| 情形 | 行为 |
|---|---|
| 待审批记录两列皆 NULL（旧数据） | `LegacyContextMissing` → `resume()` 返回 `LEGACY_CONTEXT_MISSING`，**拒绝执行**；记录本身仍可查询 |
| 单边为空 | `ContextStoreConflict`，视为损坏，拒绝 |
| JSON 与 digest 不符 | `ContextStoreConflict`，拒绝 |
| digest 自洽但与行/关联冲突 | `ContextStoreConflict`，拒绝 |
| schema 版本未知 | `UnknownSchemaVersion` → `UNKNOWN_SCHEMA_VERSION`，拒绝 |

即：**不做静默回退、不做自动补写**；旧待审批需要重新发起。

**恢复操作说明**：`ChatToolRunner.resume(call)` 完全依赖数据库原记录重建身份（`store.load_execution_context` → 绑定复核 → 当前 `allowed_names()` 复核 → `ToolExecutionContext.from_harness`），不读取当前执行器的环境值。审批等待期间不新建 span；只有恢复后真正发起的内部 LLM 调用才建 child span。

**接入说明**：`docs/HarnessExecutionContext-接入说明-2026-09-30.md`（API 速查、绑定矩阵、接入检查清单、反模式、恢复顺序、未迁移清单）。

**未迁移入口（明确列出，不视为已覆盖）**：Research / Learning / Goal Review 等独立 Runtime 与独立发起的非工具计划编译入口维持原行为，本阶段未强制统一。

## 7. 复现步骤

```bash
cd backend
export TEST_DATABASE_URL="postgresql://better_agent:<pwd>@127.0.0.1:5432/<name>_test"
python -m pytest tests/test_execution_context.py tests/test_harness_context_flow.py \
  tests/test_harness_context_approval.py tests/integration/test_harness_context_postgres.py -q
```

注意：`TEST_DATABASE_URL` 的库名必须以 `_test` / `_tests` / `_pgtest` 结尾且主机为回环地址，否则 `db_target_guard` 会 fail-closed。`isolated_postgres_test` 会在每个用例前后 `TRUNCATE` 所有应用表，**绝不能指向开发库**。
