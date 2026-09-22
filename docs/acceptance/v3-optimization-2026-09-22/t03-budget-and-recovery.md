# T03 —— 真实预算与崩溃恢复

日期：2026-09-22
任务书：`docs/V3-自进化机制优化开发任务书-2026-09-22.md` §3 T03
基线：`baseline.md` / `manifest.json`

---

## 0. 一句话结论

**历史归因是错的。** 基线里 19 个失败中有 **15 个**的真正原因是测试没有声明
`BETTER_AGENT_COST_MODE=enforce`，与「用户未提交的 `costs.py` 价格快照链路」无关。
修正后 `test_cost_control.py` 从 **10 failed → 15 passed**，两个 integration 文件从
**6 failed → 14 passed**。另发现一个真缺口：**JEV 的实际花费不入账**（见 §4）。

---

## 1. 归因修正（任务书要求「不默认沿用历史归因」）

### 1.1 症状

6 个 integration 失败 + 10 个 `test_cost_control.py` 失败，症状完全一致：

| 断言 | 期望 | 实际 |
| --- | --- | --- |
| `summary(...)["reserved_microusd"]` | `140` | **`0`** |
| `summary(...)["limit_microusd"]` | `> 0` | **`0`** |
| `set_budget(..., 0)` 后的 reserve | 抛 `BudgetExceeded` | **不抛** |

### 1.2 根因：一个默认关闭的开关

`app/config.py:14`：

```python
def monetary_limits_enabled() -> bool:
    return os.getenv("BETTER_AGENT_COST_MODE", "observe").lower() == "enforce"
```

**默认 `observe`。** 在 observe 下金额限额「只观察不拦截」，于是：

| 位置 | observe 下的行为 |
| --- | --- |
| `costs.py:111` `create_default_root_budget` | `limit_microusd = 0` |
| `costs.py:400` `reserve_attempt` | 预留金额取 `0`（`worst.microusd if monetary_limits_enabled() else 0`） |
| `costs.py:253` `_reserve` | 不做超限检查，永不抛 `BudgetExceeded` |
| `costs.py:196` `set_budget` | 不校验「限额低于当前用量」 |

所以这批测试**全部是空断言**：它们断言 enforce 语义，却跑在 observe 模式下。

### 1.3 只有部分测试文件声明了这个开关

| 文件 | 是否设 `BETTER_AGENT_COST_MODE` |
| --- | --- |
| `tests/test_model_control.py:340` | ✅ 参数化设置 |
| `tests/test_routed_model_gateway.py:353` | ✅ `enforce` |
| `tests/test_startup_contract.py:276` | ✅ `enforce` |
| `tests/test_cost_control.py` | ❌ **没设** |
| `tests/integration/test_root_task_budgets.py` | ❌ **没设** |
| `tests/integration/test_budget_waiting_and_operations.py` | ❌ **没设** |

前三个设了、后三个没设，所以后三个的失败被误读成「产品代码问题」。

### 1.4 修复：让测试自足

在三个文件里各加一个 autouse fixture：

```python
@pytest.fixture(autouse=True)
def _enforce_cost_limits(monkeypatch):
    """These cases assert *enforcement*, so they must declare the enforcing mode."""
    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "enforce")
```

**这是提高门槛，不是降低门槛** —— `enforce` 比 `observe` 严格得多。任务书禁止的是
「降低证据/权限/回放/Canary 门槛换取通过」，这里恰好相反：把本来被默认值放行的断言
真正启用起来。修复后不再依赖操作者的 shell 环境。

### 1.5 第二个独立问题（enforce 后才暴露）

`test_gateway_charges_each_retry_attempt_without_double_counting` 在 enforce 下报：

```
sqlite3.IntegrityError: FOREIGN KEY constraint failed
  connection.execute("DELETE FROM model_invocations")
```

测试的清理代码只删了 `model_attempts`，漏了 `model_invocation_events`（迁移 37，
`invocation_id TEXT NOT NULL REFERENCES model_invocations(id)`）。在 observe 下它没跑到这行
（更早就断言失败了），所以从没暴露。修法：按外键顺序补上 `DELETE FROM model_invocation_events`。

---

## 2. 修复结果

| 测试文件 | 修复前 | 修复后 | 环境变量 |
| --- | --- | --- | --- |
| `tests/test_cost_control.py` | **10 failed, 5 passed** | **15 passed** | 不需要 |
| `tests/integration/test_root_task_budgets.py` + `test_budget_waiting_and_operations.py` | **6 failed, 8 passed** | **14 passed** | 仅需 `TEST_DATABASE_URL` |

证据文件：`t03-cost-control-observe.txt`（修复前）、`t03-cost-control-fixed.txt`、
`t03-budget-baseline.txt`（修复前）、`t03-integration-fixed.txt`、
`t03-budget-enforce.txt`（用外部环境变量确认根因的那一轮）。

> 基线里那 19 个失败，**15 个是这一条**。剩下 4 个（`test_plan_security` 3 个 Windows 符号链接、
> `test_golden_journey` 1 个本机 DNS）与预算无关，归因不变。

---

## 3. T03 场景覆盖矩阵

29 个测试实例（含参数化），逐项对应任务书要求：

| T03 要求 | 覆盖测试 | 结果 |
| --- | --- | --- |
| **预留** | `test_budget_reservation_is_atomic_and_replay_safe`、`test_three_level_attempt_reservation_is_atomic_and_unconfigured_levels_are_unlimited`、`test_concurrent_roles_share_last_root_attempt` | ✅ |
| **结算** | `test_settlement_charges_once_and_releases_the_difference`、`test_three_level_settlement_and_release_are_append_only_idempotent_and_replayable` | ✅ |
| **额度不足** | `test_concurrent_reservations_cannot_overspend_a_budget`、`test_failed_money_reservation_rolls_back_root_count_and_all_ledgers`、`test_ask_does_not_revive_exhausted_execution_or_reset_limits[deadline/attempts/money]` | ✅ |
| **重复请求** | `test_duplicate_reservation_and_new_service_do_not_reset_root`（新建 service 实例后仍幂等）、`test_cost_ledger_uniqueness_is_scoped_by_budget_period` | ✅ |
| **dispatch 前失败** | `test_failed_money_reservation_rolls_back_root_count_and_all_ledgers`（预留失败时 root 计数与全部账本一起回滚） | ✅ |
| **dispatch 后结果未知** | `test_missing_usage_conservatively_charges_all_configured_levels`（usage 缺失时按最坏情况保守计费） | ✅ |
| 超预算时不继续调用（**网络前**阻断） | `test_budget_block_happens_before_network[INVOCATION/DAILY/MONTHLY]`、`test_unbudgeted_postgres_call_is_blocked_before_network` | ✅ |
| 过期/外部 root 在扣费前被拒 | `test_expired_or_foreign_root_is_rejected_before_cost_reservation` | ✅ |
| 并发不超支 | `test_concurrent_reservations_cannot_overspend_a_budget`、`test_concurrent_operation_creation_shares_one_root`、`test_concurrent_answers_only_resume_once` | ✅ |

---

## 4. 真缺口：JEV 的实际花费不入账

### 4.1 先说不是问题的那半

`learning_pipeline.py::_run_chain` 的顺序是正确的：

```
_load_experience  →  learning.dispatch(job)  →  assert_learning_call_allowed  →  decisions.decide(...)
   （本地）            （提交预算 + 派发标记）        （闸门）                    （JEV 调用）
```

`dispatch()`（`learning.py:193`）**确实做了 cycle 级预算预留**，而且是 fail-closed 的：

| 检查 | 行为 |
| --- | --- |
| `config["cycle_microusd"] <= 0` | 抛 `LearningConflict("paid learning has no configured budget")` |
| 日/月**聚合**已派发额度 + 本 cycle > 限额 | 抛 `LearningConflict("learning aggregate budget exhausted")` |
| owner 级 DAILY/MONTHLY 预算未配置 | 抛 `LearningConflict("owner aggregate budget is not configured")` |
| 以上都过 | `create_root_budget(..., limit_microusd=cycle_microusd)`，并写 `dispatched_at` |

`dispatched_at` 同时挡住了 UNKNOWN 重放：重复 `dispatch` 抛
`LearningConflict("remote learning request already dispatched")`。

Learning LLM 与 Judge 走 `LearningAgent(gateway)` / `LearningJudge(gateway)`，
`gateway` 是 `RoutedModelGateway(db, control_store)`，而
`control_store = ModelControlStore(db, events=events, costs=costs)` —— 它在
`model_control.py:260` 调 `costs.reserve_attempt(...)`、`:320` 调 `costs.settle_attempt(...)`。
**这两条路有完整的预留/结算。**

### 4.2 缺口

`JevDecisionService` 的 client 是 `TypesafeClient`（`learning_decision.py:314`），
它直接用裸 `httpx.Client` 打 TypeSafe 端点：

```python
with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
    response = client.post(self.endpoint, json=payload, headers={...})
```

**不经过 `ModelGateway` / `ModelControlStore` / `CostService`**：

- 不写 `model_invocations` / `model_attempts`
- 不 `reserve_attempt` / `settle_attempt`
- 不落 `cost_ledger`

后果：`cycle_microusd` 是一个**静态拍脑袋额度**，不反映 JEV 的真实消耗。
闸门是「cycle 数 × 每 cycle 固定额度」，不是「实际花了多少」。所以：

- ✅ 不会「预算耗尽还继续无限调用」（cycle 级闸门在）
- ❌ JEV 花超了不会即时刹车，账上也看不到

### 4.3 为什么先不改

修它需要先确定两件事，都不是代码层面能定的：

1. **TypeSafe System One 是否按调用计费**，计费单位是什么（按 request？按 token？订阅制？）
2. 若计费，`cycle_microusd` 里应给 JEV 留多少、按什么价格表

在不知道计价模型的情况下塞一个假的 reserve/settle 只会制造**看起来有账、其实是编的**的记录，
比没有账更糟。**建议记为待决策项**，由 wym 确认 TypeSafe 计价方式后再修。

---

## 5. 验收结论

| 任务书 T03 验收项 | 结论 | 依据 |
| --- | --- | --- |
| 超预算时不继续调用 | ✅ | `dispatch` 抛 `LearningConflict`；`_reserve` 抛 `BudgetExceeded`；网络前阻断有 4 个测试 |
| 幂等重试不重复扣费 | ✅ | `_reserve` 按 `idempotency_key` 去重；新建 service 实例后仍幂等 |
| UNKNOWN 不自动再调用 | ✅ | `dispatched_at` 已设时 `dispatch` 抛 `LearningConflict` |
| 已发生的成本不因候选被拒绝而错误撤销 | ✅ | `_settle` 的 charged 计算 + append-only `cost_ledger` 触发器 |
| JEV / 生成 / Judge / replay 收费调用有明确归属 | ⚠️ **部分** | 生成 ✅、Judge ✅、replay 待 T05；**JEV ❌ 不入账**（§4） |
| 不通过新执行器绕过预算 | ✅ | 闸门在 `_run_chain` 的网络 I/O 之前，且 replay 执行器（T05）必须复用同一预算接口 |

**产物**：本报告即「预算状态转换与故障场景记录」；§1 为「全量既有成本失败逐项归因」。

**限制（照任务书）**：本轮使用真实 PostgreSQL 预算服务，但模型侧仍是替身；
「验收脚本移除预算替身的路径」留到 T16 验证。

## 6. 代码改动

| 文件 | 改动 |
| --- | --- |
| `backend/tests/test_cost_control.py` | 新增 `_enforce_cost_limits` autouse fixture；清理补 `DELETE FROM model_invocation_events` |
| `backend/tests/integration/test_root_task_budgets.py` | 新增 `_enforce_cost_limits` autouse fixture |
| `backend/tests/integration/test_budget_waiting_and_operations.py` | 新增 `_enforce_cost_limits` autouse fixture |
