# ContextSnapshot Phase 2A 修复验收报告（2026-10-07）

- 依据：[修复复核 2026-10-07](../review-2026-10-07.md)（F04 定位与建议）、[修复任务与复验 2026-10-06](../../../../ContextSnapshot-Phase2A修复任务与复验-2026-10-06.md)、[上一轮修复报告](../fix-2026-10-06/acceptance-fix-report.md)
- 代码版本：HEAD **`9992533fac42f19b9e1dbe321612d94912e6e8b3`**（2026-10-07，"feat: freeze model input snapshots for Phase 2A"，68 文件）
  - 本轮 F04 修复**尚未提交**，落在工作区（`git status`：`M backend/app/model_control.py`、`M backend/tests/test_snapshot_gateway.py`）
- 解释器：`D:\pycharm\python.exe`（Python 3.13）
- 数据库：SQLite（专项/回归）；PostgreSQL 16 + pgvector（隔离测试库 `better_snapshot_usage_20261007_test`，经 `db_target_guard` 校验）
- 证据目录：`docs/acceptance/context-snapshot-phase2/phase2a/fix-2026-10-07/`
- 结论：**F04 已修复，新增 D08–D12 全部通过；PostgreSQL 五组已复跑通过（44 项）。** 仍有失败/未执行项（§6），故 **M1 仍记为「待复验」**，不宣布全部通过。

> 本报告不覆盖 `../review-2026-10-07.md` 与 `../fix-2026-10-06/` 的原始结论，只在其后追加。

## 1. F04（P2）：响应被拒绝时丢弃已知 usage

### 1.1 缺陷

`RoutedModelGateway.complete` 的响应后资产检查失败时，把 `None` 当作 response 传给结算函数：

```python
self.control_store.finish_attempt(active, ordinal, "failed", "asset_revoked", None)
```

此时真实 `response` 已存在并携带 usage。传 `None` 使 `finish_attempt` 把已知 token 使用量写成空值、
`usage_status` 退化为 `UNAVAILABLE`，成本结算也无法基于实际 usage，只能回落估算。而这次调用
**供应商已经真的执行并计费**——丢掉的是一条真实账单记录。

### 1.2 修复

`backend/app/model_control.py`：

1. **保留已收到的响应**（响应后检查的拒绝分支）：

```python
try:
    self.control_store.assert_request_active(handle.invocation_id, context)
except Exception:
    self.control_store.finish_attempt(active, ordinal, "failed", "asset_revoked", response)
    self.control_store.finish_invocation(active, "failed")
    raise
```

2. **让「同一 attempt 只结算一次」成为结构性保证**：`finish_attempt` 原先只在 `UPDATE ... WHERE id=? AND status='STARTED'` 上防重，
   但成本结算 `settle_attempt` 在 UPDATE **之前**执行，二次调用会重复记账。现在在结算前先确认该 attempt 仍处于 `STARTED`：

```python
current = connection.execute("SELECT status FROM model_attempts WHERE id=?", (attempt_id,)).fetchone()
if current is None or current["status"] != "STARTED":
    return
cost = self.costs.settle_attempt(...) if self.costs is not None else None
```

3. **复核外层异常处理是否可能重复调用 `finish_attempt`**：`assert_request_active` 抛出的 `LearningConflict`
   继承自 `ValueError`，**不是** `GatewayError`，因此 `raise` 不会被 attempt 自身的 `except GatewayError` 再次捕获——
   当前不存在重复调用。第 2 条的守卫把这一点变成不依赖调用路径的硬保证（D12 覆盖）。

### 1.3 四条约束的落实

| 约束 | 落实 | 证据 |
|---|---|---|
| attempt 与 invocation 仍为 `FAILED`；不采用该响应；不设成功的 selected attempt | `finish_attempt(..., "failed", "asset_revoked", response)`；`finish_invocation(active, "failed")` 不传 ordinal | D08、D10（`selected_attempt_id IS NULL`） |
| 实际 token usage 正常保存，费用按已发生使用量结算 | `response.usage` 进入 `finish_attempt` → `usage_status=COMPLETE`、token 列写入、`settle_attempt` 按实际 usage 计价 | D08（`ESTIMATED_COMPLETE` / 2000 micro-USD） |
| 不再 retry/fallback，不修改原快照 | 检查失败即 `raise`，无 `continue`/`break`；快照字节不变 | D10（`max_attempts=3` 仍只发 1 次、快照 digest 不变） |
| 同一 attempt 只结算一次 | `finish_attempt` 的 `STARTED` 守卫 | D12 |

### 1.4 修复前复现（先确认测试能抓到）

临时把 `response` 改回 `None`（随后已还原，工作区无残留标记）：

| 测试 | 修复前 | 现象 |
|---|---|---|
| D08 | **failed** | `assert None == 1000` —— usage 被丢弃 |
| D09[usage-partial] | **failed** | `assert 'UNAVAILABLE' == 'ESTIMATED_PARTIAL'` |
| D12（守卫临时关闭） | **failed** | `model.attempt.finished` 事件 2 条，应为 1 条 |

## 2. 新增验收场景与节点（D08–D12）

| 场景 | 测试 ID | pytest 节点 |
|---|---|---|
| 请求在途撤销，供应商返回完整 usage → 失败、usage 保留、按实际 usage 结算 | D08 | `tests/test_snapshot_gateway.py::test_d08_a_revocation_after_the_response_keeps_the_usage_and_settles_it` |
| 缺失/不完整 usage → 保持已有估算规则，不伪造实际用量 | D09 | `::test_d09_a_revocation_after_the_response_without_usage_does_not_fabricate_tokens`（参数化 2：`[usage-absent]`/`[usage-partial]`） |
| 响应后校验失败 → 网络仅 1 次、无重试、快照不变 | D10 | `::test_d10_a_revocation_after_the_response_does_not_retry_or_rewrite_the_snapshot` |
| 正常响应 → 成功状态与结算行为不变 | D11 | `::test_d11_a_valid_response_after_the_check_still_succeeds_and_settles` |
| 同一 attempt 只结算一次 | D12 | `::test_d12_a_settled_attempt_cannot_be_settled_a_second_time` |

测试用**真实** `LearningAssetService` + `ModelControlStore` + `RoutedModelGateway` + 真实 `CostService`
（注册真实价格快照），**仅模型响应为桩**，不调用付费模型，不调整 JEV 预算策略。

D09 两种形态的既有规则（均已存在，未改动）：

| 形态 | `usage_status` | `cost_status` | `cost_microusd` |
|---|---|---|---|
| 响应完全无 usage | `UNAVAILABLE` | `UNAVAILABLE` | `NULL` |
| usage 存在但字段缺失 | `UNAVAILABLE` | `ESTIMATED_PARTIAL` | `NULL` |

两者都不伪造 token 数，也不给出「已完成」的成本。

## 3. PostgreSQL 复跑（本轮 Docker 已恢复）

1. **引擎可用**：`docker ps` → `better-postgres-1 Up (healthy)`。
2. **新建隔离测试库**：`better_snapshot_usage_20261007_test`（名字以 `_test` 结尾、回环主机，经
   `db_target_guard.assert_isolated_test_database` 校验；**未使用开发库 `better_agent`**），并装 `vector` / `pg_trgm`。
3. **配置 `TEST_DATABASE_URL` 并迁移**：`alembic upgrade head` 后 `alembic current` = **`20260930_0025 (head)`**，
   与 `alembic heads` 一致。
4. **按文件运行五组**（各自日志与退出码见 §5）。

| 组 | 文件 | 结果 | 退出码 | 时长 |
|---|---|---|---|---|
| 1 | `tests/integration/test_model_input_snapshot_postgres.py` | **14 passed** | 0 | 72.65s |
| 2 | `tests/integration/test_harness_context_postgres.py` | **13 passed** | 0 | 76.87s |
| 3 | `tests/integration/test_chat_goal_tools_postgres.py` | **5 passed** | 0 | 27.62s |
| 4 | `tests/integration/test_goal_tool_recovery_postgres.py` | **7 passed** | 0 | 32.98s |
| 5 | `tests/integration/test_root_task_budgets.py` | **5 passed** | 0 | 22.99s |
| | **合计** | **44 passed** | | |

重点项确认：

- **历史数据不补绑**：`test_p01_upgrade_from_the_previous_head_keeps_historical_calls_unbackfilled` ✅
- **快照不可改写**：`test_p07_direct_sql_cannot_rewrite_or_unbind_a_snapshot`（含状态机仍可更新、绑定列不变）✅
- **核心事务回滚**：`test_p05_a_failed_snapshot_write_rolls_back_the_invocation` 等 4 项 ✅
- **K05/K06 幂等并发**：`test_k05_concurrent_writers_with_the_same_identity_have_one_winner`、
  `test_k06_concurrent_writers_with_different_execution_identities_conflict` ✅
- **审批恢复与预算继承**：`test_harness_context_postgres.py`（13）与 `test_root_task_budgets.py`（5）✅

> 上一轮（2026-10-06）因 Docker 引擎不可用未取得 PG 证据，该记录保留在
> `../fix-2026-10-06/acceptance-fix-report.md` §6 与 `../evidence.json` 的 `environment_limited_tests`；本轮已补齐。

## 4. 回归

| 组 | 范围 | 结果 | 时长 | 日志 |
|---|---|---|---|---|
| 专项 | 快照五文件 | **113 passed** | 330.89s | `logs/after-specialty.log` |
| 回归批次 1 | 执行上下文 / harness 链路与审批 / 模型控制 / direct 与 routed 网关（6 文件） | **99 passed** | 347.68s | `logs/after-batch1.log` |
| 回归批次 2 | 编译器 / 目标程序 / 工具（5 文件） | **85 passed** | 291.49s | `logs/after-batch2.log` |
| 对话与协议 | `test_conversation.py` + `test_conversation_protocol_v2.py` | **42 passed** | 43.32s | `logs/after-conversation.log` |
| 成本与发布门禁（仓库默认 observe） | `test_cost_control.py` + `test_m5_release_gates.py` | 2 failed / 31 passed | 35.96s | `logs/after-cost.log` |
| 成本与发布门禁（`BETTER_AGENT_COST_MODE=enforce`） | 同上 | **33 passed** | 31.99s | `logs/after-cost-enforce.log` |

- 专项 113 = 上一轮 107 + 本轮新增 6 个节点（D08、D09×2、D10、D11、D12）。
- observe 模式下的 2 项失败是**既有基线**（`test_m5_release_gates.py` 未声明 `BETTER_AGENT_COST_MODE=enforce`，
  金额限额默认 observe 不拦截）：同一文件 observe 下 2 failed/16 passed，enforce 下 **18 passed**；
  `test_cost_control.py` 单跑 **15 passed**。与本次改动无关。

固定命令（与任务书第 5 节一致）：

```powershell
cd backend
python -m pytest tests/test_model_input_snapshot.py tests/test_model_input_snapshot_store.py `
  tests/test_snapshot_gateway.py tests/test_snapshot_flow.py tests/test_snapshot_recovery.py -q --tb=short -rfE
python -m pytest tests/test_execution_context.py tests/test_harness_context_flow.py `
  tests/test_harness_context_approval.py tests/test_model_control.py tests/test_model_gateway.py `
  tests/test_routed_model_gateway.py -q --tb=short -rfE
python -m pytest tests/test_goal_program_compiler.py tests/test_goal_programs.py `
  tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py -q --tb=short -rfE
```

> 纪律：`-rfE` 连写；`--basetemp` 放 `C:` 且每轮换名；一组 pytest 一次 bash 调用。

## 5. 逐次执行记录

全部日志在 `logs/`，格式为 `START <ISO> / CMD / 结果 / EXIT=<code> / END <ISO>`（本机时间 UTC+8）。

| 日志 | 组 | 起始 | 结束 | 退出码 | 结果 |
|---|---|---|---|---|---|
| `pg-1-snapshot.log` | PG 1/5 | 19:10:19 | 19:11:35 | 0 | 14 passed |
| `pg-2-harness.log` | PG 2/5 | 19:11:43 | 19:13:03 | 0 | 13 passed |
| `pg-3-chat-goal-tools.log` | PG 3/5 | 19:13:12 | 19:13:42 | 0 | 5 passed |
| `pg-4-goal-tool-recovery.log` | PG 4/5 | 19:13:49 | 19:14:26 | 0 | 7 passed |
| `pg-5-root-task-budgets.log` | PG 5/5 | 19:14:33 | 19:14:59 | 0 | 5 passed |
| `after-batch1.log` | 回归批次 1 | 19:15:16 | 19:21:06 | 0 | 99 passed |
| `after-batch2.log` | 回归批次 2 | 19:21:14 | 19:26:09 | 0 | 85 passed |
| `after-cost.log` | 成本（observe） | 19:26:14 | 19:26:56 | 1 | 2 failed / 31 passed（既有基线） |
| `after-cost-enforce.log` | 成本（enforce） | 19:28:11 | 19:28:46 | 0 | 33 passed |
| `after-conversation.log` | 对话与协议 | 19:28:52 | 19:29:39 | 0 | 42 passed |
| `after-specialty.log` | 专项五文件 | 19:29:56 | 19:35:31 | 0 | 113 passed |

## 6. 未通过 / 未执行项（如实声明）

1. **仓库默认环境下的 2 项既有基线失败**：`test_m5_release_gates.py::test_restart_maintenance_stops_invalid_canary_without_changing_stable[budget]`
   与 `::test_budget_rejects_before_attempt_and_stop_is_durable`。二者断言金额限额拦截，但未声明
   `BETTER_AGENT_COST_MODE=enforce`，默认 observe 模式不拦截。**既有基线，非 Phase 2A 回归**（enforce 下 18 passed）。
2. **本机延迟导致的环境受限用例（本轮未重跑，保留上一轮记录）**：
   `test_conversation_worker.py::test_active_turn_lease_is_renewed_during_long_model_call`（`lease_seconds=0.2` < 本机
   claim→断言 190–340ms，永久挂起）与 `test_worker_archives_old_history_before_model_and_replaces_raw_source_with_episode`
   （`AGENT_ARCHIVE_WAIT_MS` 默认 2000ms 不足）。均不在 Phase 2A 代码路径上。
3. **本轮未执行 `test_conversation_worker.py`**（上一轮已按默认/放宽两种口径记录）。
4. **未做真实付费模型冒烟**（usage 由桩响应提供；不可变性与结算边界由 mock 覆盖）。
5. **未执行全仓 `pytest`**（既有 12 项固定失败基线不在本轮范围）。
6. **Phase 2B 未实施、未验收**。

### 6.1 本轮发现的遗留不对称（未修改，供决策）

routed 网关在响应返回后**会**再查一次资产（本轮修复的正是这条路径）；direct 网关只有发送前检查，
**没有**响应后检查。`app/real_evaluation.py:1238` 会构造带 `control_store`（且启动期装配了 `learning_assets`）
的 direct 网关，因此「调用在途撤销资产」时 direct 路径会**成功**并采用该响应，而 routed 路径会失败。
本轮任务限定在 F04 的 usage 保留，**未改动 direct 路径**；是否补齐属行为变更，建议单独决策。

## 7. 交付状态

- **代码验收**：F04 修复完成；D08–D12 通过；专项 113、回归 99/85/42、成本（enforce）33、PG 44 全部通过。
- **提交 / PR 状态**：Phase 2A 主体（含 2026-10-06 修复轮）已提交为 `9992533`；
  **本轮 F04 修复仍在工作区，未提交、未形成 PR**。不得把未提交描述为已交付。
- **M1**：**待复验**。Phase 2A 范围内已无阻塞项，未清除的是 §6 的既有基线失败与环境受限用例（均不在 Phase 2A 代码路径上）。
