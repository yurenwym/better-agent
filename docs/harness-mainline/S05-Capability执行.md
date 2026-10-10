# 阶段 5：拆分能力定义与执行

前置 P05。迁移对象：tools.py 的 ToolRegistry；复用 chat_tools.py、domain.py、goal_tools.py、mcp_tools.py。
Registry 只管理定义/查询/schema；Executor 拥有执行流程。现有 AgentLoop.CapabilityExecutor 是窄协议，应适配或复用，避免同名但语义不兼容的接口。

## E01：抽出定义与 Registry

- [x] 清点 ToolSpec/ToolCall/ToolResult、ToolRisk 及全部注册/查询调用点。
- [x] 抽出明确 CapabilitySpec/Registry 或等价类型，复用现有 schema、版本摘要与 handler 绑定。
- [x] 动态 MCP 能力保留版本/撤销检查；模型只能选服务端已注册能力。
- 验收：工具 schema、名称、顺序等影响模型输入的行为保持兼容；重复定义冲突明确。
- 禁止：新增发现插件、动态 import DSL、把能力实现塞进 Registry 条件分支。

## E02：只读执行迁移

- [x] Executor 调用 PolicyEngine、校验参数、建立子 context、调用 handler、适配 Outcome。
- [x] 选择一个真实只读工具迁移并扩展到其余同类。
- [x] ToolRegistry 原执行方法作为短期薄适配，不能保留另一套执行实现。
- 验收：取消/参数错误/handler 异常语义不变；读操作 effect=not_applicable；无重复执行。

## E03：搬迁写入执行与对账

- [x] 将审批检查、发送前 claim、结果记录、unknown 标记搬入 Executor，保留原表、ID、幂等键。
- [x] 外部写入前持久化意图；成功确认才 applied，异常/部分完成/响应丢失为 unknown。
- [x] 同一逻辑操作并发、恢复、重复请求复用 claim；不以任务 lease 代替工具 claim。
- 验收：写后崩溃、取消、超时、错误返回、进程重建均不重复写；明确未执行的拒绝保持 not_started。

## E04：迁移对话、目标和 MCP 调用方

- [x] ChatToolRunner、goal_loop 通过 Executor；审批恢复保留原身份及新的 attempt span。
- [x] MCP 调用接入相同管线，保留定义漂移/服务离线/授权失效语义。
- [x] 各入口使用相同 Outcome 适配，领域正文仍按原接口返回。
- 验收：真实审批重启、目标写入恢复、MCP stale definition、不同 owner 请求完整回归。
- 约束：一类入口迁完、验证后再迁下一类，不双路执行。

## E05：清理与门禁

- [x] 删除无人使用的旧执行逻辑；保留接口标记调用方及删除条件。
- [x] 检查依赖方向，Registry 不依赖审批/DB/任务服务，Executor 不持有业务流程分支树。
- [x] 完整测试零失败，记录迁移与兼容出口。
- 验收：新增能力只需注册定义和实现；无需修改循环或集中 if/else 分发。


固定全量：后端 2428 passed、7 skipped；前端 345 passed；构建通过。日志 `outputs/mainline-e-corrected-gate.log`，详见开发记录。
