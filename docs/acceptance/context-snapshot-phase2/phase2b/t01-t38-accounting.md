# Phase 2B T01–T38 对账（2026-10-08）

状态按完整验收契约判断；partial 不计通过。详细已执行节点见 `evidence.json`、`callsite-inventory.md`。

| ID | 状态 | 证据/缺口 |
|---|---|---|
| T01 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T02 | pass | 已验证对应调用边界/失败条件；节点详见 evidence.json |
| T03 | pass | 参数化注入 snapshot、invocation、execution-context binding 写入失败；三种情况均零发送，事务内 invocation/snapshot/attempt 全回滚 |
| T04 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T05 | pass | 已验证对应调用边界/失败条件；节点详见 evidence.json |
| T06 | pass | 已验证对应调用边界/失败条件；节点详见 evidence.json |
| T07 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T08 | partial | Research Worker 取消/异常后恢复、ambient reset、旧快照保留已测；RUNNING invocation 重放零新增发送已测。UNKNOWN 不属于 model_invocations 状态，业务 UNKNOWN 的完整来源/恢复契约仍需逐项定位 |
| T09 | pass | `test_snapshot_agent_entries.py::test_t09_t10_expert_workers_isolate_interleaved_owners_and_judges` 两种汇总分支：真实 fan-out、专家、汇总、Judge；每次发送独立快照，汇总包含真实专家产物，任务/owner/bundle 绑定 |
| T10 | partial | 同上：同库两 owner 交错、一专家异常、上下文复位、跨 owner 读取拒绝均通过；本轮 SQLite，PG 根预算绑定隔离尚未验证 |
| T11 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T12 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T13 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T14 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T15 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T16 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T17 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T18 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T19 | pass | 管理端验证指定未激活版本；断言绑定目标 profile、可信 owner/purpose、有快照且 stable channel 未创建 |
| T20 | pass | SQLite 与隔离 PG 均验证跨 owner profile version 拒绝；零 provider 发送 |
| T21 | pass | 管理端缺凭据、缺 control store、零预算及隔离 PG 快照写失败均拒绝发送；失败状态/错误类别及事务回滚有断言 |
| T22 | pass | 动态断言 startup 与 API fallback 两个 ModelAdminService 均注入 runtime 的受控 control store |
| T23 | pending | 尚无满足该用例要求的验收节点 |
| T24 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T25 | pass | 已验证对应调用边界/失败条件；节点详见 evidence.json |
| T26 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T27 | pass | 已验证对应调用边界/失败条件；节点详见 evidence.json |
| T28 | partial | `test_snapshot_production_boundary.py`：Goal API body/query 注入不改变装配、无身份继续拒绝；query_goals 工具拒绝 offline_unbound。全 API/工具构造点可达性审计尚未闭环 |
| T29 | pass | 已验证对应调用边界/失败条件；节点详见 evidence.json |
| T30 | pass | 已验证对应调用边界/失败条件；节点详见 evidence.json |
| T31 | pass | 指定隔离 PG 集合 49 passed；覆盖真实入口绑定、失败零发送、历史行兼容、快照不可篡改、Harness 隔离与并发结算 |
| T32 | partial | AST + 注册清单 + 发送日志 + pytest 收集的只读对账脚本已落地；补登记 3 类入口，修正失效节点。注册 21 个，不代表最终生产分母；仍有无发送记录入口和动态分派待审计，脚本保持非零退出 |
| T33 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T34 | pass | 2026-10-09：`tests/test_snapshot_goal_runtime_entries.py::test_t34_projection_claim_failure_and_ready_retry_keep_the_snapshot`；真实 propose_execution 路径，tenant-b，claim 失败零发送，READY 后中断恢复/幂等重放不再发送，独立连接验证快照，owner/turn/bundle/root 与持久化 turn 相同（SQLite root 可为空） |
| T35 | partial | 2026-10-09：同文件 `test_t35_daily_and_period_review_restore_context_after_failure`、`test_t35_adjustment_api_repair_has_independent_committed_snapshots`；覆盖真实 daily Worker、period service、adjustment HTTP API，失败/修复独立快照及 ambient 恢复；本轮未复验 PG goal_operation 根预算及非默认 program owner |
| T36 | partial | 目标 Runtime 的 judge_run_output 正向/缺 owner 零发送已覆盖；专家与 Research Worker Judge 完整契约仍待补 |
| T37 | partial | 存在局部或底层证据，完整生产来源/故障契约尚未闭环 |
| T38 | pass | 同一 Harness turn 先后触发两次辅助分类与主回答；三份 invocation、snapshot、span 均独立，owner 一致且 span 为 turn root 的子级 |
