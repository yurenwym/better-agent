# 开发记录

## T00：映射与基线

已核对生产代码（2026-10-09）。数据库：生产 PostgreSQL，SQLite 仅历史单元测试；现有 Alembic head 20260930_0025。

| 生产者 | 统一语义 | 消费者 / 基线测试 |
|---|---|---|
| AgentLoop.Final | loop.completed | conversation.py / test_conversation_loop.py |
| Suspended.ask | awaiting_input，call_id 为恢复关联 | conversation._finish_ask / test_conversation_loop.py |
| Suspended.approval | awaiting_approval，approval_id | conversation / goal_loop / test_goal_loop.py |
| Handoff | handoff，research/expert 目标引用 | conversation 收尾 / test_conversation_handoff.py |
| Terminal.finish_step | loop.completed，运行时决定 task 完成 | goal_loop / test_goal_loop.py |
| Terminal.await_outcome | awaiting_external，run_id 检查点引用 | runtime AWAITING_OUTCOME / test_goal_loop.py |
| Terminal.report_blocked | blocked（正常业务结果），run_id 引用 | runtime BLOCKED / test_goal_loop.py |
| Exhausted | exhausted + 原守卫原因 | AgentLoop / test_agent_loop.py |
| Cancelled | cancelled，已有写入对账义务不能清除 | conversation/runtime/tool store |
| Failed(GatewayError) | 稳定 MODEL_*；未知 kind 保守归类 | goal_loop / test_model_gateway.py |
| ToolReconciliationRequired | reconciliation_required，unknown | 工具执行记录、checkpoint |
| ToolRejected | failed / TOOL_AUTHORIZATION_DENIED | 工具边界 |
| HarnessContextError | failed / IDENTITY_INVALID | 执行边界 |
| 其他异常 | failed / INTERNAL_ERROR | 禁止误报 INVALID_MODEL_ACTION |

Gateway kind：timeout、provider_unavailable、rate_limit、server、authentication、configuration、payment、budget、identity、context_invalidated、context_overflow、structure、request、cancelled、unknown。重试只是属性，沿用网关的预算/权限检查和尝试次数。

ToolResult 的 error 是领域错误字符串，保留旧输出；统一层只分类执行失败，不解析 summary。写工具发出后失败默认 unknown，明确成功才 applied，授权前拒绝为 not_started。

事件：EventStore 的 run 流、ThreadEventStore 的 thread 流各自拥有 seq。run 当前 MAX(seq)+1、thread 当前读计数再更新，PostgreSQL 并发需锁保护。既有 UNIQUE(stream,seq) 和 event_id 主键保留。调用方传 connection 的事务边界须保留。

身份：复用 execution_context.py 工厂和 HarnessContextStore。turn context 已持久化，目标 loop 根上下文存 run.budget.agent_loop_context；工具及模型有各自持久化上下文。审批恢复使用已有 approval/tool ID。TranscriptEvent.canonical 不改。

消费者：conversation 的事件/SSE、runtime 的事件投影、模型调用事件、JSONL 导出。plan.document_ready.source_message_id 是前端必要字段。研究内部 ResearchEvent 本轮不改。

契约修正：新增 awaiting_external 与 blocked 变体以保留上述真实业务语义。统一结果为可持久化摘要；旧领域对象仅留在内部适配返回值，不进入公共序列化。

T00 阅读完成；基线测试与 T01/T02 聚焦测试一起执行，未运行前不宣称基线通过。


## T01—T04：结果阶段

实现 execution_outcome.py（纯标准库、不可变标量、版本与组合校验）、outcome_adapters.py（错误/领域边界转换）。AgentLoop.run_execution 暴露统一摘要与内部领域结果；目标、native 对话与 legacy 对话的控制分支消费统一 status。领域正文不进入摘要。

ToolResult 新增可选 effect；已确认业务拒绝用 not_started，未知写入失败进入既有 RECONCILIATION_REQUIRED。异常、取消、非法返回和响应丢失均保留持久化 claim，重建 registry 后不可自动重发。目标未知内部错误不再误报模型非法操作。工具结果不确定时 native 对话立即停止。

已运行：
- 首批契约/AgentLoop/目标/events：46 passed。
- 结果、安全、审批恢复：63 passed。
- 修复明确未执行语义后，目标恢复/安全/审批：26 passed。
- 最新结果/native 对话：46 passed（outputs/harness-outcomes-final.log）。
- legacy 对话/身份/审批：37 passed（outputs/harness-legacy.log）。

## T05—T06：事件与存储

EventMetadata 是外壳的不可变元数据；外壳读取由现有 event 行字段组合，避免重复存储 seq/type/payload。events/thread_events 各新增 envelope_json；SQLite migration 48、PostgreSQL head 20261009_0026。历史行保持 NULL。

run 流使用 PostgreSQL 事务 advisory lock 后分配序号；thread 流锁 thread 行（与业务更新一致的锁顺序）；现有唯一约束保留。event_id 同内容幂等复用，异内容报错，服务端生成时间不作为重试冲突依据。

运行状态/事件、目标审批决定/事件、检查点/事件、native 目标审批创建/检查点/请求事件使用共享事务。

已运行 PostgreSQL 并发、幂等、回滚与真实目标审批集成：6 passed；新增 thread 并发与审批 span 断言：3 passed。故障注入验证审批与状态回滚通过。最新目标/事务测试：15 passed（outputs/harness-atomic-final.log）。

## T07—T08：主链路与读取

事件从持久化上下文读取，校验 digest 和绑定；不生成替代根身份。模型 attempt、工具结果、对话循环结束、研究 queued/lifecycle、专家目标引用接入。审批恢复元数据使用同 trace 下子 span，旧工具身份和旧事件 payload 中逻辑 span 均不改写。

重要兼容边界：会话审批创建的 continuation turn 按已有语义保留独立回合根；被恢复工具保持原 trace，并以审批事件串联。研究仅接通既有线程生命周期事件，内部研究引擎不改写。损坏身份的拒绝记录仍落入既有工具错误字段，不伪造成功执行轨迹。

GET /api/threads/{id}/events?envelope=true 为可选授权脱敏读取；默认 REST/SSE/JSONL 形状不变。未知版本和缺失 context 返回明确只读兼容标记。转录 canonical/hash 未修改。

已运行：事件/审批/移交 23 passed；契约/工具/目标/审批集中回归 89 passed；API 兼容及隔离 34 passed（outputs/harness-api.log）。

## T09：进行中

首轮全量 outputs/harness-contracts-all.log 在开发期间运行，含已修复失败，不能作为最终通过依据。完整最终门禁尚未完成。未调用付费模型，未提交。


### 完整回归发现及修复

首轮：8 failed, 2313 passed, 7 skipped，耗时 21:57。涉及：目标工具明确拒绝缺少 not_started；MCP 定义漂移缺少 not_started；旧审批等待事件无 call_id；迁移链断言固定旧 head。均已针对性修复，不削弱行为断言。

补充验证：PostgreSQL 目标恢复/事件并发 10 passed（harness-pg-final.log）；迁移历史链 1 passed；MCP 全套 54 passed（harness-mcp-final.log）；原 V2 审批回归通过。最终干净门禁运行中：outputs/harness-contracts-clean-all.log。

### 使用与部署

- 生产数据库先在 backend 执行 python -m alembic upgrade head，目标版本 20261009_0026。仅增加两列，不回写历史数据。应用会拒绝未升级的 PostgreSQL schema。
- ExecutionOutcome.to_dict/from_dict 是结果摘要序列化入口；LoopExecution.value 只供进程内领域适配，不可写入公开事件。
- EventMetadata 存在原事件行的 envelope_json。新消费者用 project_envelope（明确 owner）或已授权领域入口；恢复仍从业务记录和 HarnessContextStore 读取，不能从公开投影恢复执行。
- 会话事件 API 增加可选 envelope=true；默认 REST/SSE/JSONL 保持旧格式。公开投影脱敏，历史缺失/未知版本返回兼容状态。当前应用仍沿用既有本地用户模式，本轮不新增外部认证系统。
- 外部写入不能保证 exactly-once；结果不确定持久化对账义务，禁止自动重发，沿用现有回执恢复机制。


### 最终已运行门禁（2026-10-10）

命令：仓库根目录 python scripts/test_all.py。日志：outputs/harness-contracts-clean-all.log。退出码 0。

- Backend：2324 passed, 7 skipped，1286.75 秒。
- Frontend：50 files、345 passed。
- TypeScript 与 Vite 生产构建通过。
- git diff --check 通过，仅行尾转换提示。
- 7 个跳过对应显式启用的 live 集成场景（4 个）与当前环境不可用的符号链接安全场景（3 个）；未为本轮新增跳过。
- 历史测试默认 legacy，native 测试显式 loop；不能将完整历史套件表述为全部 native 覆盖。

### 尚未满足的严格契约项

全量测试通过不等于 C01/C02 每条架构要求全部完成。当前交付为已接入主链路的适配迁移，以下仍需后续实施：

1. 新事件仍允许在没有持久化上下文时按旧记录写入，尚未将所有新生产者与历史兼容写入严格分开；部分目标旧路径仍没有新外壳。对话入口及问答/审批续接已在接收事务中冻结根身份，根入口事件已有外壳。
2. 模型 attempt 在夹有 cost 事件时的 span 复用、目标审批恢复的独立 attempt 已修复；对话物化目标的跨 stream 因果已关联。旧领域路径的覆盖仍需收敛。
3. 研究和专家生命周期线程事件均已关联；专家内部事件仍沿用领域存储，未声称所有内部子任务共享完整 trace。
4. 审批恢复实际工具执行及内部模型调用已接入恢复事件的新 span；原工具身份记录与摘要保持不变。
5. Error/Outcome 已用于主要边界决策，但内部 Failed.error 与领域返回值仍保留兼容对象，完整终态唯一性及所有故障 phase 的生产者覆盖未完全收敛。

T07、T09 按严格验收保持未完成；本轮没有提交或推送。上述不是测试失败，而是规范覆盖缺口，不能以绿灯掩盖。

### 继续修复（2026-10-10）

- 模型开始/结束事件越过插入的 cost 事件复用同一 attempt span；目标工具审批恢复递增 attempt 并记录显式因果。完整门禁 outputs/harness-contracts-final-trace-all.log：后端 2324 passed、7 skipped；前端 345 passed；生产构建通过。
- ConversationService 接收普通回合、问答续接和审批续接时，在原事务中持久化根上下文。失败会连同回合、消息和事件一起回滚，重复接收复用同一身份。
- ChatToolRunner 使用已持久化恢复事件的子 span 执行工具及内部模型调用，原工具 context/digest 不覆盖。旧事件 payload 保留逻辑 span。
- 专家 queued/completed 线程事件使用发起回合和同一子 span，校验 owner/thread/bundle/source_turn，完成携带 Outcome。独立专家入口仍保留既有领域身份；内部 agent_events 未重写。
- 半缺失的 context/digest 显式拒绝，不再误判为历史记录。
- 集中回归：outputs/harness-followup-focused.log，60 passed（含 PostgreSQL 上下文并发及预算等待）；入口事务/读取回归：outputs/harness-entry-atomic.log，8 passed。
- 中间全量 outputs/harness-contracts-entry-final-all.log 在旧 M3 脚本发现接收后修改 bundle 的冲突，已停止，不能作为通过证据。脚本改为接收前指定可信分配，独立 PostgreSQL 用例通过（harness-handoff-fixed.log，1 passed）。
- native 目标根在创建事务内冻结；对话生成目标继承来源 trace，run.created 的 causation 指向已提交事务内的 execution.materialized。目标/物化/真实 PostgreSQL 回归：harness-roots-final.log，22 passed。
- 已存在可信记录的操作丢失上下文时拒绝降级 legacy；历史未绑定操作仍可兼容读取/适配。
- 历史预算等待夹具改为在入口模拟无身份的旧回合，避免先产生可信事件再清空身份；原预算、等待与并发断言保留，harness-historical-budget.log：9 passed。
- complete-all 全量结束：2324 passed、4 failed、7 skipped。3 个快照测试仍断言原逻辑 span，已改为断言恢复 attempt 子 span，并保留原 trace/bundle/快照不变检查；1 个真实回归为事务提交后遗漏统计投影刷新，已修复。对应整组 tests/test_snapshot_flow.py、test_snapshot_recovery.py、test_stats.py：32 passed（harness-final-failures-fixed.log）。
- 最新完整门禁完成：outputs/harness-contracts-verified-all.log，python scripts/test_all.py 退出码 0。Backend 2328 passed、7 skipped；Frontend 50 files、345 passed；TypeScript/Vite 构建通过。跳过原因与前述一致，无新增跳过。未付费调用，未提交或推送。其余中间全量日志不作为最终通过依据。

### 后续主链路收敛（2026-10-10）

此前 T07/T09 缺口已在 `../harness-mainline/` 的 C/P/E/R 阶段实现并分阶段验证：新执行持久化身份、恢复 attempt、原子终态、研究 epoch、专家/目标跨进程执行权及实际发送前校验。研究内部事件与领域兼容返回值按原范围保留，不误称为全部内部事件重写。R 固定全量 2440 passed、7 skipped，UI 345 passed、构建通过。任务能力阶段发现并修复专家报告遗漏历史、目标计划事务边界、任务指代被旧解析器拦截；真实路由 smoke 5 步/7 次调用通过。T07/T09 最终关闭等待 mainline-a-corrected-gate 完整结果，详见主链路开发记录。

最终关闭：mainline-a-corrected-gate 全量退出码 0，后端 2465 passed / 7 skipped，前端 345 passed、构建通过。T07/T09 主链路契约缺口关闭；完整变更、迁移、真实 smoke 和范围限制见 ../harness-mainline/implementation-notes.md。未提交或推送。
