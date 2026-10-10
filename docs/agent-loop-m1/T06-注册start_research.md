# T06 注册 start_research

状态：已完成（基础创建、幂等、上下文、缺服务、租约冲突与身份拒绝，6 passed）  
依赖：T04、T05  
阻塞：T09

## 目标

把「开深度研究」做成移交型工具。执行体调用 T05 的 `handoff_start_research`。本步可以先挂在注册表上，由测试直接执行；不必打开 `loop` 开关。

## 要做

1. 在 `backend/app/conversation_capabilities.py`（新建）注册 `start_research`：
   - 参数：`topic`（1–2000 字符）、`scope ∈ {web, local_note}`
   - `reject_identity_params=True`；`turn_id` / owner / 预算根由 context 注入
   - `context_handler` 调用 `handoff_start_research`
2. 前置失败（活动回合冲突、`context_incomplete`、服务未配置）返回 `ToolResult(ok=False, error=...)` 观察，不抛到入口。
3. 成功返回 `handoff`，含 `job_id`。
4. `ChatToolRunner.allowed_names` 在能力已注册时包含该名字（可用配置或显式 allowlist，不必等 T09）。

## 验收

| ID | 断言 |
|---|---|
| AL-T09 | 创建研究任务；回合 `policy=start_research`；行与事件与旧路径一致 |
| AL-T10 | 前置失败为观察，回合不变成研究 |
| AL-T13 | 模型传入 `owner_id` 等被拒，零副作用 |
| AL-T14 | 同一回合只产生一个研究任务 |

## 禁止

- 不删除正则 `_is_explicit_research_command` 与分类调用（T09/T14）。
- 不实现完成后回到对话循环。
- 不改研究引擎内部流水线。
