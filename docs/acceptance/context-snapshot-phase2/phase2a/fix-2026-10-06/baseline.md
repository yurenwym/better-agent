# R00 修复基线（2026-10-06）

本文件记录修复开始前的状态，供 R08 对照。**它不表示任何修复已经实施。**

## 代码状态

- HEAD：`dcfc54e358bdc8ae92e2ea1a41bf56694ea080f7`（与独立复核报告一致）。
- 工作区：未提交。`git status --porcelain` 共 20 条（7 个已跟踪文件被修改，13 个未跟踪新增）。
  第二阶段的实现**全部在未提交的工作区里**，不是 `dcfc54e` 自带的。
- 本仓库的 `.git/objects` 曾被清空过，因此**不做任何 git 写操作**；提交/PR 由人工完成。

已修改（第二阶段相关）：`app/conversation.py`、`app/db.py`、`app/live_model.py`、
`app/model_control.py`、`app/model_gateway.py`。
本轮修复新增改动：`app/goal_program_compiler.py`、`app/goal_programs.py`（F02）、
`app/learning_assets.py`（如需）。

## 环境

- 解释器：`D:\pycharm\python.exe`（3.13，本机唯一带 httpx 的解释器）。
- SQLite：stdlib，WAL。
- PostgreSQL：`pgvector/pgvector:pg16` 容器 `better-postgres-1`，`127.0.0.1:5432`，
  隔离测试库需以 `_test` 结尾并经 `db_target_guard` 校验。
- 模型网络：全部用脚本化桩（`execute_attempt` / `httpx.MockTransport`），不调用付费模型。

## 缺陷复现（修复前，已固化为自动化测试）

三条测试在本轮修复前**确实失败**，原始日志保存在本目录 `logs/`：

| 缺陷 | 复现测试 | 修复前结果 | 日志 |
|---|---|---|---|
| F01 受控 direct 网关缺发送前资产撤销检查 | `tests/test_snapshot_gateway.py::test_d01…`、`::test_d02…`、`::test_d05…`、`::test_d06…`、`::test_d07…` | 5 failed / 2 passed | `logs/before-d-family.log` |
| F03 无 harness 时幂等身份比较不完整 | `tests/test_model_input_snapshot_store.py::test_k02…`（14 个参数化）、`::test_k03…`（2 个） | 12 failed / 6 passed | `logs/before-k-family.log` |
| F02 真实编译器 JSON 修复复用原 span | `tests/test_snapshot_flow.py::test_s01…`、`::test_s03…` | 2 failed / 2 passed | `logs/before-s-family.log` |

### 逐条现象

- **F01**：`DID NOT RAISE LearningConflict`（D01/D02/D05/D06）；D07 抛出 `LearningConflict` 但
  invocation 状态停在 `RUNNING`（`assert 'RUNNING' == 'FAILED'`）——routed 网关的拒绝路径也没有
  结束 invocation。D03（资产有效时正常重试）与 D04（未配置 Learning 服务）在修复前已通过，
  用来防止"把守卫做成禁令"。
- **F03**：`run_id`/`thread_id`/`turn_id`/`agent_task_id`/`root_budget_id` 任一字段变化（含
  null→值、值→null）都得到 `InvocationReplayError` 而不是 `InvocationIdempotencyConflict`；
  单边 harness 的两个方向同样被当成 replay。`owner`/`bundle`/`purpose` 因已在比较集合中而通过。
- **F02**：真实 `GoalProgramCompiler` 先返回非法 JSON 再返回合法结果，产生 2 个 invocation、
  2 个 snapshot，但**只有 1 个 span**（两个 invocation 的 `span_id` 相同）。

## 与独立复核报告的差异

- 复核报告只给出三项缺陷的现象；本轮把每一项都转成了**可独立定位的 pytest 节点**，
  并补上了复核未覆盖的 D07（routed 拒绝后 invocation 仍 RUNNING）。
- 复核报告中"租约用例本次单跑已通过"的记录保留在 `../acceptance-report.md`，本轮 R07 会重跑确认。
