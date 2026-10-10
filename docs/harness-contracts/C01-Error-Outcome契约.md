# C01：统一 Error / Outcome

## 1. 已核对的现状

- `backend/app/agent_loop.py`：Final / Terminal / Suspended / Handoff / Exhausted / Failed / Cancelled；Failed.error 为 Any。
- `backend/app/model_gateway.py`：GatewayError.kind / attempts / 部分 public_message。
- `backend/app/tools.py`：ToolResult.ok / error / meta；ToolReconciliationRequired 继承 ToolRejected。
- `backend/app/goal_loop.py`：将结果映射到运行状态和 reason code，存在通用 INVALID_MODEL_ACTION 回退。
- `backend/app/conversation_loop.py`、`conversation.py`、`runtime.py`：分别承担模型循环适配、对话收尾、目标运行状态变更。

问题是边界含义不一致。统一契约须保留根因和业务含义，不要求一次性删除现有所有异常类。

## 2. 类型与依赖

建议新增 `backend/app/execution_outcome.py`，只依赖标准库，包含不可变值对象、枚举、校验与 JSON 编解码。命名可在 T01 调整一次，语义必须保持。

依赖方向：业务适配层 → 契约；事件契约 → 结果契约。契约不得导入 AgentLoop、网关、工具、数据库、HTTP 客户端或业务服务。

使用 dataclass(frozen=True) 与 StrEnum，除非仓库已有更适合的同等实现。不引入新依赖。优先带判别字段的结果变体；禁止构造允许任何字段组合的“大字典结果”。

## 3. ExecutionOutcome v1

每份结果描述**明确层级的一次执行**，不是整个系统的最终状态：

- scope：model / tool / loop / task；实例身份由执行上下文和事件承载。
- status：completed / awaiting_input / awaiting_approval / handoff / cancelled / exhausted / failed / reconciliation_required / awaiting_external / blocked。
- 已完成结果可携带领域产物引用；等待结果必须携带可持久化 continuation 引用；handoff 必须有已创建的目标引用。
- failed 必须有 ExecutionError；exhausted 必须有稳定限制原因；reconciliation_required 必须携带对账引用、原因及 unknown 副作用。
- cancelled 记录取消原因；若已有不确定写入，必须同时保存对账义务，不能因取消丢弃。
- 同一执行尝试最多一个最终结果；等待结果结束当前尝试，恢复以新的执行尝试产生后续结果。

Outcome 不携带原始异常、数据库连接、回调、任意 Python 对象或未审查模型正文。领域 payload 使用现有明确类型/引用，不把全部业务结构塞进公共契约。

### 关键映射

| 现有语义 | 新边界语义 |
|---|---|
| 模型返回成功 | model.completed；不能据此标记 tool/task.completed |
| Final | loop.completed |
| Terminal.finish_step | loop.completed，task 是否完成由现有运行时决定 |
| Terminal.report_blocked | 按业务原因转换；不能仅因工具成功就标记 task.completed |
| Terminal.await_outcome | 等待外部结果，保留现有检查点；具体 status 映射必须在 T00 根据服务语义锁定，必要时增加明确的 awaiting_external 变体 |
| Suspended.ask / approval | awaiting_input / awaiting_approval |
| Handoff | handoff；目标创建成功不等于研究已经完成 |
| Exhausted | exhausted，区分轮数、时限、重复动作等原因 |
| ToolReconciliationRequired | reconciliation_required；优先于父类 ToolRejected 判断 |
| 其他未知异常 | failed + INTERNAL_ERROR；不得归因于模型非法动作 |

T00 必须穷举实际变体，不能为了套表损失已存在的业务状态。

## 4. ExecutionError v1

字段：code（稳定机器码）、phase（失败边界）、retryable（技术上是否可能恢复）、public_message（安全短消息）、diagnostic_ref（可选受控诊断引用）。已有 provider kind、attempts 等必要信息通过受限、明确的诊断字段保留，禁止任意 meta 倾倒。

最低代码类别：模型超时/限流/认证/预算/配置/支付/未知供应商错误；工具参数错误/拒绝/执行失败；身份或来源失效；内部错误。实际名称和旧码映射由 T00 清单锁定，避免自创未使用的错误分类树。

- 判定使用类型、code 和明确属性，禁止解析 str(exception) 或用户可见文案做控制流。
- public_message 不回显供应商原始响应、密钥、原始提示词或完整工具参数。
- 原始异常仅在受控诊断层保留；序列化结果不可含 traceback。未知错误不能被吞掉后伪装成功。
- 业务等待和移交不是错误；预算耗尽与供应商超时不可混成同一种失败。

## 5. 副作用与重试

每个可能写入的**工具操作**记录 effect：not_started / applied / unknown。只读操作可为 not_applicable。失败后确认未产生副作用，可为 not_started；“已发出请求”或“本地超时”不能证明未执行。

- applied 只表示该操作的预期副作用已确认，不保证整个任务完成。
- 部分成功属于 unknown，并保存已有操作引用供对账；不得用单个“未执行”掩盖。
- retryable=true 是错误属性，不是重试授权。实际重试仍检查权限、来源、预算、尝试上限及工具幂等保证。
- unknown 写操作禁止自动重发，进入现有对账流程。取消、异常转换与进程恢复不得清掉对账检查点。
- 不新增通用重试器；网关拥有模型请求重试，工具恢复沿用现有工具/审批服务。
- 上层聚合不得抹掉工具操作副作用；所有相关操作仍可通过结果引用/轨迹定位。

## 6. 编解码与兼容

持久化结构包含 schema_version=1。显式编码字段并验证合法组合，不以 dataclasses.asdict 直接暴露内部对象。嵌套可变数据必须拷贝或规范化，frozen 不能被当作深度不可变保证。

未知版本显式拒绝或作为历史只读记录展示，不触发执行。未知旧错误映射到保守 unknown/internal 类别，不凭猜测标为可重试。旧接口文案、状态和序列化形状由边界适配维持。

## 7. 禁止

- 仅统一字段名，却在调用方继续检查异常字符串或重复映射同类错误。
- 把正常工具失败一律升级为整个循环失败；保留已有允许模型纠正参数的行为。
- 将所有层的完成、取消、等待折叠成 bool。
- 在基础契约中调用日志、发送事件、自动重试或更改数据库。
- 删除旧异常作为“完成指标”；迁移以真实边界消费者接入为准。

