# 开发记录

状态：阶段 0、P、E、R 固定门禁通过；A 已接入，真实 smoke 与最终完整门禁通过。保留前序未提交代码。以下按时间追加，早期“未完成”记录代表当时状态。

## C00：生产调用点与缺口（2026-10-10）

状态：已完成源码盘点。

| 边界 | 现状/缺口 | 归属与验证 |
|---|---|---|
| conversation.accept_turn / 续接 | 根身份与接受事件已同事务冻结，无需重写 | 既有 test_event_envelope / 审批测试 |
| HarnessContextStore.load_or_create_turn_context | context 两列被删除后可能重新生成根身份；旧事件仍证明原身份存在 | C01：重建必须拒绝，历史未绑定记录仍区分处理 |
| event_metadata.bind_event_metadata | 显式 metadata 仅核对五个字段，尚未验证 budget/bundle/project/task 绑定 | C01：同 trace 伪造绑定也必须拒绝 |
| event_metadata._context 的 run 路径 | 未比较 source_turn_id 与 context.turn_id、root_budget_id | C01：持久化绑定漂移测试 |
| conversation worker 收尾 | 已有执行权检查；loop.finished 与最终业务状态分别提交，需审查取消/迟到竞争 | C02：生产状态与事件竞争测试 |
| goal_loop 的 loop.finished | 多轮目标步骤共用 run 操作，attempt 语义需要收敛 | C02：多步骤、审批恢复及唯一结束结果 |
| model_control.finish_attempt | 已有数据库状态条件与提交锁，复用 | C02：保留 PostgreSQL settlement 测试 |
| tools claim / unknown | 已有持久化对账及禁止重发，不能替换为 worker lease | C02/E03：保留副作用安全测试 |
| 研究/专家交接 | queued/completed 和实际目标已关联；失败/取消 Outcome 覆盖需核对 | C03：任务生命周期失败场景 |
| Failed.error / LoopExecution.value | 进程内兼容对象，公共序列化只写 outcome.to_dict | 保留；不为统一命名新增封装 |

先完成 C01—C04，再进入 PolicyEngine；暂不增加无生产消费者的新接口。

## 记录规则

每完成一个任务追加如下内容，不将计划当作实现记录：

### 任务编号：标题

- 状态：未开始 / 进行中 / 已完成。
- 修改：实际文件及入口。
- 决策：选择、依据、职责边界。
- 验证：命令、环境、实际结果；未运行明确标记。
- 兼容与迁移：旧调用方、在途数据、部署步骤、回退限制。
- 剩余项：具体问题及归属任务；有必需缺口不得标记完成。

## 阶段门禁

| 阶段 | 状态 | 完整测试日志 | 未关闭必需项 |
|---|---|---|---|
| 0 契约收尾 | 通过 | mainline-stage0-clock-fixed-gate.log | 无 |
| 4 PolicyEngine | 通过 | mainline-policy-corrected-gate.log | 无 |
| 5 Capability | 通过 | mainline-e-corrected-gate.log | 无 |
| 6 TaskRuntime | 通过 | mainline-r-fixed-gate.log / mainline-a-corrected-gate.log | 无 |
| 7 工具化 | 通过 | mainline-a-corrected-gate.log | 无 |

## C01：身份绑定加固（进行中）

- 显式事件 metadata 只允许 span/parent_span 与可信上下文不同，其余业务身份全部比较；目标 source_turn 与 root_budget 也核对持久化行。
- 有可信历史事件的回合，根 context 两列删除后禁止重新生成身份。
- 手动/定时/重试研究锚点回合在创建事务内冻结根，复用 HarnessContextStore，无新存储层。
- 历史存储测试改为入口构造真正未绑定的旧回合；专家测试在接受前激活 bundle，不清空已冻结身份。
- mainline-c01-boundaries.log：69 passed（事件、研究、专家、目标及 PostgreSQL 上下文）；mainline-c01-identity.log：13 passed。先前 mainline-c01.log 的单个失败是拒绝原因文案发生变化，已保留原 source 错误并拆出 budget 错误。
- 本阶段尚未全量回归、未提交；历史恢复与其余生产入口仍需核对，不能据此关闭整个 C01。

## C02：执行尝试（进行中）

- 目标 loop 以 run/plan/step 标识逻辑操作；实际开始持久化 loop.started，恢复递增 attempt 并派生子 span。
- loop.finished 复用开始事件身份，以稳定 event_id 关联本次结束，审批前后结果不再都记作 attempt=1。
- mainline-c02-attempts.log：10 passed，包含目标审批恢复与 PostgreSQL 目标入口。

### C02：并发提交与事务收敛（继续开发）

- 研究 claim_next 使用 PostgreSQL 行锁及 SKIP LOCKED；完成、取消的状态检查持有任务行锁。mainline-c02-final.log：72 passed，覆盖研究竞争、身份事件、目标与运行时。
- 专家任务使用 run → task 的一致加锁顺序，领取刷新状态，完成/取消共用 run 锁；事件序号改为原子 UPDATE RETURNING。父任务 join 只在状态更新成功后产生事件，避免多个子任务重复结束父任务。
- mainline-c02-experts.log：26 passed，包含真实 PostgreSQL 完成/完成、完成/取消、双 worker 领取，以及原有任务与 worker 测试。
- 对话 native loop 在开始时记录 worker epoch 和子 span，结束事件移到回答、等待、取消、失败、研究/专家移交的状态事务中。稳定结束 event_id 绑定开始事件；取消沿用 unknown 副作用的 reconciliation_ref。
- mainline-c02-conversation.log：77 passed。新增事务回滚/取消测试最初有测试接入错误（读取序列化 context 层级错误、patch 到 worker pool 而非 primary worker）；修正后 mainline-c02-atomic-final.log：46 passed（对话 loop、runtime、goal loop）。
- 目标步骤完成、等待外部结果、阻塞和 loop.finished 合并事务；get_run、plan.get、mark_step_completed、_transition 复用调用方连接。取消的状态/事件/检查点合并事务；检查点从同一连接读取当前状态和事件游标。
- 最新目标事务回归 mainline-c02-goal-atomic.log：53 passed，包含步骤事务中断回滚、检查点同事务读取及 PostgreSQL 竞争。此批之后仍需审查进程中断恢复、执行结束竞争和 C01 历史适配边界；C02 未标记完成。
- 本次未增加表、依赖或第二套 worker；未付费调用、未提交。尚未运行本阶段全量门禁。

### C01/C02：恢复与幂等补充

- 对话 worker 和文档工具只读取已冻结的 turn context；目标 native loop 缺失根 context 时拒绝执行，不再运行时静默创建。历史读取保持兼容，历史执行恢复需显式迁移。
- mainline-c01-c02-current.log：95 passed（对话 loop/worker、目标 loop、runtime、统计），包括旧回合缺身份时零发送、零身份回写。
- 研究及专家相同完成结果重交复用持久化结果，冲突结果拒绝；专家额外核对原 lease epoch/owner。研究现有完成接口只有 owner，epoch fencing 仍属于 R 阶段必需项。
- mainline-c02-idempotency.log：37 passed（含 PostgreSQL 双领取及完成竞争）。此前记录的重复完成拒绝语义已被本项的相同结果幂等复用替代。
- 对话租约接管与目标恢复先结束中断 attempt，再创建新 span/attempt；unknown 工具保留 reconciliation_ref。mainline-c02-recovery.log：20 passed；mainline-c02-goal-recovery.log：11 passed，包含真实 PostgreSQL 目标审批。
- 目标最终 run.completed 与 COMPLETED 状态同事务，取消不能被迟到完成覆盖。统计投影在提交后刷新。mainline-c02-projections.log：47 passed；mainline-c02-terminal-final.log：40 passed；mainline-c02-terminal-pg.log：7 passed，新增目标完成/取消竞争检查状态、事件及检查点一致。
- _transition 在同事务重新读取持久化状态并校验转换，防止使用旧 RunSnapshot 覆盖另一进程已取消的状态。mainline-c02-state-fencing.log：41 passed。
- 模型失败导致目标 FAILED 的状态、事件和检查点也已合并事务；mainline-c02-consolidated.log：88 passed。
- 尝试将目标 legacy 入口也统一冻结身份时，mainline-c01-entry-complete.log 出现 7 failed、46 passed：旧预算恢复与反思夹具仍依赖创建后替换来源/预算。该扩展已撤回，native 根冻结及模型调用继承上下文保留；mainline-c01-entry-compatible.log：53 passed。旧入口迁移仍是 C01 缺口，不能跳过。
- 仍处阶段 0；C03/C04 和后续阶段尚未完成，不以聚焦测试替代全量门禁。

### C01：研究/专家实际模型调用身份

- 研究 worker 执行前读取来源持久化 context；模型调用继承 trace，run_id 绑定研究 job，bundle 继续使用原演进/学习服务解析结果，未修改其选择规则。
- mainline-c01-research-context.log：40 passed；mainline-c01-model-lineage.log：25 passed，新增实际模型发送读取持久化 invocation 的继承验证。
- 对话移交的专家 execute/synthesize/既有安全检查调用统一经最小 _model_context 适配，继承发起回合 trace，附带当前 child/coordinator task 标识；独立 advisory 保留原领域身份，后续 R04 明确迁移。
- mainline-c01-expert-context.log：27 passed；新增对话专家实际模型 trace 测试 mainline-c01-expert-lineage.log：3 passed。首轮测试漏传 activate 的幂等键已修正。

### C01：新旧入口明确分开（开发中）

- 新目标入口通过 _persist_run_root 在创建事务中冻结身份，覆盖 legacy/loop 两种执行算法；物化目标始终继承来源根。
- 将确实验证旧预算/旧反思行为的测试改为入口构造历史未绑定记录（historical_run_identity fixture），不清空已有身份或回写旧事件；保留其业务断言。此前撤回的扩展以此方式重新实施。
- mainline-c01-legacy-migration.log：65 passed（runtime/checkpoint/API/materializer/goal/event 全组）。
- 真实目标网关禁止缺根身份执行，并逐字段核对当前 run 绑定；没有 evolution 的目标也在创建事务固定 stable bundle，避免网关晚绑定冲突。
- mainline-c01-goal-dispatch.log 最初 4 failed、15 passed：3 个暴露创建时未固定 bundle，1 个旧测试等待更晚的 PermissionError。修复后剩余 HTTP 夹具创建后改 source 的冲突，改为入口事务绑定后，mainline-c01-goal-dispatch-final.log：18 passed；对应 PostgreSQL native 目标测试在 dispatch-fixed 批已通过。
- C01 尚需收敛 standalone advisory 与历史执行迁移；C02 剩余在途双进程目标执行权及研究 epoch 在 R 阶段明确处理。未运行阶段完整门禁，未提交。

## C03：生命周期映射（开发中）

- 研究 cancelled/partial/retry_scheduled、目标 run.cancelled/run.failed 及专家 failed/cancelled 补齐 Outcome；专家失败和取消在原业务事务发回来源线程。
- 部分报告不改原 PARTIAL 状态或 payload；Outcome 为 task.failed + TASK_INCOMPLETE。研究重试为 loop.failed + retryable，业务 job 仍 QUEUED。专家 incomplete 同样不误报任务成功。
- 模型旧命名 model.attempt_finished 映射与网关 model.attempt.finished 一致；取消保留 cancelled 语义。未知错误保守 INTERNAL_ERROR，不解析文案。
- mainline-c03-outcomes.log：78 passed；mainline-c03-lifecycle.log：19 passed，新增来源 trace、取消、部分结果与重试 scope 验证。
- 待继续核对已知错误码/phase、历史恢复边界并运行阶段全量门禁。未声明 C03 完成。



### C01—C03：继续收敛（2026-10-10）

- 模型前置校验、快照准备纳入 `_model_call` 的 finally 范围；失败会清理 active task 并恢复网关 context。新增零发送和外层 context 恢复测试。
- `event_envelope.py` 仅保留纯契约；授权读取/脱敏投影移到有真实 API 消费者的 `event_projection.py`。
- 对话复用已有 ask/研究/专家任务时补齐本次 loop.finished；有活动 attempt 的移交复用仍检查 worker 执行权。
- 对话/目标已知异常直接从现有 exception_outcome 映射后持久化，不在任务事件层解析异常字符串；unknown 写入保留真实 tool call 引用。ToolRegistry 抛出的对账异常统一携带 call.id。
- 目标审批 request、checkpoint、loop.finished 同事务；取消先于审批提交时不创建审批。新增结束事件失败时全部回滚测试。
- 研究 started 按实际 attempt 派生新 span，后续 retry/complete 继承该 attempt；不改变研究内部算法。
- 已通过：mainline-c01-c03-cleanup.log 66 passed；mainline-handoff-reuse.log 2 passed；mainline-contracts-current.log 77 passed（含 PostgreSQL native 目标）；mainline-research-attempt-trace.log 1 passed。
- 中间失败：错误保留测试误将 operation_ref 当位置参数，审批回滚测试误假设原先存在 checkpoint，均修正后在 77 项中通过。
- 专家根身份正在迁移：创建事务复用 append-only 的 agent.run.created 存储服务端 context，无新表；会话来源继承 turn，独立专家在入口冻结根。历史无根调用拒绝；原上下文快照 hash 不变。
- mainline-expert-persisted-root.log 初轮 4 failed / 57 passed：worker 的 run 投影没有 id，修为读取 task.agent_run_id，正在复测；尚未据此声明完成。
- 全量 mainline-stage0-integration.log 仍运行。期间有代码修改，仅作排查记录，不能作为当前最终固定版本门禁。阶段 0 保持进行中；后续 P/E/R/A 未开始。


### C01—C03：最新聚焦回归

- 专家固定根修复后：mainline-expert-trace-current.log 18 passed；mainline-expert-final.log 19 passed；mainline-expert-history-postgres.log 11 passed，包含历史无根零发送及 PostgreSQL 完成/取消竞争。
- 对话计划物化及既有安全调用继承持久化根。物化首轮 3 failed / 57 passed 暴露局部 import 遮蔽与错误 store 所属，修复后 mainline-projection-context-fixed.log 22 passed。
- 研究任务层直接接收 worker 映射的类型化 Outcome；预算等已知模型失败保持 code/phase，不输出原始异常。mainline-research-error-current.log 59 passed。
- legacy 目标完成步骤/等待结果的状态、事件、checkpoint 同事务；提交时重新核对取消。mainline-legacy-terminal-atomic.log 34 passed。
- 专家根冻结仅在创建发生：新会话专家继承来源 turn，独立 advisory 在服务端入口生成；现有 append-only 创建事件保存 context；业务快照和其摘要没有改变。不自动补写旧记录，无根历史可读但真实模型执行拒绝。
- event_projection 的所有仓库生产/测试引用已迁移，无第二份实现。已有安全调用仅迁移身份，不新增评分工作或付费调用。
- 待关闭：全量失败详情与固定版本重跑；目标跨进程执行权/研究同 owner 接管 fencing 按 R 阶段落实，阶段 0 必须明确已有边界，不能提前声称全链执行权完成。


- 最新收束验证 mainline-contract-final-focused.log：36 passed（事件/专家/unknown 工具）。git diff --check 无错误。
- 旧模型命名 model.attempt_started / model.attempt_finished 与新命名复用同一 attempt span；不增加模型发送。
- 阶段 0 全量排查继续，当前已有失败，尚不能关闭 C04。未运行本轮前端门禁（test_all 在后端成功后才运行前端）。


### 阶段 0 全量排查结果

- `python scripts/test_all.py`：后端 **2 failed, 2352 passed, 7 skipped**，1326.34 秒。日志 mainline-stage0-integration.log。期间修改代码，属于排查批，前端未执行。
- 失败均为旧夹具创建后改变已冻结绑定：M5 unknown-bundle observer 改为创建历史无根记录；memory conversation 反思来源改为创建事务内绑定，不清空或重写已有事件。
- mainline-integration-fixes.log：上述两组 **11 passed**。mainline-c03-final.log：研究/事件 **59 passed**。
- 当前生产代码收束，开始固定版本完整门禁；完成之前不标记 C04。


## 后续 PolicyEngine 接入盘点（只读设计，尚未切换生产路径）

| 当前权威 | 实际职责 | 后续处理 |
|---|---|---|
| tools.ToolRegistry.authorize | 已注册能力、skill allowlist、参数/schema/path、写审批 | 权限决定迁移 P01/P03；参数与路径验证保留执行适配 |
| chat_tools.ChatToolRunner.allowed_names/execute/resume | 回合允许工具、owner/原身份读取、审批恢复绑定 | P02/P03 消费统一决定；持久化记录和业务文案保持 |
| ApprovalService.require_granted | 参数摘要、binding digest、审批状态与到期 | 仍是审批事实权威；PolicyEngine 不新增审批存储 |
| McpToolAdapter.ensure_current/_stale_approval | 服务可用、定义变化、当前配置与原批准版本 | MCP 特有事实读取保留；通用 allow/deny 判定统一 |
| goal_tools 领域 owner 查询 | program/action/document 的所有权与业务状态 | 保留资源查询；模型参数不进入可信身份字段 |
| ModelControlStore.assert_request_active | 已绑定 invocation 的来源资产与固定 prompt 有效性；两种网关共用 | P04 扩展事实/统一决定，每次 retry 刷新 |
| ModelControlStore.start_attempt / costs | 实际发送前原子预算预留 | 保留原子预留；策略 allow 不代表预留成功 |
| DurableQueue / research / agents | worker owner/epoch/deadline/cancel | P04 只消费事实；统一执行权属于 R，不在 PolicyEngine 内写租约 |
| tool_execution_claims | 写入执行意图、unknown 与对账 | 保留，allow 不能覆盖 unknown；E03 搬迁实现而非复制表 |

最小实现顺序：先注册范围/审批纯规则和真实 ToolRegistry 消费者，再接 ChatToolRunner 的身份事实与 MCP 当前定义，最后两个网关的逐次发送检查。不得为每个领域写一个只改名的 PolicyEngine，也不得把 provider 预算预留变成布尔预检。


## TaskRuntime 范围预核对（R01 尚未开始实现）

| 机制 | 当前存储权威 | 类型/不能替代的职责 | 已确认缺口与归属 |
|---|---|---|---|
| 对话 DurableQueue | turn_jobs | worker owner/epoch/deadline、每线程排他、全局容量 | R05 复用表与现有领取；统一 finish 要携带业务事务 |
| 研究服务 | research_jobs + research_job_attempts | worker owner/deadline、attempt 与恢复章节 | R03 补独立 epoch；所有 mutate/heartbeat/finish 必须传 token，不能仅在最终 complete 校验 |
| 专家服务 | agent_tasks + agent_task_attempts | worker epoch/deadline，coordinator/child 独立执行权 | R04 保留 fan-out/join；统一 token 校验和事务提交，根身份已在创建冻结 |
| 目标执行 | runs + 本进程 asyncio.Lock | 同步 API 驱动的执行串行化 | R06 明确跨进程执行权；取消不应等待整次模型调用的锁 |
| 计划物化 projection claim | turns.direction_projection_* | 编译准备的业务领取/幂等来源绑定 | R06 迁移清单必须单列；保留 READY 草稿恢复，不重复编译 |
| 工具 claim | tool_execution_claims | 写入意图与 unknown 对账 | 不合入 worker lease；过期任务令牌不能授权重发工具 |
| 成本预留 | 现有 costs/task_budget_roots | 金额/次数/时限的原子准入 | 不合并为任务队列状态 |
| Learning/evaluation/embedding 等后台 | 各自现有表 | 本轮主链路外的后台任务 | 明确延后，不能宣称全部后台统一 |

生产迁移顺序仍为研究 → 专家 → 对话 → 目标/在途收尾。选择存储时优先提取现有租约算法到最小内核，保留领域表；若决定新增表，必须先给出不能复用现有存储的证据和单一调度切换步骤。此表是源码盘点，非 R 阶段完成声明。

- 独立前端检查 mainline-stage0-frontend.log：50 files / **345 passed**。完整 test_all 固定版本门禁仍在运行。


### 固定门禁期间只读审查的待办

- conversation native `Status.EXHAUSTED` 仍抛 RuntimeError 后重新映射为 INTERNAL_ERROR；应让 `_finish_failure` 接受本轮已生成 Outcome，持久化 task/loop 各自 scope。这是 C03 必需修复，当前门禁结束后实施并补实际循环耗尽测试，不解析 RuntimeError 文案。
- 工具参数/schema 拒绝仍与权限拒绝共用 ToolRejected；C03/P01 需明确类型化参数错误边界，不靠中文文案区分。旧返回文案保留。
- HarnessContextStore.load_turn_context 有重复的局部 nullcontext import，可在下一次生产变更收束时移除。
- R06 前的目标本地锁不构成跨进程执行权，不能将恢复扫描中关闭旧 attempt 的行为声称为多 worker 正确；该限制必须由任务执行权迁移关闭。


### 任务能力接入预核对（A 阶段未开始）

- 已有模型能力：start_research、delegate_experts；当前注册为 READ 以沿用明确委托启动的现有行为，不能因拆分 Registry 顺手改变审批体验。
- 现有用户可见报告/专家消息已由领域服务在完成事务内写入，回传继续复用；A04 不创建第二个消息投递器。
- 尚缺模型可调用的任务查询/取消；A01 可采用统一 `get_task` / `cancel_task` 加受限 kind 和 ref（最终以实现时锁定为准）。任务引用必须查持久化 owner，不能仅凭 ID 前缀或 thread 相同授权。
- ResearchService 的 get/cancel 本身尚不带 owner 参数，现有研究 HTTP 端点也未像专家端点显式传 owner；P05/A03 接入要补到服务查询边界，避免只在新工具层保护。专家端点已有 owner 检查可复用。
- 对话 handoff 成功已经结束当前 turn；查询/取消属于后续新回合，不恢复旧 loop，不自动循环模型轮询。

### C03：参数错误与专家失败根因收尾

- 修正 goal_capabilities 的 ToolArgumentError 导入；schema、对话/目标原生参数和目标业务参数验证统一抛该子类。权限拒绝保持 ToolRejected，公开 Outcome 区分 TOOL_INVALID_ARGUMENT 与 TOOL_AUTHORIZATION_DENIED，不解析异常文案。
- mainline-argument-outcomes.log：58 passed。mainline-exhaustion-current.log：41 passed，循环耗尽保留 EXHAUSTED，不再重新分类为 INTERNAL_ERROR。
- 专家 worker 将真实异常适配为 ExecutionError，经原 fail 事务写入 attempt 和失败事件；任务结束时传给来源线程。未知异常仍为 INTERNAL_ERROR，领域旧 code 保留兼容。重试事件使用 loop scope，任务终止才用 task scope。无新增表。
- mainline-expert-error-outcome.log：48 passed（含 PostgreSQL 竞争）；mainline-expert-worker-error.log：1 passed / 12 deselected，真实 worker 失败路径保留 MODEL_BUDGET 且不持久化原始供应商错误。
- mainline-stage0-fixed-gate.log 仍在运行，但启动后有上述生产修改，因此仅作整合排查，不能作为固定最终版本门禁。C04 和后续阶段保持未完成。
- ChatToolRunner 前置参数拒绝与权限拒绝分开返回；已知 ToolResult 错误映射稳定 code，但不覆盖 write unknown 对账。mainline-tool-errors-current.log：71 passed；mainline-chat-rejections.log：9 passed。新增 chat 测试初轮缺少构造器参数，补齐后通过。
- `_block` 先写 run.blocked 再保存 checkpoint，使恢复游标包含阻塞原因事件；二者仍在同一事务。mainline-checkpoint-cursor.log：39 passed。
- 生产修改收束后启动 `python scripts/test_all.py > outputs/mainline-stage0-final-gate.log`；结果待完成。此前 fixed-gate 继续作为整合排查记录，不能代替本次门禁。
- 随后的审批恢复审查发现撤权/绑定拒绝缺少 effect，导致已确认零执行的写调用被报告为 unknown；已显式标记 NOT_STARTED，并验证持久化事件为授权失败。mainline-approval-rejection-outcome.log：36 passed。final-gate 启动后因此有代码修复，该批也降为排查记录，最终固定版本门禁仍须重跑。
- mainline-stage0-fixed-gate.log 后端结束：2364 passed / 7 skipped，1335.26 秒；前端进行中。该日志仍属于运行期间有修改的排查批。
- 最新固定代码启动完整门禁：mainline-stage0-frozen-gate.log，命令 `python scripts/test_all.py`。等待结果期间不再修改生产和测试代码。

### 后续阶段的接口约束补充（设计盘点，未实施）

- P03 保留 ApprovalService 对参数 hash、binding digest、有效期的权威读取；纯规则只消费验证结果，不能复制审批 SQL 或接受模型提供的布尔授权。
- P04 需要在每次模型 attempt 的调用方回调之后读取取消/执行权。现有 direct gateway 只在循环外检查 cancel_event，流式读取期间的取消不能替代发送前拒绝；沿用现有 assert_request_active 插入点。
- R02 优先提取 DurableQueue 已存在的 LeaseToken/LeaseLost 语义，避免再建同名不兼容类型；对话的每线程排他和全局容量仍是该队列独有的准入条件。
- R03 研究的 attempts 用于失败重试上限，不能直接替代执行权 epoch；所有章节/进度/心跳/终态写入要一起迁移，不能只拦最终 complete。
- C02 当前只证明业务提交竞争与原有 owner/epoch 边界。跨进程目标执行与研究同 owner 接管仍未满足全链执行权，不能在 R07 前声称这些场景完成。

### 门禁进度更新

- mainline-stage0-fixed-gate.log 已完整结束：后端 2364 passed / 7 skipped；前端 50 files / 345 passed；构建通过。仍属非固定版本排查结果。
- mainline-stage0-frozen-gate.log 早期出现两个失败，对应 late_ask / concurrent_answers。未完成时尚无 traceback，不能判断根因。单独复现命令 `python -m pytest -q backend/tests/integration/test_budget_waiting_and_operations.py -k 'late_ask or concurrent_answers' -x`：2 passed / 7 deselected（mainline-gate-early-failure.log）。等待完整失败详情，不通过放宽断言掩盖。
- 连同之前的 PostgreSQL 用例按相同顺序复现（a17、native goal、attempt settlement、budget waiting）：mainline-gate-prefix.log 13 passed。旧 final-gate 已停止并清理其独立测试容器，避免与 frozen-gate 争用资源；未动开发数据库或其他容器。
- 错误分类继续盘点发现 legacy runtime 仍按 `str(exc).startswith` 判断参数错误，现已改为现有 error_from_exception；MCP jsonschema ValidationError 使用同时兼容 McpToolRejected 的参数错误子类，未知 schema/资源授权仍保留拒绝语义。mainline-error-boundaries-final.log 正在跑。已有失败的 frozen-gate 因本次修复也只作为整合诊断，未声明最终通过。
- mainline-error-boundaries-final.log 已结束：71 passed，79.36 秒。接下来保持代码不变，等待全量失败详情。
- frozen-gate 结束：2 failed / 2372 passed / 7 skipped，1364.56 秒。两项失败是预算剩余 420.011032/420.012124 秒超出 420 秒；恢复使用应用时间、读取使用数据库时间，存在时钟偏差。改为恢复及 PostgreSQL ask 创建使用数据库 clock_timestamp，测试等待锚点也使用同一时钟；保留 420 秒上限，不放宽断言。mainline-budget-clock.log：9 passed。补 worker 时钟快一小时的回归，正在验证。
- mainline-budget-clock-complete.log：31 passed，含 worker 时钟快一小时仍保留 420 秒上限；审批恢复的另一处调用同步迁移，PostgreSQL tool call 的等待锚点改用数据库时间。mainline-approval-clock.log：18 passed（含真实 PostgreSQL 审批写入）。
- 当前启动 `python scripts/test_all.py > outputs/mainline-stage0-clock-fixed-gate.log`，代码保持不变，结果待定。
- 固定版本完整门禁已结束，退出码 0：backend **2376 passed / 7 skipped**（1334.48 秒）；frontend **50 files / 345 passed**；TypeScript/Vite 构建通过。7 个 skip 为 4 个需显式启用的 live 场景及 3 个当前环境不可用的符号链接用例，无新增 skip。历史套件默认 legacy，native 用例显式 loop。

## 阶段 0 收尾范围与后续依赖

- 身份、结果、事件的主链路适配已接入并通过上述固定门禁；内部 Failed.error 保留进程内兼容，研究/专家内部领域事件不机械重写。Outcome 与 EventMetadata 均固定 schema_version=1，历史缺外壳只读兼容，未知版本不可执行。
- C02 的数据库业务终态竞争、事务回滚、同结果重交及未知写入对账已验证。执行权统一的跨进程目标排他、研究同 owner 接管属于 R02/R03/R06，仍未实现；前序 T07/T09 的严格全链完成声明继续保留，直到 R07 验证后关闭。
- 原文“C04 关闭所有前序项才能 P01”与 R 阶段负责执行权迁移形成循环依赖。实现顺序明确为：阶段 0 关闭已有执行权边界内的契约与门禁 → P/E → R 补执行权 → R07 回验 C02/T07/T09。不是豁免验收，最终 A06 必须关闭这些必需项。

## P01：最小权限决策（已接入）

- 新增纯 policy_engine.py：不可变 PolicyInput/PolicyDecision，allow/deny/require_approval 与稳定原因。只接入已注册、允许范围、身份参数、写审批事实，无 DB/工具反向依赖。
- tools.ToolRegistry.authorize 为真实消费者；参数/path 验证保留在执行边界，审批 hash/binding/有效期仍由 ApprovalService 验证。拒绝优先于审批，原异常文案兼容。
- mainline-p01-policy.log：50 passed（policy、tools、claim、unknown 副作用、goal tools）。P02 的持久化身份与 P04 来源/预算事实尚未接入，不能据此声称全链权限统一。

## P02：对话只读能力（已接入）

- ChatToolRunner 执行前重新读取来源 turn 根身份及未删除线程，验证 owner/thread/project/bundle/budget；传入 span 只允许执行嵌套变化，不能伪造 trace 或业务绑定。PolicyEngine 消费验证结果，拒绝时零 handler 调用。
- mainline-p02-identity.log：5 passed；mainline-p02-read.log：55 passed。覆盖合法一次调用、跨 owner、删除线程、删除 context、伪造 trace，对话/目标工具及真实 worker 回归。
- P03 进行中：将相同检查移动到审批创建之前，读写共享检查；无调用方的 tool_harness 已删除。审批恢复仍沿用原记录与 claim。
- P03 首批 mainline-p03-approval.log：51 passed。恢复时 owner、当前能力可用范围、未删除来源线程接入同一 decide；mainline-p03-resume.log：37 passed（MCP、审批重启、PostgreSQL 写入）。原记录绑定摘要、审批参数、能力定义漂移与 unknown claim 保留原权威校验。

## P04：模型实际发送前检查（进行中）

- direct/routed gateway 每次 attempt 在调用方回调之后通过统一规则判断取消；取消不预留新 attempt，也不发送，不进入模型重试。
- mainline-p04-cancel.log：13 passed，包含两种网关首次发送前取消及第一次失败后取消，检查实际发送数、attempt 行数和 invocation CANCELLED。
- 来源依赖/预算/租约事实统一仍待实施；原 assert_request_active 与原子成本预留保留。mainline-policy-gateways.log 正在跑，不将 P04 标记完成。

### P03/P04/P05 持续实施

- mainline-policy-gateways.log：96 passed；mainline-p04-source.log：52 passed。来源拒绝统一消费验证事实，保留 LearningConflict/EvolutionGateError 原异常；未知基础设施错误仍直接拒绝，不重试修复来源。
- 审批恢复先用原 turn 关联的 owner/thread 查询当前数据库记录，再允许返回缓存结果；外部传入的过期 snapshot 不再决定状态。未授权请求不能读取结果或修改他人的审批记录。恢复重新验证原 turn 根身份。
- 原生 ToolSpec 增加实际消费的 version（默认 1）与定义摘要；新对话审批绑定 schema/risk/path/身份参数约束/version。恢复发现摘要变化或旧审批缺少摘要时，要求重新发起；MCP 保留原定义/config 绑定。仅 handler 语义改变必须显式升级 version。不对历史审批自动补授权。
- mainline-p03-resume-scope-fixed.log：49 passed；mainline-p03-definition.log：89 passed（含 MCP）。删除 execute/execute_async 内紧邻 authorize 的重复审批读取；claim 和对账路径仍是原实现。
- 成本预留在原事务行锁内读取额度事实并调用 PolicyEngine；未增加独立预检或重复预留。mainline-p04-budget.log：71 passed，含 PostgreSQL 根预算。
- 研究服务增加可选 owner 范围用于用户调用；内部 worker 仍由租约校验。研究 HTTP 查询/报告/来源/列表/取消/重试/删除及创建接入现有 owner dependency。mainline-p05-research-policy.log：36 passed；mainline-p05-owner-api-fixed.log：13 passed。首次新增 API 测试漏 Content-Type 得到 415，补正确请求头后通过，未放宽生产授权。
- 新 send_authority 是进程内任务范围的发送校验桥接，只有现有 worker 安装，模型不可提供。对话、专家和研究在实际发送前重新执行现有 DB 执行权校验；direct/routed 每次发送和重试均经过，失败后重置 ContextVar。它不创建任务表、不领取任务，也不替代 TaskRuntime。研究 async generator 只在推进下一事件时绑定，不能将 guard 泄露给调用方。
- mainline-p04-worker-authority.log：36 passed；mainline-p04-worker-regression.log：58 passed；mainline-p04-authority-send.log：11 passed；mainline-p04-send-final.log：60 passed，含真实研究 worker 取消/不同 owner 接管零发送。
- 仍开放：研究同 owner 的 epoch 防护、目标跨进程执行权、检查与远端发送间非原子窗口、完整 P05 门禁。前两项由 R02/R03/R06 实现，R07 必须回验 P04/C02/T07/T09。不能将上述聚焦结果当成整个开发完成。
- mainline-policy-final-focus.log：91 passed；mainline-policy-contract-final.log：13 passed。固定生产/测试代码启动 `python scripts/test_all.py`，日志 mainline-policy-fixed-gate.log，结果待定。

### E/R 迁移盘点（门禁等待期间，只读设计）

| 现有机制 | 权威数据及调用点 | 后续处理 |
|---|---|---|
| 工具写入 claim | tool_execution_claims / tools._claim_execution，unknown 由 recover_result 对账 | E03 原样搬迁；不能与 worker lease 合并 |
| 审批 | approvals / ApprovalService；对话额外 turn_tool_calls | E03 保留原绑定/ID，不新建审批表 |
| 预算预留 | task_budget_roots + cost_ledger/cost_budgets / CostService | 保持原子锁与幂等；不是任务领取 |
| 对话 | turn_jobs / DurableQueue；SQLite 兼容在 ManagedTurnWorker | R05 复用表及 token，保留线程排他和容量准入；epoch 与 attempts 分开 |
| 研究 | research_jobs + research_job_attempts / ResearchService | R03 补独立 epoch；领取、心跳、进度、章节及最终提交全部传 token |
| 专家 | agent_tasks + agent_task_attempts / AgentTaskService | R04 保留 run 先于 task 锁顺序、fan-out/join；共享租约校验 |
| 目标执行 | AgentRuntime._lock 仅进程内；模型 await 期间存在跨进程并发 | R06 必须补持久化执行权；cancel 不等待整段模型调用 |
| 计划物化 | turns.direction_projection_* / PlanMaterializer | R06 迁移执行权；现有心跳缺过期条件且无 epoch，不能漏掉 |
| 辅助后台 | Learning/归档/embedding/通知等 | 本轮不迁移；不声称全仓后台统一 |

- E 的兼容调用点：McpToolSync 读取 registry.workspace；默认笔记 handler 调 safe_path；部分故障测试 monkeypatch _record_call。拆分时改测试到真实 Executor 注入点，不能为了私有测试 API 引入动态转发框架。
- R 的在途切换必须停旧 worker 再迁 schema/启新 worker；研究原 attempts 不可充当 epoch。禁止旧新代码同时领取。保留已有业务表可避免批量复制历史结果，但所有写入方法必须使用同一租约内核，不能只提取类型。

### P05 全量门禁修复与后续草稿（2026-10-10）

- 首次 P 门禁：2422 passed、7 skipped、2 failed；未进入前端阶段。发现的是快照静态调用盘点仍引用旧 worker 方法名，以及 legacy 计划修改审批未绑定能力摘要。更新盘点符号和真实审批创建路径，聚焦回归 11 passed（mainline-policy-gate-fixes.log）。
- 修正后固定版本重跑 `python scripts/test_all.py`，日志 mainline-policy-corrected-gate.log。运行期间正式代码及测试冻结。
- E 拆分暂存于 outputs/mainline-e-draft，尚未接入生产：纯 tool_contracts/CapabilityRegistry、单一 ToolExecutor、ToolRegistry 薄兼容层；对话与目标调用方迁移到 executor。目标原生写入审批也绑定定义摘要；旧审批不自动补授权。32 项目标审批/超时对账/恢复测试通过（mainline-e-goal-definition-fixed.log）。其中恢复测试显式传递原审批摘要，仍断言重建 registry 后不得重复副作用。
- R 草稿暂存于 outputs/mainline-r-draft，尚未接入生产：研究 epoch、共享 claim/heartbeat/finish、专家适配。claim 在数据库锁内检查 available_at 和尝试上限；已删除领域 finish 后的重复租约字段写入。正补隔离 PostgreSQL 竞争与回滚测试。对话、目标、计划物化迁移尚未完成，不能将草稿算作阶段通过。

### E 接入与 R 草稿验证（2026-10-10 后续）

- P 固定版本全量通过：2424 passed、7 skipped；前端 345 passed、50 files，构建通过（mainline-policy-corrected-gate.log）。P04 的研究同名接管和目标跨进程边界仍按计划留待 R07 回验。
- E 已接入正式代码：tool_contracts.py、capability_registry.py、tool_executor.py；tools.py 保留内建能力及旧直接调用的薄兼容入口。ChatToolRunner、goal_loop、legacy goal runtime 使用同一 Executor，原审批/claim/对账表与幂等键保持原权威。
- E 首次全量：2418 passed、7 skipped、10 failed。9 个失败来自对话替换每回合 registry 后 executor 仍引用旧目录；改为直接使用当前 registry.executor。另一个失败来自旧故障注入点，迁移到 ToolExecutor.execute_async。修正回归 54 passed（mainline-e-catalog-fixed.log）；新的固定全量 mainline-e-corrected-gate.log 运行中，不标记 E05 通过。
- R 草稿：对话 worker 领取/续租/校验/结束复用内核，保留 DurableQueue 线程排他、容量准入、流式与审批恢复。研究移交结束 turn job 由对话 worker 同事务提交，不再由研究服务直接释放对话租约。草稿 76 项对话测试通过（mainline-r-turn-fixed.log）。
- 隔离 PostgreSQL 草稿：12 passed，涵盖重启、取消与提交竞争、旧 epoch 拒绝、租约领取竞争和报告/事件回滚（mainline-r-turn-postgres-fixed.log）。1 条 warning 为既有 TestClient 弃用提示。首次草稿运行遗漏 backend 默认模式 fixture，补齐后重跑，未改断言。
- 研究既有测试迁移为显式携带领取时的 epoch；研究/API/观察器/事件 57 passed（mainline-r-research-migrated-tests.log）。这些仍在 outputs 草稿，尚未完成正式 schema 迁移或目标/计划物化接入。

### R 执行权草稿收敛（未切换生产）

- 计划物化接入共享执行权：direction_projection_epoch/attempts；领取、续租、模型发送前、草案存储、失败提交均绑定本次 token。过期心跳不能复活，旧实例不能覆盖 READY。23 passed（mainline-r-projection-fixed.log），含同名接管与 READY 中断恢复。
- 目标运行在既有 runs 增加执行租约字段，业务 state 独立保留；公共执行入口由短事务领取、定时续租、结束释放，模型/工具前后与关键写入检查 token。取消保留独立短事务。28 passed（mainline-r-goal-fixed.log）；PostgreSQL 双实例互斥与过期响应拒绝 2 passed（mainline-r-goal-postgres.log）。尚需检查所有领域侧写入边界及实际发送故障矩阵，不据此关闭 R06。
- 草稿迁移 0027 复用 research_jobs/turns/runs，仅补缺失执行字段；隔离 PostgreSQL 联合调用迁移 upgrade 后 14 passed（mainline-r-combined-postgres.log）。旧 worker 必须停止后迁移；回退时运行中的研究、物化、目标会拒绝删除执行权字段。
- 研究/专家/对话过期恢复与专家即时取消进一步收敛到 TaskRuntime。首次恢复测试发现专家 epoch 递增两次，保持原兼容语义：过期释放通过状态失效，下一次 claim 才递增；对话 recover_expired 保留既有主动 epoch 失效行为。

### E05 通过与 R 正式接入

- E 修正后固定全量：2428 passed、7 skipped，前端 345 passed（50 files），构建通过。日志 mainline-e-corrected-gate.log；E01—E05 已更新。
- R 正式接入 task_runtime.py 及研究、专家、DurableQueue/对话、目标、计划物化；Alembic head 更新到 20261010_0027，SQLite 兼容在既有迁移完成后补字段。具体切换约束见 task-runtime-migration.md。
- 正式代码聚焦 86 passed（mainline-r-production-focus.log）；正式 Alembic 升级的 PostgreSQL 42 passed（mainline-r-production-postgres.log），包含迁移链、模型快照历史兼容、研究/专家/目标竞争、对话 fencing、队列容量和恢复。
- 补齐 goal_loop 开始/结束事务及审批决定事务内的执行权校验。R07 最终聚焦和完整门禁尚待通过；阶段 A 尚未接入，不标记整个任务完成。

### A 工具草稿（R 全量等待期间）

- outputs/mainline-a-draft：锁定 get_task/cancel_task，仅当前对话且同 owner 的研究/专家协调任务可访问；引用采用 kind/id，专家使用 coordinator_task_id。启动工具保留旧 job_id/run_id 并增加 task_ref，旧消费方兼容。
- 复用研究/专家取消和报告消息，查询返回业务状态，不返回 lease/epoch。新回合操作事件关联原 task_ref，trace 属于新回合，原任务根预算保持不变。
- 37 passed（mainline-a-mainline-draft.log），含启动、查询、重复取消、跨用户拒绝、完成报告去重、重启后报告进入后续模型上下文、删除线程后的拒绝。尚未正式接入，待 R07 通过。
- R 最终聚焦 34 passed（mainline-r-final-focus.log）；固定全量 mainline-r-fixed-gate.log 运行中。期间未修改正式代码。

### R07 固定门禁与 A 正式接入（2026-10-10）

- R 固定完整门禁通过：后端 2440 passed、7 skipped；前端 345 passed、50 files；构建通过。日志 `outputs/mainline-r-fixed-gate.log`。运行期间生产代码保持冻结。
- A 已正式接入 get_task/cancel_task、启动返回 TaskRef、同 owner/同线程查询及取消、新回合 trace 关联原任务。正式聚焦 56 passed（mainline-a-production-focus.log），隔离 PostgreSQL 10 passed（mainline-a-production-postgres.log）。
- 结果提交检查线程有效性与持久化模型依赖；线程删除与专家提交按 PostgreSQL 行锁串行化。来源已撤销时拒绝发布；沿用原报告/消息/终态同事务，不新增投递队列。
- 专家结果原先以 task_id 充当消息 turn_id，导致后续 canonical transcript 漏读。改为使用持久化 source_turn_id，保留独立任务引用。研究与专家均验证重复完成仅一条消息、重启后报告进入后续上下文。
- 目标计划创建/批准/修改/取消步骤补上同事务执行权校验、状态和事件提交；投影在提交后刷新。过期写入回归 3 passed；目标/计划回归 34 passed（mainline-a-goal-atomic-draft.log）。
- 真实 smoke 首次发现旧 memory reference resolver 提前拦截“刚才那个研究任务”，尚未调用模型就进入 AWAITING_INPUT。现仅在已有任务工具结果且查询指向任务时交给 AgentLoop 解析；保留服务/项目指代规则。34 passed（mainline-a-reference-fix.log）。当时启动的 A 全量已主动停止，不能作为通过记录。
- 修复后真实 smoke 完成 5 个连续步骤：普通回答、研究启动、查询、取消、专家启动。共 7 次实际调用，每次 1 attempt；成本账本 ESTIMATED_COMPLETE 合计 7802 microusd（0.007802 USD），低于 100000 microusd 上限。7 份模型输入快照、逐响应 fsync NDJSON 和每步状态已保存于 `outputs/mainline-a-live-smoke/`。失败的未付费查询保留记录，只续跑该步骤，未重复前两次调用。
- 真实 smoke 只验证实际模型的对话路由和任务服务提交；研究/专家后台模型未运行，完整生命周期与故障行为使用确定性端到端及真实 PostgreSQL 测试。没有开展 Judge/配对评测。
- 最终固定版本门禁 `outputs/mainline-a-corrected-gate.log` 运行中。未完成前不关闭 A06 或宣称整个计划完成。没有提交、推送或部署。

### 最终交付（2026-10-10 18:35）

- `python scripts/test_all.py` 固定版本退出码 0：后端 **2465 passed、7 skipped**（1482 秒），前端 **345 passed / 50 files**，TypeScript/Vite 构建通过。日志 `outputs/mainline-a-corrected-gate.log`。测试期间未修改生产代码或测试。
- 7 skips：4 项显式 opt-in 的 live/simulated-live 测试，3 项 Windows 环境无法创建符号链接。没有新增 skip；本轮真实模型验证是已记录的独立主链路 smoke，不冒充这些被跳过的测试。
- `git diff --check` 通过。代码无新增依赖；0027 迁移、部署顺序、在途处理和兼容边界见 task-runtime-migration.md。
- C/P/E/R/A 所有任务关闭；前序 T07/T09 主链路缺口同步关闭。领域内部事件、历史专家消息不作全库重写；辅助 Learning/归档/embedding/通知 worker 按既定范围保留。
- 真实 smoke 总计 7 次模型调用，账本估算 **0.007802 USD**，冻结输入与逐响应记录齐全；隔离 PostgreSQL 已停止，数据卷保留。没有执行研究/专家后台付费工作或 Judge 评测。
- 工作区代码已完成，**未 git commit、未 push、未部署**。本记录不覆盖或丢弃前序未提交修改。
