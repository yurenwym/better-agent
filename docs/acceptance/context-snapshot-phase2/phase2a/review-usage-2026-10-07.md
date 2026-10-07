# usage 修复与 PostgreSQL 复验的独立核对

日期：2026-10-07。基于 `9992533` 之后的当前工作区；本次未修改业务代码。

## 修复核对

`model_control.py:1091` 已把实际 response 传给 asset_revoked 的 finish_attempt 路径。D08～D12 检查真实 CostService 的 token 记录、费用结算、响应不被选中、无重试、快照不变、正常成功路径及顺序重复结束。因此原 usage 丢失问题已修复。

## 新发现：并发结束 attempt 的保护不是原子的（P2）

位置：`backend/app/model_control.py:751`～`:766`。

新增逻辑先 SELECT status，再执行成本结算、条件 UPDATE 和事件写入。PostgreSQL 事务里普通 SELECT 不锁定该行，两个事务可以同时读到 STARTED；后续条件 UPDATE 虽然只有一个实际更新，但未检查 rowcount，两方仍会执行结束事件写入。

独立复现使用另一新建隔离 PostgreSQL 数据库、真实 ModelControlStore 与 CostService。在两个调用均完成状态读取、进入 settle_attempt 前用 Barrier 同步，随后并发结束同一 attempt。结果：两个调用均返回，`model.attempt.finished` 事件为 **2 条**，预期 1 条。

该诊断没有建立费用预留，因此不据此宣称发生重复扣费。现有 CostService._settle 还有账本幂等保护；这里确认的是外层结束流程及审计事件没有做到并发幂等，不能用 D12 的顺序调用测试证明并发安全。

建议二选一：PostgreSQL 先 SELECT ... FOR UPDATE 锁定 attempt 后判断状态（SQLite 继续沿用 BEGIN IMMEDIATE），或用条件更新原子取得结束权且检查 rowcount，再在同一事务中结算并写最终数据/事件。失败必须整体回滚，不能只在重复写事件后忽略异常。

新增真实 PostgreSQL 并发测试，断言只有一方执行结束副作用、结束事件恰好一次、账本无重复、用量与最终状态一致；保留现有顺序重复用例。

## 本次测试

- `test_snapshot_gateway.py`、`test_snapshot_recovery.py`、`test_model_control.py`、`test_routed_model_gateway.py`：**66 passed，36.38 秒**。
- `git diff --check` 通过，仅换行提示。
- Docker 已恢复，五文件 PostgreSQL 复验使用新建隔离库 `better_usage_review_8fc671f4_test`，经 db_target_guard 校验：**44 passed，224.14 秒**。覆盖 `test_model_input_snapshot_postgres.py`、`test_harness_context_postgres.py`、`test_chat_goal_tools_postgres.py`、`test_goal_tool_recovery_postgres.py`、`test_root_task_budgets.py`。
- 本轮正式测试合计 **110 passed**；上述并发诊断另行执行，发现的问题不在当前 110 项测试的覆盖范围内。本轮结果不等同于 Phase 2A 全量验收通过。

不把原报告的测试数量当作本次实测；不覆盖既有报告或原始日志。

## 后续修复：并发结束 attempt

用户授权修复后，`finish_attempt()` 在 PostgreSQL 的状态读取上增加 `FOR UPDATE`。记录锁覆盖状态检查、真实 CostService 结算、attempt 更新和事件写入，直至事务提交或回滚；等待方拿到锁后重新判断状态，已结束则返回。SQLite 继续使用既有 `BEGIN IMMEDIATE`。

新增 `backend/tests/integration/test_attempt_settlement_postgres.py`：

- 并发结束同一 attempt：暂停首个调用的结算，确认第二个调用在 PostgreSQL 等待记录锁，再放行。断言结算函数只调用一次、两个结束事件类型各一条、每个已预留预算维度各一条真实 CHARGE、费用 2000 microUSD、预留归零、usage 与失败状态正确。
- 结算后注入审计事件写入异常：验证 attempt、账本、预算全部回滚；随后重新结束成功且只结算一次。
- 负对照：仅在独立测试进程内移除行锁，同一并发测试按预期失败，结算函数被调用两次；未改动磁盘上的修复代码。

单元与网关回归：**66 passed，32.97 秒**。新增 PostgreSQL 测试首次运行因夹具使用了不合法的 reason=`initial` 产生两个 setup error，已改为已有合法值 `primary`；后续正式结果另记，不将首轮计为通过。

修复后 PostgreSQL 六文件运行结果为 **44 passed、2 failed，238.47 秒**：原有五文件全部通过；新增两项失败于账本总数断言，原因是既有实现会为 INVOCATION / DAILY / MONTHLY 分别生成 CHARGE，不能把跨维度三条记录误认为重复扣费。断言改为预留维度与扣费维度严格一致、每个维度恰好一条且金额为 2000，再单独复跑新增文件：**2 passed，9.88 秒**。

最终本轮分批验证合计 **112 项通过（66 + 44 + 2）**。测试使用独立数据库 `better_settlement_fix_b06d8902_test`，生产/开发库未写入；负对照使用另一个独立测试库。`git diff --check` 通过。本次未提交或推送，不将这些定向测试扩大解释为 Phase 2A 全量验收完成。
