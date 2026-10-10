# T02 AgentLoop 类型与接口

状态：已完成（类型阶段独立验证：2 passed；随后进入 T03）  
依赖：无  
阻塞：T03

## 目标

新增 `backend/app/agent_loop.py` 的类型与协议，不实现循环。后续 T03 只往这个文件加 `run`。

## 要做

1. 实现总览第 4.1 节的 `AgentProfile`、`LoopGuards`。
2. 实现 `LoopOutcome` 变体：`Final`（含可选 `artifact`）、`Terminal`、`Suspended`、`Handoff`、`Exhausted`、`Failed`、取消结果。
3. 定义协议（Protocol 即可，不接真实网关）：
   - `LoopModel.complete(messages, tools, context, callbacks) -> ModelResponse`
   - `CapabilityExecutor.execute(call, *, bound_text=None) -> CapabilityOutcome`
4. `CapabilityOutcome`：`result` / `pending` / `handoff` / `terminal` / `bind_text`。
5. `LoopGuards` 字段与默认值按总览 4.6 节。
6. 小测试：类型构造、冻结 dataclass、非法枚举拒绝。

## 改动范围

- 新建 `backend/app/agent_loop.py`
- 新建或追加 `backend/tests/test_agent_loop.py`（仅类型）

## 验收

- 模块可导入；无 `AgentLoop.run` 或对 `LiveConversationModel` 的依赖。
- Profile 含 `bind_text` 字段。

## 禁止

- 不实现循环、不注册工具、不改对话或目标运行时。
- 不新增数据库表。
