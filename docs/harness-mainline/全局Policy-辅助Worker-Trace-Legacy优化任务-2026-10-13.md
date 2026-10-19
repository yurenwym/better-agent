# 全局 Policy、辅助 Worker、Trace 与 Legacy 优化任务

日期：2026-10-13<br>
范围：LLM Harness 主链路及其后台任务<br>
目标：在不扩大业务范围的前提下，统一授权边界、后台执行权和可观测性，并为 Legacy 删除建立证据。

## 一、代码现状

| 模块 | 现有基础 | 当前缺口 |
|---|---|---|
| Policy | `policy_engine.py` 已有纯函数 `decide()`；模型、工具、预算和任务控制已部分接入 | 输入仍是布尔事实，各入口分别组装规则；资产、通知、Learning promotion 没有完全接入 |
| Worker | Archive、Embedding、Learning 已各自实现租约；核心任务已有 `TaskRuntime` | 租约、恢复、事件和提交协议仍分散 |
| Trace | 已有 `EventMetadata`、Envelope、事件投影、Thread/Run 轨迹页面 | 对话、模型、工具、研究、专家、Learning 仍是分开的查询链，缺少统一 Trace 查询 |
| Legacy | 新对话主循环默认走 loop；Learning V3 可配置启用 | `learning_legacy.py`、旧对话路由和旧保存分支仍然可达，缺少生产使用证据 |

## 二、总体顺序

实际开发顺序调整为：

```text
Policy 契约 → Trace 关联字段 → Worker 适配 → Legacy 清理
```

Worker 迁移必须先具备 Trace，否则无法验证租约丢失、重复发送和结果提交是否正确。

## 三、开发约束

1. 不把所有业务规则塞入一个巨大 PolicyEngine。
2. 领域服务继续负责业务状态、来源校验、checkpoint 和提交事务。
3. TaskRuntime 只负责执行权、租约、取消、续租、fencing 和通用生命周期。
4. 已经可能产生外部副作用的操作不能依赖盲目重试，必须保留对账状态。
5. Legacy 只能在有调用证据、迁移测试和回退方案后删除。
6. 所有新增写操作默认拒绝，审批、预算、来源和身份必须由服务端事实决定。
7. 不新增与现有领域服务重复的业务实现。

---

## 阶段 P：全局 Policy 泛化

### P01 定义统一 Policy 请求

修改：

- `backend/app/policy_engine.py`
- 新增或补充 `PolicyRequest`、`VerifiedFacts`、`PolicyDecision`

统一请求至少包含：

```python
PolicyRequest(
    subject=SubjectRef(...),
    action=ActionKind.MODEL_SEND,
    resource=ResourceRef(...),
    phase=PolicyPhase.SEND,
    facts=VerifiedFacts(...),
)
```

事实包括：身份、能力、审批、来源、预算、取消状态、租约和执行权。

验收：

- 未注册能力、越权身份、来源失效、预算不足、租约失效均有稳定拒绝原因。
- 决策函数保持纯函数，可单独测试。
- 不允许模型输入直接构造可信身份或预算事实。

### P02 统一模型发送边界

修改：

- `backend/app/model_gateway.py`
- `backend/app/model_control.py`
- `backend/app/send_authority.py`

所有首次发送、重试和 fallback 都经过同一 Policy Adapter，并在发送前重新检查：

- invocation 是否仍有效
- 来源资产是否撤销
- root budget 是否仍可用
- cancel 是否已生效

验收：撤销来源或取消任务后，首次发送和每次重试都不会发出请求。

### P03 统一工具与任务控制边界

修改：

- `backend/app/tool_executor.py`
- `backend/app/task_capabilities.py`
- `backend/app/research/service.py`

将工具执行和任务查询/取消统一转换为 PolicyRequest。领域服务仍负责具体状态变化。

验收：

- owner、thread、project 不匹配时统一拒绝。
- 工具审批不替代身份、来源和预算检查。
- 取消已完成任务返回终态，不产生新的副作用。

### P04 补齐资产和通知 Policy

修改：

- `backend/app/learning_assets.py`
- `backend/app/learning_promotion.py`
- `backend/app/notifications.py`

为以下动作补齐统一决策：

```text
asset.read
asset.write
asset.promote
notification.send
notification.retry
```

验收：Learning promotion、记忆资产读取和通知发送都有统一的身份、审批、来源和外部副作用状态。

---

## 阶段 T：统一 Event Envelope 与 Trace

### T01 固定 Trace 关联字段

修改：

- `backend/app/event_envelope.py`
- `backend/app/event_metadata.py`
- `backend/app/events.py`

统一使用：

```text
trace_id
operation_id
parent_span_id
causation_event_id
attempt
phase
outcome
```

规则：

- 重试沿用同一个 `trace_id`，只增加 `attempt`。
- 子任务使用新的 `operation_id`，通过 `parent_span_id` 和 `causation_event_id` 关联。
- Legacy 事件不能伪造新的 Envelope。
- Envelope 中不得写入密钥、完整 prompt 或未脱敏模型输入。

验收：一次对话可以关联到模型调用、工具调用、任务领取、任务完成和最终结果。

### T02 增加统一 Trace 查询 API

修改：

- `backend/app/api.py`
- `backend/app/event_projection.py`

新增只读接口：

```text
GET /api/traces/{trace_id}
GET /api/threads/{thread_id}/trace
GET /api/tasks/{kind}/{id}/trace
```

要求：

- 先按 owner 授权，再读取事件。
- 统一合并 thread、run、task、worker 事件。
- 老事件返回 `legacy_context_missing` 或 `unsupported_version`。
- 默认返回脱敏投影，审计模式才展开允许字段。

同时修正现有线程 Envelope 查询中固定 `owner_id="local-user"` 的实现，改用可信请求上下文。

### T03 打通 Trace UI

修改：

- `frontend/src/pages/TrajectoryPage.tsx`
- `frontend/src/hooks/useRunTelemetry.ts`
- `frontend/src/hooks/useThreadTelemetry.ts`
- `frontend/src/components/EventStream.tsx`

提供三个视图：

1. 时间线：对话 → 模型 → 工具 → 任务 → Worker → 结果。
2. 因果关系：展示父事件、因果事件、重试次数。
3. 失败详情：展示拒绝原因、失败阶段、是否重试、是否需要对账。

研究、专家和 Learning 使用同一 Trace 数据源，只保留各自的业务卡片。

验收：

- 真实对话的所有关键阶段出现在同一条 Trace。
- 重试不创建新的 root trace。
- 用户不能读取其他 owner 的 Trace。
- 页面不展示敏感输入和模型内部推理。

---

## 阶段 W：辅助 Worker 统一

### W01 抽取公共租约接口

修改：

- `backend/app/task_runtime.py`

抽取最小接口：

```python
claim()
heartbeat()
require_lease()
finish()
recover_expired()
request_cancel()
```

不要直接把所有 Worker 的数据库列强行映射到同一张抽象表。TaskRuntime 负责执行权，领域 Worker 负责自己的业务字段。

### W02 迁移 Archive

修改：

- `backend/app/memory_archive.py`

保留 Archive 特有约束：

- `source_hash`
- transcript 范围
- prompt/tokenizer 版本
- 摘要提交校验

只迁移 claim、heartbeat、lease lost、恢复和生命周期事件。

验收：租约失效后不能提交摘要；同一输入恢复不会重复写入摘要。

### W03 迁移 Embedding

修改：

- `backend/app/embedding_worker.py`

保留：

- `revision_id`
- `profile_id`
- `content_hash`
- embedding profile 和向量维度校验

验收：旧 revision、profile 变化、向量协议错误和租约失效都不能写入错误向量。

### W04 迁移 Learning 的执行权

修改：

- `backend/app/learning.py`
- `backend/app/learning_pipeline.py`

只迁移：

- claim
- heartbeat
- lease lost
- expired recovery

保留 Learning 自己的：

- checkpoint
- learning budget reservation
- source validation
- canary/promotion transaction

验收：暂停 policy、版本变化或租约丢失时不能采用学习结果。

### W05 评估 Notification

修改：

- `backend/app/notifications.py`

先补齐 delivery 状态机：

```text
PENDING → SENDING → SENT
                  ↘ RETRY_WAIT
                  ↘ FAILED
```

确认外部 webhook 的幂等和对账语义后，再决定是否接入 TaskRuntime。不能直接套用模型任务的自动重试。

验收：发送失败、超时、重复请求和 provider 已接收但本地未知时都有可查询状态。

---

## 阶段 L：Legacy 清理

### L01 记录 Legacy 使用情况

为以下入口增加低敏遥测：

```text
legacy.learning.execute
legacy.conversation.route
legacy.plan.save
legacy.archive.fallback
```

只记录 owner、thread、operation、原因、版本和是否产生副作用，不记录完整 prompt。

### L02 禁止隐式 fallback

增加显式配置：

```text
BETTER_AGENT_LEGACY_MODE=disabled
```

新路径失败时不得静默退回 Legacy，应返回可识别的 `LEGACY_PATH_DISABLED`，并写入 Trace。

### L03 删除无生产依赖的实现

删除前必须满足：

- 一个完整生产周期无 Legacy 调用。
- Trace 中无 Legacy 事件。
- 测试不再依赖 Legacy 业务路径。
- 历史任务和结果有新路径读取方式。
- 已验证备份恢复和回退方案。

建议删除顺序：

1. conversation legacy route
2. 旧计划保存 fallback
3. `learning_legacy.py`
4. 旧事件写入路径
5. 最后删除兼容字段和历史测试夹具

---

## 最终验收门槛

### Policy

- 模型、工具、任务、资产和通知使用统一决策契约。
- 来源撤销、预算耗尽、取消和租约丢失均能阻止实际副作用。

### Worker

- Archive、Embedding、Learning 的执行权行为统一。
- Notification 在确认外部幂等前不强行迁移。
- 所有 Worker 都能报告 claim、heartbeat、commit、失败和恢复事件。

### Trace UI

- 一次对话可以看到完整跨组件因果链。
- 重试、拒绝、未知副作用和 Legacy 路径可区分。
- owner 隔离、脱敏和旧事件兼容检查通过。

### Legacy

- Legacy 使用量有数据证明。
- 默认路径不再隐式降级。
- 删除后完整回归和历史结果读取通过。

## 推荐第一轮实施范围

第一轮只做：

1. P01：统一 Policy 请求和 Decision。
2. T01：固定 Trace 关联字段。
3. T02：实现 Thread/Run/Task 的只读 Trace 查询。
4. W01：抽取最小租约接口。
5. W02：迁移 Archive。

第一轮不做：

- 删除 `learning_legacy.py`。
- 强行迁移 Notification。
- 重写所有领域服务。
- 引入新的调度系统。
- 改变现有业务审批语义。

每完成一个小任务，都必须补充单元测试、跨调用幂等测试和至少一条真实 Trace 验证，再进入下一项。
