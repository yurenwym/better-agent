# 自适应 ReAct 轮数与安全上限规格修订

## 修订范围

本文件修订 2026-08-17-personal-agent-v1-design.md 中关于“默认五轮 ReAct 预算、追加预算和剩余预算展示”的产品语义。其余状态机、Checkpoint、工具审批、事件、记忆和模型协议保持不变。

## 核心决策

Agent 的实际结束轮数由模型决定，服务端仍保留不可绕过的安全上限。

模型通过已有的决策动作控制循环：

- complete_step：当前步骤已经完成，立即结束步骤。
- continue：当前信息不足，继续下一次模型决策。
- tool_call：调用一个工具并将结果作为下一次观察。
- await_outcome / blocked：进入对应的等待或阻塞状态。

服务端的 max_react_iterations_per_step 不是任务计划，也不是用户需要管理的普通预算，而是防止死循环、费用失控和异常工具重复调用的安全保护阈值。不能由模型自行增加或取消该阈值。

## 用户界面语义

- 正常执行时不显示“剩余预算”和“追加预算”。
- 进度侧栏显示当前步骤已经执行的 ReAct 循环次数，例如“已执行循环 3 次”。
- 只有因 react iteration budget exhausted 进入 BLOCKED 时，才显示“安全保护阈值已触发”。
- 用户操作命名为“继续执行一次”，语义是给当前步骤增加一次安全保护额度并立即恢复执行，而不是让用户规划模型轮数。
- “继续执行一次”之外仍保留修改计划、取消步骤和结束目标等控制。
- AWAITING_OUTCOME 和普通 EXECUTING 状态不显示追加额度按钮。

## 后端约束

POST /api/runs/{run_id}/budget 只允许在 Run 处于 BLOCKED 且阻塞原因为 react iteration budget exhausted 时调用；amount 必须为正数。接口完成额度增加后，前端紧接着调用 resume，使用户的一次点击形成完整恢复动作。

现有 budget.warning、budget.exhausted、checkpoint.saved 和 run.resumed 事件继续保留，用于轨迹和审计。react_iteration 继续记录实际已经执行的决策轮数，安全剩余额度仅作为运行时内部状态和恢复依据。

## 验收标准

1. 模型返回 complete_step 时可以在安全上限之前结束步骤。
2. 模型连续返回 continue 时，服务端仍会在安全上限处保存 Checkpoint 并进入 BLOCKED。
3. 非预算阻塞、普通执行和等待外部结果时，追加额度接口被拒绝或按钮不出现。
4. 预算阻塞时，用户点击“继续执行一次”只增加一次额度并恢复 Run。
5. 侧栏显示已执行循环次数，不显示“剩余预算 N 轮”。
6. 原有预算耗尽恢复、Checkpoint、事件和前端状态测试继续通过。

