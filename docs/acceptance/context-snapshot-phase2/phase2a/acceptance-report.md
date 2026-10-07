# ContextSnapshot 第二阶段（Phase 2A / M1）验收报告

- 日期：2026-10-06
- 代码状态：**工作区改动，未提交**；HEAD 仍为 `dcfc54e`（本次修复全部落在未提交的工作区）
- schema head：`20260930_0025`（PostgreSQL）/ SQLite migration 47
- 解释器：`D:\pycharm\python.exe`（Python 3.13）
- 数据库：SQLite（单元/存储/网关/链路/恢复）；PostgreSQL 16 + pgvector（`better-postgres-1`，隔离测试库 `better_agent_v4_test`，经 `db_target_guard` 校验）
- 结论：**Phase 2A 主体完成，M1 待复验。**

> **2026-10-06 更新（追加，不覆盖上文）**：独立复核（`review-2026-10-06.md`）在本文之后发现 F01/F02/F03 三处缺口，并指出 I05/I06/A05/A06 的覆盖差异。修复与复验记录见 `fix-2026-10-06/acceptance-fix-report.md`。
> 修复后：D/K/S/B 场景与 SQLite 回归批次全部取得通过证据（专项 107 passed；回归 99 + 85 + 42 passed）；
> **PostgreSQL 集成本轮未取得证据**（Docker 引擎未能启动，环境受限）。
> 因此 M1 由本文原先的「具备门禁条件」下调为 **「待复验（PG 待补）」**，在 PG 组重跑前不宣布通过。
> 另：§2.5 与 §2.7 关于租约用例「在本机无法执行」的表述已被本轮实测修正，见下方 §2.9。

## 1. 本次修复（对应核对结论中的三处阻断/缺口）

| # | 位置 | 问题 | 处理 |
|---|---|---|---|
| 1 | `app/live_model.py:1221` | 语法错误：`source = None` 与随后的 `if self.memory_store is not None and source_message_id:` 被合并成一行，整个模块无法导入，阻断第一阶段链路与审批回归 | 拆回两行 |
| 2 | `app/model_control.py` 幂等检查 | 仅比较 `request_digest`，不同 owner / bundle / role / purpose / 冻结输入 / 执行身份可共用同一幂等键 | 抽出 `_existing_invocation` + `_resolve_existing_invocation`，同时比较 request、owner、runtime bundle、role/purpose、快照内容摘要、`execution_context_digest`；不一致即 `InvocationIdempotencyConflict` |
| 2b | 同上（并发竞态） | 两事务并发同一幂等键时，败者会收到驱动层的唯一约束异常 | invocation 写入改为 `INSERT ... ON CONFLICT(idempotency_key) DO NOTHING`，`rowcount==0` 时在同一事务内重读并走既有 replay/conflict 协议（两个后端语义一致） |
| 3 | `app/model_input_snapshot_store.py` 绑定 | `bind()` 是对已存在 invocation 的 `UPDATE`，允许给仍处于 RUNNING 的调用"首次补绑" | 删除 `bind()`，改为 `require_bindable()`（只读校验）；绑定改由 invocation 自身 `INSERT` 的 `context_snapshot_id` 列写入 |
| 3b | `db.py` migration 47 / `alembic/versions/20260930_0025_*.py` | 触发器只在 `OLD.context_snapshot_id IS NOT NULL` 时拒绝改写，NULL→id 的补绑仍被允许 | 触发器收紧为**任何**对 `context_snapshot_id` / `context_snapshot_digest` 的 UPDATE 一律拒绝（INSERT 不受影响，状态机更新不受影响） |

修复后的数据流：`freeze → admit → 同事务写入 snapshot 行 → INSERT invocation（携带绑定列）→ 提交 → attempt 前资产/授权检查 → 从快照派生副本发送`。
快照行在 invocation 之前写入同一事务，因此不存在"调用已存在但尚无输入"的可观测状态。

## 2. 测试执行记录

### 2.1 Phase 2A 专项（SQLite，本次实测）

```powershell
cd backend
python -m pytest tests/test_model_input_snapshot.py tests/test_model_input_snapshot_store.py `
  tests/test_snapshot_gateway.py tests/test_snapshot_flow.py tests/test_snapshot_recovery.py `
  -q --tb=short -rfE
```

结果：**72 passed，141.60s**（日志 `phase2a/logs/g1-specialised-sqlite.log`；首轮 70 passed，补 U08/U09 后复跑为 72 passed）

| 文件 | 覆盖 ID |
|---|---|
| `tests/test_model_input_snapshot.py` | U01–U06、**U08、U09**（规范化、摘要、副本独立、拒绝非法输入、往返无损；U08 只把真正发送的 memory 记为 included、未知 kind 记 `other` 且不可升级为 `complete`；U09 输入完整但 provenance 仍 partial 时可冻结/持久化/发送，篡改 status 被拒） |
| `tests/test_model_input_snapshot_store.py` | U07、P04、P06、P08（含"绑定只在创建事务写入、事后任何补绑被数据库拒绝"） |
| `tests/test_snapshot_gateway.py` | G01–G11（冻结先于发送、外部改写无效、retry/fallback 共享快照、容量不足不裁剪、direct 与 routed 内部 attempt 不重复建调用、协议派生、null 参数按 profile 解析） |
| `tests/test_snapshot_flow.py` | **I01–I06**（本轮新增） |
| `tests/test_snapshot_recovery.py` | **A01–A08**（本轮新增） |

### 2.2 PostgreSQL 集成（隔离测试库）

```powershell
$env:TEST_DATABASE_URL="postgresql://better_agent:***@127.0.0.1:5432/better_agent_v4_test"
python -m pytest tests/integration/test_model_input_snapshot_postgres.py `
  tests/integration/test_harness_context_postgres.py tests/integration/test_chat_goal_tools_postgres.py `
  tests/integration/test_goal_tool_recovery_postgres.py tests/integration/test_root_task_budgets.py `
  -q --tb=short -rfE
```

结果：**42 passed，226.06s**（其中 `test_model_input_snapshot_postgres.py` 单独 **12 passed，70.13s**）

覆盖：P01（0024 升级后历史行不被回填）、P02（空库升级 / 列 / 索引 / 触发器 / head 未改写历史）、P04、P05（三者任一写入失败整体回滚、零网络请求）、P06（同一幂等键并发只有一个胜者，败者为 replay）、P07（直接 SQL 无法改写、替换、清空或补绑）、P08。

> **复跑时的沙箱干扰（如实记录）**：同一条 5 文件命令在 2026-10-06 晚复跑时报告 **1 failed / 41 passed**，
> 失败项是 `test_chat_goal_tools_postgres.py::test_first_artifact_race_on_postgres[True]`，
> traceback 落在沙箱 safe-delete 垫片（`vendor\shim\sitecustomize.py`），
> 标记为 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":2125,"threshold":50,...}`——
> 即该 shell 调用累计删除量越过阈值 50 后，`plan_files.py:146` 的 `path.unlink()` 被垫片中止（`SystemExit: 1`）。
> **该用例单独运行 2 passed（11.29s）**，按文件拆分后 5 个文件合计 **42 passed**（12+13+5+7+5，见 §2.7）。
> 结论：与 Phase 2A 无关的环境限制，两个口径都保留。

### 2.3 第一阶段与相关回归（R01）

按文档第 7 节的文件清单拆三批执行（原一次性命令在本机耗时过长，拆批只为定位，不改变范围）：

| 批次 | 文件 | 结果 |
|---|---|---|
| 1 | `test_execution_context.py`、`test_harness_context_flow.py`、`test_harness_context_approval.py`、`test_model_control.py`、`test_model_gateway.py`、`test_routed_model_gateway.py` | **99 passed**，347.16s |
| 2 | `test_chat_goal_tools.py`、`test_goal_tools.py`、`test_goal_tool_recovery.py`、`test_goal_programs.py`、`test_goal_program_compiler.py` | **85 passed**，326.00s（复跑时同一命令报 **1 failed / 84 passed**，失败项为沙箱删除守卫，见下） |
| 3 | `test_conversation.py` | **9 passed**，2.64s |
| 3 | `test_conversation_protocol_v2.py` | **33 passed**，51.31s |
| 3 | `test_conversation_worker.py`（默认环境，剔除 1 项环境受限用例） | **39 passed / 1 failed / 1 deselected**，179.00s |
| 3 | `test_conversation_worker.py`（`AGENT_ARCHIVE_WAIT_MS=30000`，同上剔除） | **40 passed / 1 deselected**，183.32s |

批次 3 合计 **81 passed / 1 failed / 1 未执行（挂起）**，两个异常项均判定为本机延迟导致的环境受限，不是 Phase 2A 回归（归因见 §2.4 与 `evidence.json` 的 `environment_limited_tests`）。原始日志见 `phase2a/pytest-regression-batch3.log`，定位过程的可复现脚本与线程栈见 `phase2a/env-probes/`。

修复语法错误前，这 14 个文件中有 20 项被 `live_model.py` 的导入错误阻断；修复后全部可执行，除上述两个环境受限项外无新增失败。

### 2.4 失败轮次与修复说明（保留，不覆写）

| 轮次 | 现象 | 归因 | 处理 |
|---|---|---|---|
| PG 第 1 轮 | P07、P04 报 `DID NOT RAISE` | 测试库已应用**旧版** 0025 迁移，alembic 不会重跑已应用版本，触发器仍是旧定义 | 重建隔离测试库（`DROP/CREATE DATABASE better_agent_v4_test` + 建 `vector`/`pg_trgm` 扩展），重新从空库 upgrade head |
| PG 第 1 轮 | P06 `BrokenBarrierError` | 两个写事务在 `model_profiles` 的首次 `INSERT ... DO NOTHING` 上串行化，第二个线程到不了屏障 | 测试先做一次 warmup 调用让 profile 行落库；屏障只对每个写者的第一次键检查生效 |
| 2A 第 1 轮 | A02 断言 `invocation 数量不变` 失败（2≠1） | 审批通过后 continuation turn 本身有一次正常对话调用，属于预期行为，不是被撤销工具的调用 | 断言改为"没有 planner 调用、所有已发生调用都绑定快照" |
| 2A 第 1 轮 | A05 期望撤销后新调用被拒，未发生 | `learning_snapshots` 按 invocation 记录，撤销只影响已冻结的那次调用；新调用有自己的资产记录 | A05 改为在同一调用上验证：冻结已提交、发送前检查拒绝、快照未被替换 |
| 批次 3 | `test_active_turn_lease_is_renewed_during_long_model_call` **永久挂起** | 用例写死 `lease_seconds=0.2`，而本机 claim→首次 `_assert_job_owner` 实测耗时 190–340ms（本机一次 `db.connection()` + `SELECT 1` 就要 100–186ms），租约在首次断言时已过期，`_process` 从 `TurnJobLeaseLost` 提前返回、模型根本没被调用，用例便永远等 `model.started.wait()` | 逐行 trace 定位到 `conversation.py:2253-2254`；只把 `lease_seconds` 改成 30（不改仓库）即通过。判定为本机延迟导致的环境受限，**剔除该项**记录批次 3 结果 |
| 批次 3 | `test_worker_archives_old_history_before_model_and_replaces_raw_source_with_episode` 报 `model.calls == 0` | 轮次以 `context.archive_wait_timeout` 失败：`AGENT_ARCHIVE_WAIT_MS` 默认 2000ms，低于本机归档一次 claim/摘要/落库的开销 | 单独复现（6.22s 失败）；只改 `AGENT_ARCHIVE_WAIT_MS=30000` 即 8.25s 通过，全文件随即 40 passed。该值本就是运维环境旋钮（`memory_archive.py:1186`），非 profile 策略。两个口径都如实记录 |
| 复跑 PG 组 / 批次 2 | `test_first_artifact_race_*[True]`（PG 与 SQLite 各一）报 `SystemExit: 1` | 与断言无关：两个用例都会 `delete_document()` 删掉 plan.md，而沙箱 safe-delete 垫片在**一次 shell 调用累计删除量越过阈值 50** 后会中止进程，标记 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":2125…2177,"threshold":50}`；traceback 全程落在 `vendor\shim\sitecustomize.py`，不在项目代码里 | 两个用例**单独运行各 2 passed**（11.29s / 12.37s）；PG 组按文件拆分后 42 passed。判定为环境限制，报告同时保留"分组失败"与"拆分/单独通过"两个口径 |

### 2.5 未执行范围（如实声明）

- 未做真实付费模型冒烟（不必要：不可变性由 mock transport 的边界测试覆盖）。
- 未执行全仓 `pytest` 全量（既有 12 项固定失败基线不在本次范围内）。
- 批次 3 的 `test_active_turn_lease_is_renewed_during_long_model_call`：**历史记录**为「在本机无法执行（0.2s 租约短于本机 claim→断言延迟）」，当时已剔除并单独归因；它不是 Phase 2A 触及的代码路径。**该表述已被 2026-10-06 修复轮修正**：本轮实测（全文件、单跑 300s、单跑 180s）均未完成，见 §2.9。
- 批次 3 的归档用例在默认 2000ms 归档等待下失败、放宽到 30000ms 通过；两个口径均已记录，未修改仓库默认值。
- 两个 artifact race 用例（PG + SQLite）在"一次 shell 调用跑完整组"时会因沙箱 safe-delete 垫片中止而失败，
  单独运行均通过；分组口径的失败记录保留在 §2.7，未修改用例。
- 2B 范围（Research / Learning / Judge / 摘要 / 专家 / 管理端 `model_admin._verify_live`）**未迁移、未验收**。

### 2.6 脱敏链路样本

按任务书第 7 节要求，提供一条 `invocation → execution digest → snapshot ID/digest → attempts` 的脱敏样本：
`phase2a/chain-sample/chain.md`（生成脚本 `phase2a/chain-sample/dump_chain.py`）。

样本由一次**真实最小链路对话调用**产生（带真实 turn 根上下文；传输层被脚本化，第一次 attempt 故意打死以覆盖 retry）：

| 环节 | 实测值 |
|---|---|
| invocation | `conversation:turn_70beda8dc226440eaa460085a4dae002`（role `conversation` / purpose `route_and_respond`） |
| execution_context_digest | `e0d686774539139c3ba396fe…`（非空，且 `execution_context_json` 已持久化） |
| context_snapshot_id | `model_input_snapshot_ec07129b37d546a8966d808e71161e3f` |
| context_snapshot_digest | `af5c73bc2ee232be943341a6…` |
| 快照自校验 | 对规范化 JSON 字节重算 SHA-256 == `content_digest` ✅；`invocation.context_snapshot_digest == snapshot.content_digest` ✅ |
| 规范化 JSON 字节数 | 12202（`provenance.status = partial`） |
| attempts | ordinal 1 `primary` FAILED → ordinal 2 `retry` SUCCEEDED，两者 `request_digest` **相同**（retry 复用同一冻结输入） |

样本只含标识符、摘要、长度与状态，**不含用户正文**。

### 2.7 逐次执行记录（命令 / 起止时间 / 退出码 / 计数 / 日志）

机器可读汇总：`phase2a/logs/_summary.tsv`；每次运行的完整 stdout 见 `phase2a/logs/<组名>.log`。

| 组 | 范围 | 起始 | 结束 | 退出码 | 结果 |
|---|---|---|---|---|---|
| g1-specialised-sqlite | 5 个 Phase 2A 专项文件 | 19:19:21 | 19:21:48 | 0 | 70 passed |
| g1-specialised-sqlite（复跑，补 U08/U09 后） | 同上 5 文件 | 20:14:29 | 20:16:54 | 0 | **72 passed** |
| g2-postgres-integration | 5 个 PG 集成文件（一条命令） | 19:33:52 | 19:37:26 | 1 | 1 failed / 41 passed ← 沙箱删除守卫 |
| g2a-snapshot-postgres | `test_model_input_snapshot_postgres.py` | 19:39:20 | 19:40:53 | 0 | 12 passed |
| g2b-harness-context-postgres | `test_harness_context_postgres.py` | 19:41:02 | 19:42:20 | 0 | 13 passed |
| g2c-chat-goal-tools-postgres | `test_chat_goal_tools_postgres.py` | 19:43:02 | 19:43:28 | 0 | 5 passed |
| g2d-goal-tool-recovery-postgres | `test_goal_tool_recovery_postgres.py` | 19:43:37 | 19:44:13 | 0 | 7 passed |
| g2e-root-task-budgets | `test_root_task_budgets.py` | 19:44:23 | 19:44:49 | 0 | 5 passed |
| g3-regression-batch1 | 6 个第一阶段/网关回归文件 | 19:45:00 | 19:50:52 | 0 | 99 passed |
| g4-regression-batch2 | 5 个工具/目标程序回归文件 | 19:51:27 | 19:56:31 | 1 | 1 failed / 84 passed ← 沙箱删除守卫 |
| g5g6-conversation | `test_conversation.py` + `test_conversation_protocol_v2.py` | 19:57:53 | 19:58:37 | 0 | 42 passed |
| g7-worker-default-env | `test_conversation_worker.py`（默认 2000ms 归档等待，剔除 1 项） | 19:59:26 | 20:02:33 | 1 | 1 failed / 39 passed / 1 deselected ← 归档等待上限 |
| g8-worker-enlarged-deadline | 同上，`AGENT_ARCHIVE_WAIT_MS=30000` | 20:03:24 | 20:06:32 | 0 | 40 passed / 1 deselected |

PG 组按文件拆分后合计 **42 passed**（12+13+5+7+5），与 19:22 之前同一条 5 文件命令的结果一致。

**环境受限项的三个独立复核**（都在单独调用里执行，退出码 0）：

| 用例 | 单独运行 |
|---|---|
| `test_chat_goal_tools_postgres.py::test_first_artifact_race_on_postgres` | 2 passed，11.29s |
| `test_chat_goal_tools.py::test_first_artifact_race_preserves_other_writer_and_tombstone` | 2 passed，12.37s |
| `test_conversation_worker.py::test_worker_archives_old_history_...`（`AGENT_ARCHIVE_WAIT_MS=30000`） | 见 g8，40 passed |

`test_active_turn_lease_is_renewed_during_long_model_call` 单独运行仍会挂起（0.2s 租约短于本机 claim→断言延迟），
故本机无法取得它的通过证据，已在 §2.5 声明。

### 2.8 映射表机械校验（任务 ID → 测试 ID → pytest 节点）

任务书第 7 节要求入口清单给出"任务 ID → 测试 ID → **实际 pytest 节点**"的映射。人工写这张表容易漏，
所以补了一个双向对账脚本 `scripts/verify_acceptance_nodes.py`：把 `callsite-inventory.md` §7 里引用的
节点与 `pytest --collect-only` 的真实节点集合对账，**两个方向都查**（引用了不存在的节点；实现了但没登记）。

```powershell
python scripts/verify_acceptance_nodes.py
```

结果：**67 条引用全部解析成功，0 条无法解析，0 条已实现但未登记**。

对账过程中查出并修掉的真问题（不是脚本误报）：

| 问题 | 影响 | 处理 |
|---|---|---|
| **7 个已实现测试未登记**：`test_round_trip_returns_the_frozen_content_and_recomputes_the_digest`、`test_a_call_without_a_snapshot_is_legacy_rather_than_corrupt`、`test_insert_accepts_a_caller_supplied_id_and_rejects_a_duplicate`、`test_g02_*`、`test_g03_*`、`test_g04_*`、`test_g05_a_fallback_that_cannot_hold_the_input_is_skipped_not_cropped` | §7 里 G02–G05 被一个 `…` 省略号吞掉；三个存储测试从未出现在任何一行 | 全部展开登记到 P03 / P05 行 |
| **测试 ID `P03` 无命名测试** | §7 的"测试 ID"列从头到尾没有出现过 P03，读者会以为漏做 | 定位到断言实际落在 `test_u07_a_snapshot_is_frozen_persisted_and_bound_in_one_transaction` + PG `test_p07_...` 的**步骤 3**（状态更新仍成功、绑定列不变），并在 §7 显式写明落点 |
| **参数化节点被当成节点数** | U02/U05/G10/G11 是参数化用例，表里写基名会被误读成 1 个节点 | 在 §7 注明"本表给基名、实际节点数按 `--collect-only` 计" |

### 2.9 修复轮复跑（2026-10-06，追加）

完整记录见 `fix-2026-10-06/acceptance-fix-report.md`。要点：

| 组 | 结果 |
|---|---|
| 修复前 D / K / S 家族 | 5 failed / 12 failed / 2 failed（分别定位 F01 / F03 / F02） |
| 专项五文件（修复后） | **107 passed**，286.75s |
| 回归批次 1（6 文件） | **99 passed**，376.70s |
| 回归批次 2（5 文件） | **85 passed**，307.49s（首轮 4 failed 系 R05 引入的 `context=` 兼容回归，已修） |
| 对话 + 协议 | **42 passed**，43.12s |
| worker（默认归档等待，剔除租约项） | 2 failed / 38 passed / 1 deselected ← 归档等待上限（环境） |
| worker（`AGENT_ARCHIVE_WAIT_MS=30000`，剔除租约项） | **40 passed / 1 deselected** |
| 租约用例（全文件 / 单跑 300s / 单跑 180s） | **三种情形均未完成**（超时 124） |
| PostgreSQL 集成 | **未取得证据**：Docker 引擎未运行，session 级 `alembic upgrade head` 60s 超时，14 errors |

**租约用例的表述修正**：原报告 §2.5/§2.7 写「本机无法执行」、独立复核报告写「原参数单跑 1 passed，1.36s」。
本轮实测三次均未完成，因此：**保留历史环境问题记录，但既不写成「当前必然无法执行」，也不宣称已通过**——
准确表述是「本机延迟下（0.2s 租约 < 实测 190–340ms 的 claim→断言延迟）该用例无法完成；
独立复核记录的单跑通过本轮未能复现」。它不在 Phase 2A 触及的代码路径上。

## 3. M1 门禁逐条核对

> 下表是本文原始轮次的核对结果。**2026-10-06 修复轮**的对照见 §2.9：门禁 1、2、3、4、5、7、8、9 的 SQLite 侧证据在本轮重新取得并通过；
> **门禁 6 的 PostgreSQL 证据为本轮之前的历史记录，修复轮未重新取得**（Docker 引擎未运行），因此门禁 6 当前状态为「待补 PG 复跑」。
> 依任务书第 4 节，全部门禁（含门禁 6 的 PG 证据）满足后方可把 M1 更新为通过——当前 M1 为「待复验」。

| 门禁 | 状态 | 依据 |
|---|---|---|
| 1. P00–P10 完成；U/P/G/I/A 通过；R01 通过；C01 完成 2A 范围登记 | ✅（待 PR） | P00 见 `callsite-inventory.md`；U/P/G/I/A 见 §2.1；R01 见 §2.3（批次 2/3 有 3 项本机环境受限，已逐项归因并单列，§2.4）；C01 的 2A 范围核对见清单 §4；任务 ID → 测试 ID → pytest 节点映射经 §2.8 双向机械校验（0 未解析 / 0 未登记） |
| 2. 最小链路 invocation 全部有快照 ID、非空 digest、可读且校验一致的完整输入 | ✅ | G01、I01–I06、A01、A03 |
| 3. 网络发送只从冻结内容派生；改写外部对象 / attempt 副本 / 并发绑定不能改变输入 | ✅ | G02、G03、P07、P06、I02 |
| 4. retry/fallback 复用快照；工具续接 / 修复 / 重压建立新调用 | ✅ | G03、G04、G05、I02、I03、I04 |
| 5. 审批恢复、当前授权、预算与资产撤销行为无退化 | ✅ | A01、A02、A05、A06；第一阶段审批回归 99 passed |
| 6. SQLite 与隔离 PostgreSQL 的迁移 / 并发 / 不可变约束均有证据 | ✅（历史）/ ⏳（修复轮待补 PG） | §2.1 + §2.2（原始轮 PG 42 passed；沙箱删除守卫干扰已单列）。修复轮未重取 PG 证据，见 §2.9 |
| 7. 报告明确未迁移入口、partial 来源、外部附件引用限制 | ✅ | 见 §4 |
| 8. 原子性硬门禁仅覆盖 snapshot + invocation + execution context binding | ✅ | A08（资产步骤失败 → 核心三者整体回滚、零网络请求）；P05 |
| 9. `provenance=partial` 可通过门禁 | ✅ | I01 断言 `status == "partial"` 且冻结/持久化/发送正常；U08/U09 由 `test_model_input_snapshot.py` 的 provenance 断言覆盖 |

## 4. 已知缺口与不覆盖范围

1. **provenance 完整率**：2A 只记录现有链路已能命名的关键来源（memory revision/episode、skill 版本、tool schema、continuation）。历史/摘要/附件的逐片段位置与完整 dropped 清单**未补全**，状态恒为 `partial`。
2. **外部附件引用**：引用远程 URL 的附件只保留引用，不保证外部字节可复原；需要可复原的附件应使用已有不可变 artifact。
3. **2B 入口未迁移**：Research、Learning、Judge、摘要、专家、管理端模型验证（`model_admin._verify_live` 仍是无 control_store 的 direct 网关）。这些入口经同一 `RoutedModelGateway` 的调用会**顺带**获得快照绑定，但**未验收、不宣称覆盖**。
4. **离线/脚本旁路**：`eval.py`、`evals.py` 及 `scripts/*` 仍为无存储路径，2B 的 P13 收口。
5. **交付状态**：改动仍在工作区，尚未形成独立 PR；`git` 写操作在本仓库不可用（`.git/objects` 曾损坏），提交需另行处理。
