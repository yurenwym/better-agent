# T12 目标步骤接入 AgentLoop

状态：已完成（目标与恢复测试 9 passed；PostgreSQL 正式网关审批恢复 1 passed；完整 legacy 回归通过；包含持久 trace、守卫与核对 checkpoint）  
依赖：T11  
阻塞：T13

## 目标

`AgentRuntime._execute_step` 改为 `AgentLoop.run`。`loop` 模式下 `decide` 不再要求 action JSON，改为原生 tool call + T11 终止型工具。

## 要做

1. 目标 Profile：`allow_text_final=False`，`terminal` 为三个终止型工具，守卫沿用单步 5 次迭代、墙钟、相同动作、连续错误，并加上「重复失败即停」。
2. `LoopOutcome` 映射：
   - `Terminal(finish_step)` → 现有步骤完成事件与预算重置
   - `Terminal(report_blocked)` → `_block("MODEL_REQUESTED_BLOCK")`
   - `Terminal(await_outcome)` → `AWAITING_OUTCOME` + checkpoint
   - `Suspended` → 现有审批 checkpoint
   - `Exhausted` → 现有 `_block(reason)`
3. 取消 `continue` 动作：继续即进入下一轮。
4. 恢复：`_pending_action` 仍作为循环恢复后的第一个待执行调用。
5. `legacy` 模式保持 action JSON 循环，直到 T14。

## 验收

| ID | 断言 |
|---|---|
| AL-T19 | 三终止型映射到完成 / 阻塞 / 等待，事件与旧路径一致 |
| AL-T20 | 写工具审批 checkpoint；批准后工具不重复执行 |
| AL-T21 | 纯文本不当完成；提示后计入迭代；耗尽则阻塞 |

既有目标运行时、审批、checkpoint 测试在 `legacy` 下不回归；`loop` 下补上述断言。

## 禁止

- 不改 `plan` / `needs_clarification` / `reflect`。
- 不把研究/专家接到目标步骤。
- 不改默认开关为 `loop`。
