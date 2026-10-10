# TaskRuntime 切换说明

R 与 A 固定版本全量门禁均已通过。本文说明部署时必须遵守的切换约束，不表示已部署。

## 存储权威

| 执行路径 | 权威记录 | 新增字段 |
|---|---|---|
| 研究 | research_jobs | lease_epoch |
| 专家 | agent_tasks | 无，沿用既有 epoch/owner/deadline |
| 对话 | turn_jobs | 无，沿用既有 epoch/owner/deadline |
| 计划物化 | turns | direction_projection_epoch、direction_projection_attempts |
| 目标运行 | runs | execution_status、execution_owner、execution_until、execution_epoch、execution_attempts |

TaskRuntime 在上述记录上领取、续租和检查执行权。领域服务选择任务、更新报告或计划、写事件；这些操作与任务状态在同一事务完成。工具副作用仍由 tool_execution_claims 对账，成本仍由原账本结算。

目标的 execution_status 只表示是否有请求正在执行。审批等待、用户输入等待及目标完成继续由 runs.state 表示；退出请求释放租约，不把等待状态当作失败。计划物化 READY 表示草案已持久化，可继续使用既有幂等键完成物化。

## 升级顺序

1. 停止接收新执行请求，停止旧版对话、研究、专家 worker 和 API 进程；等待其退出。禁止新旧 worker 混跑。
2. 备份数据库，运行 Alembic `upgrade head`。0027 只增加字段，不搬迁、清空或重新生成历史结果。
3. 启动新版进程。原有研究和物化中的记录保留截止时间，过期后由新实例领取并分配新 epoch；已完成报告和 READY 草案保持原读取路径。
4. 检查审批恢复、取消、任务查询以及工具对账。unknown 写入只能恢复结果或人工对账，不能因重新领取任务而再次写入。

SQLite 仅用于既有测试兼容；字段在历史迁移执行完成后补齐。生产数据库仍使用 Alembic 管理。

## 回退限制

- 先停止所有新 worker/API，再考虑回退，不能仅切换部分实例。
- 0027 downgrade 遇到运行中的研究、正在编译的计划物化或持有执行租约的目标会拒绝删除字段。先完成或取消在途工作。
- 回退代码会失去本次新增的 fencing 能力。回退不等于允许旧版与新版同时执行。
- 审批绑定能力定义摘要；缺少摘要的历史审批不自动补授权，应重新发起逻辑调用。

## 范围

Learning、归档、embedding、通知等辅助后台任务未纳入本次 TaskRuntime。任务执行权检查与远端请求不构成跨系统原子事务；已发送请求的未知副作用仍需原有对账。

## 对话任务能力配置

- 使用 `BETTER_AGENT_LOOP_MODE=loop`（当前默认值），对话工具包括 start_research、delegate_experts、get_task、cancel_task。legacy 作为历史兼容开关，不提供新增任务查询/取消工具。
- 生产预算需要显式配置 `BETTER_AGENT_COST_MODE=enforce` 及原有模型、价格和预算；仅设置 observe 不会启用货币限额。
- 继续使用现有研究/专家 worker 启停方式；不新增调度进程。get_task 只读取已提交状态，不触发后台模型或自动轮询。
- TaskRef 为 `{kind: research|expert, id: ...}`；研究 id 是 job_id，专家 id 是 coordinator_task_id。保留原启动返回的 job_id/run_id，旧客户端无需改名。
- 查询和取消只允许当前线程、当前 owner 的任务。删除线程后不能查询或继续发布；同一结果重复完成仍返回原结果，冲突内容拒绝。
- 结果与对应报告、线程消息、终态事件同事务。事务失败后可用已保存结果重新提交，不需要再次请求模型。专家消息关联原 source_turn_id，后续对话采用现有历史打包/归档约束。
- 已有历史专家消息若仅关联 task_id，没有自动回填到 turns。新提交使用正确的 source_turn_id；本次没有扫描修改历史消息。

## 最小真实 smoke

2026-10-10：deepseek-flash，5 个对话步骤、7 次调用；只启用对话 worker，后台研究/专家模型未运行。预算上限 0.10 USD、单调用 0.03 USD、最多 10 调用、每调用 1 attempt；账本估算 0.007802 USD。结果保存在 `outputs/mainline-a-live-smoke/`；该目录为本地运行产物，不是产品入口。隔离 PostgreSQL 容器使用独立卷保存中断恢复数据，不连接开发或生产数据库。
