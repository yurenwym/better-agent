# ContextSnapshot Phase 2A 修复验收报告（2026-10-06）

- 依据：[修复任务与复验](../../../../ContextSnapshot-Phase2A修复任务与复验-2026-10-06.md)、[独立复核报告](../review-2026-10-06.md)、[原验收报告](../acceptance-report.md)
- 代码状态：**工作区改动，未提交**；HEAD 仍为 `dcfc54e`（本轮修复全部落在未提交工作区）
- 解释器：`D:\pycharm\python.exe`（Python 3.13.0，pytest 9.1.1）
- 数据库：SQLite（专项/回归）；PostgreSQL 16 + pgvector（隔离测试库 `better_agent_v4_test`，经 `db_target_guard` 校验）
- 证据目录：`docs/acceptance/context-snapshot-phase2/phase2a/fix-2026-10-06/`
- 结论：**F01/F02/F03 三处缺陷已修复，D/K/S/B 场景与回归批次全部取得通过证据**；PostgreSQL 集成本轮**未取得当前证据**（Docker 引擎未能启动，环境受限，见 §6）。因此本报告**不宣布 M1 通过**，M1 状态为「待复验（PG 待补）」。

> 本报告不覆盖 `../review-2026-10-06.md` 的原始结论，只在其后追加修复记录。

## 1. 三处缺陷：修复前复现 → 根因 → 修复 → 修复后证据

### F01（P1）受控 direct 网关缺少发送前资产撤销检查

| 环节 | 内容 |
|---|---|
| 修复前复现 | `before-d-family.log`：**5 failed / 2 passed / 19 deselected**；现象 `DID NOT RAISE LearningConflict`（D01/D02/D05/D06）与 `assert 'RUNNING' == 'FAILED'`（D07） |
| 根因 | routed 网关在每次 attempt 前调用资产检查，受控 direct 网关只写快照与资产记录，随后直接 `start_attempt` → 网络；且 routed 的拒绝路径没有结束 invocation，留下 `RUNNING` |
| 修复 | ① 新增共享入口 `ModelControlStore.assert_request_active(invocation_id, context)`：输入为**已绑定** invocation + owner + bundle，不重新解析最新 stable / 学习策略；未配置 Learning 服务时该检查不安装。② direct `ModelGateway.complete` 在 `on_attempt_started` 回调**之后**、`start_attempt` **之前**调用它，失败即 `finish_invocation(handle, "failed")` 并 re-raise（不重试、不 fallback、零网络、快照不改写）。③ routed 的内联检查替换为同一方法，并补齐拒绝路径的 invocation 收尾。 |
| 修复后证据 | D01–D07 全绿（见 §4 专项组）：`tests/test_snapshot_gateway.py::test_d01..test_d07` |

### F02（P2）真实编译器 JSON 修复复用执行 span

| 环节 | 内容 |
|---|---|
| 修复前复现 | `before-s-family.log`：**2 failed / 2 passed / 6 deselected**；现象 `assert 'cebccfe…' != 'cebccfe…'`（两个 invocation 同一 span） |
| 根因 | `GoalProgramCompiler._validated` 的两次 `complete` 复用 ambient `ModelCallContext`；purpose 相同，`child_call_context` 不新建 span，修复调用看起来像首次调用的重试 |
| 修复 | ① 新增 `model_control.new_logical_call(context, *, role, purpose)`：从**父**上下文派生独立逻辑调用（新 span/invocation/snapshot），继承 owner/trace/budget/bundle/task，清空 `invocation_id`/`idempotency_key`/`input_snapshot_id`/`context_snapshot_digest`；无 harness 时走 `replace()` 兼容路径。② `_validated` 循环前读一次 `parent = self._parent_context()`，每次迭代从**同一父**派生兄弟 span。③ `goal_programs._model_context` 不再预派生 child span（把 harness 当父）。④ 新增只读 `current_call_context()`；无父上下文时**不传** `context=` 参数，保持旧独立入口的调用形状不变。 |
| 修复后证据 | S01–S05 全绿；脱敏样本 `chain-sample/chain-compile.md` 展示编译/修复两个 span 同挂根 span、trace 相同、快照不同，且首个逻辑调用的 2 个 attempt 共用同一 invocation 与同一 `request_digest` |

### F03（P2）无 harness 时幂等身份比较不完整

| 环节 | 内容 |
|---|---|
| 修复前复现 | `before-k-family.log`：**12 failed / 6 passed / 16 deselected**；现象为第二次调用得到 `InvocationReplayError`（应为 `InvocationIdempotencyConflict`） |
| 根因 | `_execution_identity_digest` 在无 harness 时返回 `None`；`_resolve_existing_invocation` 仅在**双方** execution digest 均非空时比较，也不逐字段比较 run/thread/turn/task/root_budget |
| 修复 | ① `_IDENTITY_COLUMNS` 声明持久化公共身份列（`owner_id, run_id, thread_id, turn_id, agent_task_id, root_budget_id, runtime_bundle_id`，`agent_task_id` 即 harness 的 `task_id`）。② `_existing_invocation` 的 SELECT 补齐这些列。③ `_resolve_existing_invocation` 逐字段严格比较（含 null）并比较 role/purpose，保留 `request_digest` 与 `context_snapshot_digest`。④ 新增 `_execution_identity_conflicts`：单边 harness 一律拒绝作为相同调用重放，双方有 harness 时额外比 `execution_context_digest`。 |
| 修复后证据 | K01–K06 全绿（含 PostgreSQL 并发 K05/K06） |

## 2. 新增/加强场景与节点

| 组 | 场景 | 文件 | pytest 节点（基名） |
|---|---|---|---|
| D（发送前撤销） | D01–D07 | `tests/test_snapshot_gateway.py` | `test_d01_a_direct_gateway_refuses_the_first_send_after_a_revocation` … `test_d07_the_routed_gateway_keeps_its_per_attempt_check` |
| K（幂等身份） | K01–K04 | `tests/test_model_input_snapshot_store.py` | `test_k01_an_identical_call_is_still_a_replay_and_writes_nothing`、`test_k02_without_a_harness_every_public_identity_field_is_compared`（参数化 14）、`test_k03_a_one_sided_harness_is_never_the_same_call`（参数化 2）、`test_k04_the_same_columns_with_a_different_span_or_input_are_a_conflict` |
| K（PG 并发） | K05–K06 | `tests/integration/test_model_input_snapshot_postgres.py` | `test_k05_concurrent_writers_with_the_same_identity_have_one_winner`、`test_k06_concurrent_writers_with_different_execution_identities_conflict` |
| S（编译与修复身份） | S01–S05 | `tests/test_snapshot_flow.py` | `test_s01_a_real_compiler_repair_is_a_second_logical_call` … `test_s05_a_resumed_compile_keeps_its_tool_identity_and_pinned_bundle` |
| B（真实来源及证据） | B01–B03 | `tests/test_snapshot_flow.py` | `test_b01_a_memory_update_is_reselected_and_recorded_by_the_next_turn`、`test_b02_a_skill_update_binds_a_new_version_and_a_revoked_one_stops_the_send`、`test_b03_a_partial_input_with_a_known_reference_freezes_sends_and_stays_honest` |

R06 对既有用例的加强（不是新增 ID，但改变了被验收路径）：

| 用例 | 加强前 | 加强后 |
|---|---|---|
| I05（`test_snapshot_flow.py`） | 两段手写 messages 模拟 Memory 更新 | 真实 `MemoryService` 建候选/确认/编辑版本 + 真实 `ContextAssembler` 选择与组装；断言新旧快照正文、版本引用、摘要与磁盘版本历史 |
| I06（`test_snapshot_flow.py`） | 自定义 `_Compiler` 替身，一次调用 | 真实 `GoalProgramCompiler` + 生产 Context 绑定（`gateway.set_call_context`），断言 child span 与 trace，并断言 ambient 已恢复 |
| A05/A06（`test_snapshot_recovery.py`） | A05 直接调 `assets.assert_request_active`；A06 只覆盖 routed | 参数化 `[direct]`/`[routed]`，在网关**外**统计网络次数与 invocation 终态，不再直接调用检查辅助函数 |

### 2.1 节点名机械校验（2026-10-06）

上表节点名与 `callsite-inventory.md` §7.1 由 `scripts/verify_acceptance_nodes.py` 对账，结果：**87 条引用，0 未解析，0 已实现未登记**。过程中修掉两处：

- 脚本原以 `text.split("## 7.")[1]` 取节，而 `### 7.1` 标题里含相同子串 `## 7.`，导致扫描在 §7.1 处被截断（§7.1 从未被校验）。改为从 `## 7.` 标题取到文件末。
- 本报告初稿的 F01/F02 复现节点名与真实测试名有出入（D02/D05/D06、S02/S03）。以 `--collect-only` 为准修正为：`test_d02_a_direct_gateway_does_not_retry_past_a_revocation`、`test_d05_a_failing_required_check_leaves_no_open_attempt_or_reservation`、`test_d06_a_callback_that_revokes_is_caught_before_the_wire`、`test_s02_a_network_retry_stays_inside_one_logical_call`、`test_s03_a_repair_that_retries_keeps_its_own_identity`。

## 3. 修复过程中发现并修掉的自身回归（如实记录）

| 现象 | 归因 | 处理 |
|---|---|---|
| 批次 2 首轮 **4 failed**：`test_goal_program_compiler.py::test_operations_route_limits_and_repairs_consistently[4 例]` 报 `TypeError: Gateway.complete() got an unexpected keyword argument 'context'` | R05 的改动无条件传 `context=`，破坏了只接受 `request` 的鸭子类型网关替身；这正是任务书要求「保持旧独立入口兼容」的那条路径 | `_validated` 在无父上下文时**不传** `context=`，恢复旧调用形状；复跑批次 2 → **85 passed** |
| B02 首版 `KeyError: 'id'` | `SkillPlatform.version()` 返回的键是 `version_id`，不是 `id` | 测试改用 `version_id` |

## 4. 复跑记录（命令 / 起止 / 退出码 / 计数 / 日志）

全部日志在 `fix-2026-10-06/logs/`。时间为本机时间（UTC+8）。

| 组 | 范围 | 起始 | 结束 | 退出码 | 结果 | 日志 |
|---|---|---|---|---|---|---|
| 修复前 D | D 家族（`-k "d0"`） | 21:00 | 21:00 | 1 | **5 failed / 2 passed / 19 deselected** | `before-d-family.log` |
| 修复前 K | K 家族 | 21:02 | 21:02 | 1 | **12 failed / 6 passed / 16 deselected** | `before-k-family.log` |
| 修复前 S | S 家族 | 21:08 | 21:08 | 1 | **2 failed / 2 passed / 6 deselected** | `before-s-family.log` |
| 专项（修复后，编译器兼容回归修复前） | 快照五文件 | 21:32:11 | 21:37:01 | 0 | 107 passed | `after-specialty.log`（第一段） |
| 专项（修复后，最终） | 快照五文件 | 见日志 | 见日志 | 0 | **107 passed** | `after-specialty.log`（第二段） |
| 回归批次 1 | 执行上下文 / harness 链路与审批 / 模型控制 / direct 与 routed 网关（6 文件） | 22:10:25 | 22:16:45 | 0 | **99 passed** | `after-batch1.log` |
| 回归批次 2 | 工具 / 目标程序 / 编译器（5 文件） | 22:05:06 | 22:10:17 | 0 | **85 passed** | `after-batch2.log` |
| 对话与协议 | `test_conversation.py` + `test_conversation_protocol_v2.py` | 22:16:52 | 22:17:39 | 0 | **42 passed** | `after-conversation.log` |
| worker（默认归档等待，剔除租约项） | `test_conversation_worker.py` | 22:36:43 | 22:40:10 | 1 | **2 failed / 38 passed / 1 deselected** ← 归档等待上限（环境） | `after-worker-default.log` |
| worker（`AGENT_ARCHIVE_WAIT_MS=30000`，剔除租约项） | 同上 | 22:40:10 | 22:43:21 | 0 | **40 passed / 1 deselected** | `after-worker-enlarged-deadline.log` |
| 租约用例单跑 | `test_active_turn_lease_is_renewed_during_long_model_call` | 22:28:12 | 22:33:14 | **124（超时）** | 300s 上限内未完成 | `after-lease-standalone.log`（第一段） |
| 租约用例单跑（复跑） | 同上 | 22:33:36 | 22:36:40 | **124（超时）** | 180s 上限内未完成 | `after-lease-standalone.log`（第二段） |
| PG 集成 | `test_model_input_snapshot_postgres.py` | 22:43:44 | 22:44:51 | 1 | **14 errors**：session 级 `alembic upgrade head` 超时（Docker 引擎未运行） | `after-pg-snapshot.log` |
| PG 集成（Docker 仍在时，早前） | 同上，`-k "k05 or k06"` | 21:5x | 21:5x | 0 | **2 passed**（K05/K06） | 见 §6 说明 |

固定测试命令（与任务书第 5 节一致）：

```powershell
cd backend
python -m pytest tests/test_model_input_snapshot.py tests/test_model_input_snapshot_store.py `
  tests/test_snapshot_gateway.py tests/test_snapshot_flow.py tests/test_snapshot_recovery.py -q --tb=short -rfE
python -m pytest tests/test_execution_context.py tests/test_harness_context_flow.py `
  tests/test_harness_context_approval.py tests/test_model_control.py tests/test_model_gateway.py `
  tests/test_routed_model_gateway.py -q --tb=short -rfE
python -m pytest tests/test_goal_program_compiler.py tests/test_goal_programs.py `
  tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py -q --tb=short -rfE
python -m pytest tests/test_conversation.py tests/test_conversation_protocol_v2.py -q --tb=short -rfE
```

> 注：`-rfE` 必须连写（`-rf -rE` 会被后者覆盖，失败清单消失）；`--basetemp` 放在 `C:` 且每轮换名。

## 5. 完成门禁逐条核对

| 门禁 | 状态 | 依据 |
|---|---|---|
| 1. F01/F02/F03 原始复现已转为自动化测试并在修复后通过 | ✅ | §1 的 before/after 日志；D01–D07、K01–K06、S01–S05 |
| 2. direct/routed 必需资产检查逐 attempt 生效；撤销后无新增网络请求；失败调用状态与预算预留完整 | ✅ | D01–D07（D01/D06 断言零网络 + `FAILED` + 无悬挂 attempt；D05 断言无 attempt/预算预留） |
| 3. 新逻辑调用独立 span/invocation/snapshot；网络重试保持原身份；审批恢复继承原 Tool Context | ✅ | S01–S05；`chain-sample/chain-compile.md` |
| 4. 幂等比较覆盖无 harness、单边 harness、null 差异与 PG 并发；无身份不同却视为 replay | ✅（PG 并发见 §6） | K01–K04（SQLite，14+2 参数化）；K05/K06 早前在隔离 PG 库 2 passed |
| 5. I06/A05/A06/I05 证据来自实际被验收路径 | ✅ | §2 的加强表；I06 用真实编译器 + 生产 Context 绑定；A05/A06 参数化 direct/routed 并在网关外计数；I05 用真实 Memory 服务 + ContextAssembler |
| 6. 原 Phase 2A 专项及相关回归通过；未执行/失败/环境受限项明确列出 | ⚠️ 部分 | 专项 107 passed、批次 1 99、批次 2 85、对话 42 均通过；**PG 集成本轮环境受限未取得证据**，租约用例与 2 项归档等待用例为本机延迟导致的环境受限，均单列于 §6 |
| 7. 验收报告、节点映射、evidence 与实际测试结果一致；代码验收与提交/PR 状态分别注明 | ✅ | 本报告；`callsite-inventory.md` §7.1；`evidence.json`；§7 交付状态。节点映射经 `scripts/verify_acceptance_nodes.py` 机械对账：§7 + §7.1 共 **87 条引用，0 未解析，0 已实现未登记**（脚本原先用 `split("## 7.")` 取节，会被 `### 7.1` 里同样的子串截断——本轮改为从 `## 7.` 标题取到文件末，使 §7.1 一并纳入校验） |

## 6. 环境受限项与未执行项（如实声明）

1. **PostgreSQL 集成本轮未取得证据**。本轮开始时 Docker Desktop 已停止（`docker ps` 报 `failed to connect to the docker API … dockerDesktopLinuxEngine`），沙箱无法保持 Docker Desktop GUI 进程存活（启动后进程被回收，`Get-Process` 计数为 0），因此 `better-postgres-1` 未运行，session 级 `alembic upgrade head` 在 60s 上限内超时，14 项全部 ERROR。**这是环境问题，不是产品问题**：同一文件在 Docker 可用时（本轮更早）`-k "k05 or k06"` 单跑 **2 passed**；原验收报告记录的 5 文件 42 passed 亦保留为历史结果。**待 Docker Desktop 恢复后需重跑该组**。
2. **租约用例在本机无法取得通过证据**：`test_active_turn_lease_is_renewed_during_long_model_call` 在（a）全文件运行、（b）单跑 300s 上限、（c）单跑 180s 上限 三种情形下均未完成。根因与本机延迟有关：用例写死 `lease_seconds=0.2`，而本机 claim → 首次 `_assert_job_owner` 实测 190–340ms（一次 `db.connection()` + `SELECT 1` 需 100–186ms），租约在首次断言时已过期，worker 从 `TurnJobLeaseLost` 提前返回、模型未被调用，用例永远等 `model.started.wait()`（`tests/test_conversation_worker.py:836-849`）。**独立复核报告记录的「原参数单跑 1 passed，1.36s」本轮未能复现**；两个口径都保留，不宣称通过。
3. **worker 文件另有 1 项归档等待用例失败**：`test_worker_archives_old_history_before_model_and_replaces_raw_source_with_episode` 与 `test_worker_does_not_generate_when_required_archive_permanently_fails` 在默认 `AGENT_ARCHIVE_WAIT_MS=2000` 下失败（后者 `assert 'RUNNING' == 'DEAD_LETTER'`，即 2000ms 内未走完归档失败判定），放宽到 `30000` 后同一文件 **40 passed**。判定为本机延迟导致的环境受限，未修改仓库默认值。
4. **未做真实付费模型冒烟**（不必要：不可变性由 mock transport 的边界测试覆盖）。
5. **未执行全仓 `pytest` 全量**（既有 12 项固定失败基线不在本轮范围）。
6. **Phase 2B 未实施、未验收**（Research / Learning / Judge / 摘要 / 专家 / 管理端 `model_admin._verify_live`）。

## 7. 脱敏链路样本

`fix-2026-10-06/chain-sample/chain-compile.md`（生成脚本 `dump_compile_chain.py`，可复跑）：

- 由**真实 `GoalProgramCompiler` + 真实 `ModelGateway`** 产生，传输层脚本化：attempt 1 → 504，attempt 2 → 非法 JSON，attempt 3 → 合法 program。
- 实测结论（样本末尾「结论」节，全部 ✅）：
  - 两个逻辑调用（编译 / 修复）`span_id` 互不相同；
  - 两个 span 的 `parent_span_id` 都等于根上下文 span，`trace_id` 相同；
  - 两个逻辑调用各自绑定不同快照（不同 `context_snapshot_id` / `context_snapshot_digest`）；
  - 首个逻辑调用的 2 个 attempt 共用同一 invocation 与同一 `request_digest`（同一冻结输入）；
  - 两个快照的 `SHA-256(规范化 JSON) == content_digest`，且 `invocation.context_snapshot_digest == snapshot.content_digest`。
- 样本只含标识符、摘要、长度与状态，**不含提示词正文**。

## 8. 交付状态

- **代码验收**：F01/F02/F03 修复完成，D/K/S/B 与回归批次在 SQLite 侧全部取得通过证据；PG 侧待补。
- **提交 / PR 状态**：改动**仍在工作区，未提交、未形成 PR**。本仓库 `git` 写操作不可用（`.git/objects` 曾损坏），提交需另行处理。**不得把未提交描述为已交付。**
- **M1**：不宣布通过。状态记为「Phase 2A 主体完成，PG 复跑后复验」。
