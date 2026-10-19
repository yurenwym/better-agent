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

## B01：生产入口与验证清单（2026-10-14）

- 状态：已完成源码盘点。只读，未改生产代码。
- HEAD：`eb840fbce227f5a69576afffa845000362473e6c`。其下已有未提交修改（Policy/Trace/租约/事件相关，见当时 `git status`），本清单描述的是含这些未提交文件的工作树，不是 HEAD 单独的树。
- 历史测试只作背景，不作为本轮验收：非 integration 2346 passed、3 skipped；含 Trace 专项的 integration 191 passed、4 skipped；Trace 定向 18 passed。

### 调用入口、副作用与事实来源

| 边界 | 生产入口 | 实际副作用 | 事实来源 | 统一 Policy |
| --- | --- | --- | --- | --- |
| 模型发送 | `ModelControlStore.assert_sources_active`；`RoutedModelGateway.complete`（`model_control.py`）；`ModelGateway.complete`（`model_gateway.py`） | 允许后才 `start_attempt` 并调用 provider；拒绝时关闭本次 invocation（取消或失败）。预算仍由 `start_attempt` / costs 原子预留 | subject 来自 `ModelCallContext`；来源有效性来自 `learning_assets.assert_request_active` 与 `learning.assert_pinned_prompt_active`；执行权来自 `execution_authority()` | 已调用 `model_send_request`。资源 owner/thread 仍从 subject 复制，安全事实多数默认允许 |
| 工具执行 | `ToolExecutor.authorize` / `_decide_tool`（`tool_executor.py`）；`ChatToolRunner._tool_request_decision`（`chat_tools.py`） | 拒绝返回 `ToolRejected` 或 `ToolResult`，不执行 handler。通过后才写 `turn_tool_calls` 或执行 | 注册表、skill allowlist、subject（回合 owner/thread/turn）、审批服务 `require_granted` | 已调用 `tool_execute_request`。`tool_resume_request` 无生产调用。资源身份仍从 subject 复制 |
| 任务查询/取消 | 对话工具 `task_capabilities.operate`；HTTP：`cancel_research`、`get_research_job`、`cancel_agent_run`、`get_agent_run`、`cancel_turn`、`cancel_run` | 查询只读。取消调用 `ResearchService.cancel`、`AgentTaskService.cancel_run`、对话/目标既有取消 | 工具路径读 `research_jobs`/`agent_runs` 并比对 context owner 与 thread；HTTP 使用 `owner_id` 依赖 | 仍调用旧 `decide(PolicyInput)`，未用 `task_cancel_request`。没有独立的只读 `task.query` 动作 |
| 任务领取/提交 | `TaskRuntime.claim/heartbeat/finish/require`；研究 `ResearchService.claim/claim_next`；专家 `AgentTaskService.claim_next/complete/fail`；对话 `DurableQueue`；归档 `ConversationArchiver.claim`；向量 `EmbeddingWorker.claim`；学习 `LearningService.claim/finish` | 更新各自任务行的 lease/status。失败路径不应改业务结果，但是否经统一决策尚未接入 | 任务行、lease owner/epoch、数据库时间。业务 owner 与 worker owner 目前分列在领域字段里 | `task_claim_request` / `task_commit_request` 无生产调用 |
| 资产读/写/采用 | 读：`LearningAssets.freeze` / `freeze_request` / `assert_request_active`。写与采用：`PromotionGate.promote` → `_apply` → `learning_targets.*.promote`；`complete_canary` 同样进入 `_apply` | 通过后写 revision、启用或 `learning_promotions`。质量/成本/canary 仍在 `promotion_gate` | 候选、evaluation、owner 参数、evolution 审批状态。提交时是否重读来源版本尚未统一到 Policy | `asset_read_request` / `asset_write_request` / `asset_promote_request` 无生产调用 |
| 通知发送/重试 | `NotificationService.send/retry/test/deliver_completed`；HTTP `test_channel`、`retry_notification`；研究完成时 `research/worker.py` 调用 `deliver_completed` | 先插入 `notification_deliveries` PENDING，再 HTTP POST，然后改成 SENT 或 FAILED。超时与断连也记 FAILED。test 不写 delivery 行 | channel 行没有 owner。delivery 只关联 `job_id`+`channel_id`+`attempt`。报告来自 `research_reports` | `notification_send_request` / `notification_retry_request` 无生产调用 |

### 后台 Worker

| Worker | 启动 | 状态表 | 恢复 | 事件 | owner |
| --- | --- | --- | --- | --- | --- |
| Archive | `startup.build_runtime` 在有 gateway 时构造 `ManagedArchiveWorker`；`main.lifespan` 调用 `start` | `memory_archive_jobs`、`conversation_archive_state` | `recover_expired`，轮询里失败被吞掉，不中断循环 | `_lifecycle` 只调用可选回调。生产启动没有传入 `on_lifecycle`，所以不进 EventStore / Trace | 任务行 `owner_id`，来自线程 scope |
| Embedding | 仅 PostgreSQL 且配置了 embedding key 时构造；`lifespan` 启动 | `embedding_jobs`，键为 `(revision_id, profile_id, content_hash)` | claim SQL 直接拿走过期 RUNNING，没有单独恢复事件 | 无事件写入 | worker 进程 owner（`embedding-{pid}`），不是业务 owner |
| Learning | 没有独立 worker 类。`ManagedExperienceObserver.run_once` 调用 `LearningService.run_once`；`lifespan` 启动 observer | `learning_jobs`、`learning_policies` | claim 时把过期 RUNNING 标为 QUEUED 或 UNKNOWN（已 `dispatched_at`） | pipeline 不写统一 Trace | `run_once` 默认 `local-user`，另外扫描未暂停的 `learning_policies.owner_id`。执行上下文不从任务行恢复 |
| Notification | 无调度循环。发送由研究 worker 和 HTTP 直接调用 | `notification_channels`、`notification_deliveries`（仅 PENDING/SENT/FAILED） | 无。失败即可再次 `retry`，按 attempt 插新行 | 无 | channel 无 owner；历史行不能安全归给 `local-user` |

### 待接入（不能按定义或旧测试计数）

无生产调用的 adapter：`tool_resume_request`、`task_claim_request`、`task_commit_request`、`task_cancel_request`、`asset_read_request`、`asset_write_request`、`asset_promote_request`、`notification_send_request`、`notification_retry_request`。

仍可达的 legacy 分支：

- `config.agent_loop_mode` 默认 `loop`。`BETTER_AGENT_LOOP_MODE=legacy` 时 `conversation.py` 仍走旧计划保存（`_is_plain_existing_plan_save`）。
- `LearningService.execute`：`pipeline is None`（`BETTER_AGENT_LEARNING_V3` 默认 OFF）时调用 `learning_legacy.execute_legacy`。
- 没有 `BETTER_AGENT_LEGACY_MODE`。旧路径没有 `legacy.*` 遥测事件。

后续任务直接使用本表，不再全库重盘。P01 从模型和工具这两条已有调用收紧资源身份与显式事实。

### P01：Adapter 身份与显式事实

- 状态：已完成。`_request` 不再把 subject 的 owner/thread 抄到 resource。模型和工具发送的安全事实改为必填参数。
- 生产入口：`ModelControlStore.decide_model_send` 读取 `model_invocations` 的 owner/thread。对不上或行不存在时抛 `InvocationAccessError`，不 `finish_invocation`。工具侧由调用方传入资源 owner。
- 验证：`python -m pytest tests/test_policy_adapters.py tests/test_policy_engine.py tests/test_send_policy.py tests/test_tools.py tests/test_chat_policy.py` → 100 passed。
- 未改：任务、资产、通知 adapter 当时还没有生产调用，事实默认只留在那几条未接线的签名上。

### P02：任务查询和取消

- 状态：已完成。新增 `task.query`。对话 `operate`、研究 `get/cancel`、专家 `get_run`、对应 HTTP 在调用领域服务前走 `decide_task_control`。
- 不变量：跨 owner、跨 thread、删除线程、伪造 ID 都返回原有不可访问结果，且不调用 `get`/`cancel`/`cancel_run`。终态取消仍幂等。查询和取消不因预算耗尽被拒绝。
- 验证：`tests/test_task_capabilities.py tests/test_research_api.py tests/test_policy_adapters.py` → 58 passed。

### P03：任务执行权决策

- 状态：已完成开发接入。`TaskRuntime.claim/require/finish/recover_expired/request_cancel` 在原事务和行锁内，用任务行上的业务 owner 做决策。worker lease owner 只出现在租约字段，不写成资源 owner。
- 不变量：决策拒绝时不更新任务行。恢复和取消不要求持有已失效租约。没有来源回合的目标 run 从持久化 `agent_loop_context` 解析业务 owner，并校验 run_id；缺失、畸形或绑定不符时拒绝执行，不再绕过 Policy。预算不在这里拦截。
- 验证：`tests/test_agent_tasks.py tests/test_task_runtime.py tests/test_policy_adapters.py` → 46 passed。隔离 PostgreSQL 的双领取测试未在本轮重跑。
- 下一项：P04 资产读取、写入和采用。

### P02/P03 审查缺口修复（2026-10-14）

- 本轮仅修复三个已确认缺口，按用户要求完成后停止，不进入 P04。
- 专家终态取消在原事务中返回已有结果；重复 HTTP 取消不再返回 409，不重复写事件或改终态。
- `cancel_now` 调用统一取消决策；领域取消发现拒绝时抛出冲突，回滚整笔 run/task/attempt/event 变更，避免部分子任务取消后留下不一致状态。
- 独立 Goal 的 claim/heartbeat/finish 使用持久化上下文的业务身份；缺失或错误上下文不再自动允许。历史任务仍可读取，但没有可信身份不能继续执行，也不自动补造 owner。
- 新增测试覆盖：HTTP 重复取消、已成功任务取消不改终态、第二个子任务取消被拒绝时整笔回滚、独立 Goal 的有效/拒绝/缺失/畸形/错误 run_id 上下文。两条旧测试按新身份语义调整：预算兼容测试保留可信上下文；缺失来源回合测试断言拒绝且无交互或记忆写入。
- 隔离 PostgreSQL：`python -m pytest tests/integration/test_task_runtime_postgres.py tests/integration/test_goal_execution_postgres.py -q -rs`（backend 目录），6 passed，11.25 秒。包含新增专家并发取消仅一次事件、旧 worker 提交拒绝，以及原有双领取、过期接管和 Goal 执行权测试。
- 本轮不声明全量门禁通过，不提交、推送或部署。
- 最终定向回归：`python -m pytest tests/test_task_runtime.py tests/test_agent_tasks.py tests/test_agent_api.py tests/test_task_capabilities.py tests/test_policy_adapters.py tests/test_goal_execution_lease.py tests/test_runtime.py -q`（backend 目录），103 passed，99.87 秒。修正后的同轮生产代码与上述 PostgreSQL 验证一致；后续只调整旧测试预期和本记录。

### P04：资产读取、写入和采用

- 状态：已完成。生产读取走 `LearningAssets.freeze_request` 和 `assert_request_active`，按资产行上的 owner 调用 `asset_read_request`。记忆修订、技能启用和行为采用在提交前同时调用 `asset_write_request` 与 `asset_promote_request`。资源 owner 来自提案、技能或 evolution 候选行，不从调用者复制。
- 不变量：跨 owner、来源撤销、技能基线变化、缺少审批时不写 memory revision、不启用技能、不写 PROMOTED。拒绝可以留下 REJECTED 审计行，原因是 `authorization denied: ...`。质量门槛失败仍是 `promotion gate failed: ...`。未就绪的 canary 不进入 `_apply`。合法回滚仍走原 rollback，不套用采用审批。
- 记忆接受和技能启用都在原事务里复核 owner、scope 或基线版本后再写。行为采用在调用 `evolution.promote` 前复核候选 owner、CANARY 状态和 approval_id；evolution 自己的事务继续复核版本、审批和活跃 canary。
- 验证：`python -m pytest tests/test_policy_adapters.py tests/test_learning_promotion.py tests/test_learning_v3_fixes.py tests/test_learning_targets.py -q`（backend 目录），114 passed。`tests/integration/test_learning_v3_postgres.py::test_a_memory_cycle_runs_end_to_end_on_postgres`，1 passed。未跑完整门禁，未提交。
- 下一项：P05 通知归属和 delivery 状态。

### P04 两处审查问题修复（2026-10-15）

- 冻结编译请求时，已引用的记忆缺失、跨 owner、已归档或已过期，直接抛出 `LearningConflict`，事务回滚；不再仅丢弃依赖后继续发送原内容。技能绑定及记忆 pin 中缺失或归属不符的资产也明确拒绝。原有每次发送、重试前的来源复核保留。
- 记忆采用事务提交后调用既有 `MemoryStore.project(owner_id)`，恢复 USER.md / MEMORY.md 投影。文件写入失败时保留 PENDING 意图，由既有 `recover_projections()` 恢复，不回滚已提交记忆。
- 新增 13 个回归场景：合法编译请求、归档/过期/缺失/跨 owner、首次发送前撤销、重试前撤销、技能及记忆绑定的缺失/跨 owner、采用后即时投影、投影失败后的恢复。发送验证使用离线 MockTransport，没有付费模型调用。
- 验证：`python -m pytest tests/test_policy_adapters.py tests/test_learning_promotion.py tests/test_learning_v3_fixes.py tests/test_learning_targets.py tests/test_send_policy.py tests/test_memory_v2.py -q` → 176 passed（88.46 秒）；随后新增的绑定回归 `python -m pytest tests/test_learning_promotion.py -k unreadable_bound -q` → 4 passed（4.61 秒）。合计 180 项定向回归通过。
- 隔离 PostgreSQL：`python -m pytest tests/integration/test_learning_v3_postgres.py::test_a_memory_cycle_runs_end_to_end_on_postgres -q -rs` → 1 passed（10.27 秒），通过现有 fixture 执行迁移。
- 本轮只修复这两处问题；未执行全仓库门禁，未提交、推送或部署。P05 及后续任务状态不变。

### P05/P06：通知归属、投递状态与安全发送（2026-10-15）

- 状态：开发完成，本阶段完整门禁已验证，待审查。未进入 T/W/L，未提交、推送或部署。
- 生产入口：`NotificationService.create/list/get/update/delete/send/retry/test/deliver_completed`；HTTP 创建、列表、更新、删除、测试和报告重试传入 `x-owner-id`。`decide_notification` 只接受显式安全事实。研究完成仍由 `research/worker.py` 调用 `deliver_completed`。
- 归属：新渠道必须有 `owner_id`。历史渠道 `owner_id` 保持 NULL，不能发送，也不回填 `local-user`。报告 owner 来自 `research_jobs.thread_id → threads.owner_id`，且 `deleted_at IS NULL`、owner 非空。无法证明归属的 delivery 保持 NULL。
- 身份：唯一键是 `dispatch_key`。自动报告 `report:{job_id}:{channel_id}`；显式重试 `retry:{job_id}:{channel_id}:{uuid}`；测试 `test:{channel_id}:{uuid}`，`job_id` 为空，`purpose='test'`，不伪造 research job。测试行写入审计。
- 状态：领取在同一事务里先插入 PENDING 再改为 SENDING。唯一冲突不发 HTTP，返回已有行。外发前再次授权。ConnectError（含连接超时）为 RETRY_WAIT / NOT_SENT，同一 key 不再自动发送。读/写超时、协议错误和读写错误为 UNKNOWN / RESULT_UNKNOWN。HTTP 错误为 FAILED / REJECTED。配置、密钥或私有 URL 为 FAILED / NOT_SENT。成功为 SENT。落盘 SENT 失败时尝试把仍为 SENDING 的行记为 UNKNOWN / RESULT_NOT_STORED；已提交的 SENT 不会被覆盖。崩溃留下的 SENDING 不自动重发。
- 授权：跨 owner 或无主渠道在领取前是 KeyError（API 404），不写行、不发 HTTP。已归属但禁用的渠道在领取前返回 FAILED 视图，不写行、不发 HTTP。报告未完成或撤销时，`retry` 在发送前 KeyError。`deliver_completed` 只列报告 owner 名下的渠道；没有 owner 时返回空。没有 schedule 的手工任务仍不通知。
- 并发边界：领取失败者不发送。禁用若发生在外发前复核之后、socket 之前，这一次仍可能发出。不在 HTTP 期间持有数据库锁。
- 迁移：SQLite 为 `db.py` migration 51，语句在 `notification_schema.py`。Alembic revision `20261015_0030`，`POSTGRES_SCHEMA_HEAD` 同步。旧 PENDING 改为 UNKNOWN，旧 SENT/FAILED 保留，`purpose='report'`，`dispatch_key='legacy:' || id`。owner 只从仍存在的线程复制。降级会隐藏 UNKNOWN、SENDING 和测试历史，`downgrade()` 直接拒绝；未对共享测试库执行降级。
- 门禁中另行修正的三处既有失败：来源回合与持久化上下文不一致时，空闲 Goal 抛出 `run source identity changed`，活租约仍是 `LeaseLost`。专家终态取消保持幂等；完成已经胜出时取消返回现有 SUCCEEDED，取消胜出时完成仍被拒绝，终态事件和产物仍只有一份。工具调用的 `thread_id` 被改掉后，恢复会记下 FAILED / `CONTEXT_IDENTITY_CONFLICT`，不再停在 APPROVED。
- 定向验证：`python -m pytest tests/test_notifications.py tests/integration/test_notification_postgres.py -q --tb=short`（backend）→ 14 passed，11.49 秒。假 provider，没有真实通知。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2591 passed、7 skipped，1476.45 秒。前端 vitest 50 个文件、347 passed，37.76 秒。`tsc -b && vite build` 通过，5.58 秒。该脚本不含 Playwright。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。
- 已知限制：RETRY_WAIT 不会被 `deliver_completed` 再次拾起。显式重试 UNKNOWN 使用新的 `dispatch_key`，可能再次外发，旧 UNKNOWN 行保留。没有认领历史渠道的 API。provider 没有查询接口，UNKNOWN 不做自动对账。
- 下一项：T01。HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`；本批改动在未提交工作区。

### P05/P06 审查退回修复（2026-10-15）

- 状态：三处边界已修，定向回归和阶段完整门禁已通过，待审查。T/W/L 仍未开始。未提交、推送或部署。
- 发送后不能确认成功时记 UNKNOWN / RESULT_UNKNOWN，不再记 FAILED / NOT_SENT。非对象 JSON、无法解码的响应、以及提供方已返回后的结果处理异常都属于这一类。提供方明确拒绝（业务码或 HTTP 错误）仍是 FAILED / REJECTED。密钥缺失、私有 URL、连接失败仍发生在收到响应之前。
- 显式重试必须带幂等键。同一用户、报告、渠道和幂等键只领取一次；重复提交返回已有投递，不再外发。新的幂等键才是下一次主动发送。对 UNKNOWN 的再次发送必须 `confirm_duplicate`，并写入 `prior_delivery_id` 指向原投递；未确认时不创建新行、不外发。
- 删除渠道改为软删除：`deleted_at` 置位并停用，列表和新建发送都看不到它。投递行保留。已发出、尚未落盘的请求仍能写入 SENT 或 UNKNOWN。`history` 在删除后仍能读到这些行。
- 迁移：SQLite migration 52；Alembic `20261015_0031`，`POSTGRES_SCHEMA_HEAD` 同步。已有软删除或 UNKNOWN 重发关联时，0031 的降级拒绝。0030 的降级拒绝不变。
- 旧数据升级：PostgreSQL 从 `20261013_0029` 写入旧渠道和 PENDING/SENT 投递后再升到 head。PENDING 变 UNKNOWN，SENT 保留，归属只从仍存在的线程复制，历史渠道 owner 仍为空。
- 定向验证：`python -m pytest tests/test_notifications.py tests/integration/test_notification_postgres.py -q --tb=short`（backend）→ 20 passed，16.60 秒。假 provider，没有真实通知。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2597 passed、7 skipped，1468.91 秒。前端 50 个文件、347 passed，16.01 秒。构建通过，900 毫秒。不含 Playwright。`git diff --check` 退出码 0。

### P05/P06 第二轮审查修复（2026-10-15）

- 状态：本轮三处边界已修，定向回归和阶段完整门禁已通过，待审查。未宣布 P05/P06 验收通过。T/W/L 未开始。未提交、推送或部署。
- 发送阶段：生产入口是 `NotificationService._post`。本地准备（`_prepare_request`：缺密钥、URL 校验）在请求离开进程之前，失败记 FAILED / NOT_SENT。`client.post` 里只有 `httpx.ConnectError`（含连接超时）记 RETRY_WAIT / NOT_SENT。响应读取、解压、解码，以及任何不能证明未产生外部副作用的异常，记 UNKNOWN / RESULT_UNKNOWN。HTTP 状态错误仍是 FAILED / REJECTED。明确成功仍是 SENT。
- 渠道成功字段：生产入口是 `_required_success_code`。serverchan 必须有整数 `code == 0`；wecom、dingtalk 必须有整数 `errcode == 0`。缺字段、字符串、布尔或浮点都记 UNKNOWN / RESULT_UNKNOWN。非零整数是明确拒绝，FAILED / REJECTED。webhook 不套用 `code`/`errcode`：HTTP 2xx 且非 JSON，或 JSON 对象没有 `ok`、或 `ok is True`，记 SENT；`ok is False` 记 FAILED / REJECTED；`ok` 为其他类型记 UNKNOWN。
- 遗留 SENDING：生产入口是 `retry` → `_claim_retry`。同一 `dispatch_key` 在同一事务里返回已有投递，不新增发送。同一用户、报告、渠道下的 SENDING 和 UNKNOWN 都视为结果未确认。新幂等键不带确认时拒绝，不建行、不外发。可能重复的再次发送必须 `confirm_duplicate` 为真，且 `prior_delivery_id` 属于这组未确认投递；新行写入该关联，原行状态和确认依据不覆盖。超时不作为“原请求未发出”的证据。
- 并发：检查与领取在同一个事务。PostgreSQL 对渠道行 `SELECT ... FOR UPDATE`，未提交的 SENDING 会挡住另一连接的普通重试。唯一约束冲突在事务外返回已有行。SQLite 用 `BEGIN IMMEDIATE`。不持有锁跨越 HTTP，也没有新增 Worker。
- 迁移：没有新迁移。SQLite 51/52 与 Alembic `20261015_0030`、`20261015_0031` 不变，`POSTGRES_SCHEMA_HEAD` 仍是 `20261015_0031`。旧数据升级行为与上一轮相同。
- 定向验证：`python -m pytest tests/test_notifications.py tests/integration/test_notification_postgres.py -q --tb=short`（backend）→ 44 passed，38.88 秒。含损坏 gzip、发送中取消后的未确认重试、渠道成功字段，以及隔离 PostgreSQL 上渠道锁未提交时的并发领取。假 provider，没有真实通知。上一轮幂等、UNKNOWN 确认、渠道软删除和旧数据迁移测试仍在并通过。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2621 passed、7 skipped，1583.78 秒。前端 50 个文件、347 passed，37.92 秒。`tsc -b && vite build` 通过，5.34 秒。该脚本不含 Playwright。代码冻结后 `git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。
- 已知限制：外发前复核之后、socket 之前的禁用仍可能发出一次。`PoolTimeout` 等非 `ConnectError` 的超时记 UNKNOWN，不据此断言未发送。RETRY_WAIT 不会被 `deliver_completed` 拾起。确认后的再次发送是新行，原 SENDING/UNKNOWN 保留。没有认领历史空 owner 渠道的 API。provider 没有查询接口，UNKNOWN 不做自动对账。`test()` 每次仍生成新的幂等键。
- 下一项仍是 T01，本批没有启动。HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`；本批改动在未提交工作区。

### P06 跨入口重复发送修复（2026-10-15）

- 状态：自动报告发送与显式重试已共用领取规则。定向回归和阶段完整门禁已通过，待审查。未宣布 P05/P06 验收通过。T/W/L 未开始。未提交、推送或部署。
- 生产入口：`deliver_completed` → `send` 与 `retry` 都进入 `NotificationService._claim`。原先只在重试入口使用的 `_claim_retry` 已并入这个方法。`test` 也走 `_claim`，但 `job_id` 为空，不参与报告级判断。
- 同一操作：事务内先锁渠道，再按 `dispatch_key` 查找。同一 key 返回已有投递，不新增发送。新 key 不能绕过同一 `owner_id`、`job_id`、`channel_id` 上的 SENDING 或 UNKNOWN。
- 自动通知：不自动确认重复风险。已有 SENT 时返回最早的 SENT，不再外发。没有 SENT 而存在 SENDING 或 UNKNOWN 时，返回最早的那条未确认投递，不新建行、不外发。原投递和历史保留。这个结果不抛异常，也不修改研究任务状态。
- 显式再次发送：SENDING 或 UNKNOWN 仍要求 `confirm_duplicate` 为真，且 `prior_delivery_id` 属于这组未确认投递。新行写入该关联，原行不覆盖。已成功的投递不阻止另一把显式 key。领取前的归属、软删除和授权检查保持原样。
- 锁顺序：报告发送、显式重试和测试领取都是先 `SELECT notification_channels ... FOR UPDATE`（仅 PostgreSQL），再读投递，再插入。SQLite 使用 `BEGIN IMMEDIATE`。锁在 HTTP 之前释放。
- 迁移：没有新迁移。SQLite 51/52 与 Alembic `20261015_0030`、`20261015_0031` 不变，`POSTGRES_SCHEMA_HEAD` 仍是 `20261015_0031`。
- 定向验证：`python -m pytest tests/test_notifications.py tests/integration/test_notification_postgres.py -q --tb=short`（backend）→ 50 passed，40.83 秒。修复前，显式 key 与自动 key 会各进入假 provider 一次。修复后发送次数保持 1。覆盖发送中的重试、UNKNOWN、已成功后的迟到自动通知、自动发送先开始时的普通重试，以及同 key 重放和合法确认。隔离 PostgreSQL 用两个 `NotificationService` 分别调用 `retry` 与 `deliver_completed`，在渠道锁未提交时交错；原有手工插入 SENDING 的锁测试仍在。假 provider，没有真实通知。研究任务保持 COMPLETED。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2627 passed、7 skipped，1628.73 秒。前端 50 个文件、347 passed，18.13 秒。`tsc -b && vite build` 通过，873 毫秒。该脚本不含 Playwright。代码冻结后 `git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。
- 已知限制：外发前复核之后、socket 之前的禁用仍可能发出一次。自动路径把 SENT 视为该报告已经送达；仅有 RETRY_WAIT 或 FAILED 时，另一把新 key 仍可能外发，同一自动 key 不会。确认后的显式发送是新行，原 SENDING 或 UNKNOWN 保留。没有认领历史空 owner 渠道的 API。provider 没有查询接口，UNKNOWN 不做自动对账。`test()` 每次仍生成新的幂等键。
- 下一项仍是 T01，本批没有启动。HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`；本批改动在未提交工作区。

### T01–T04 轨迹查询（2026-10-15）

- 状态：T01.1–T04.4 曾实现并完成当时的门禁。该轮 Codex 复审未通过，见下一节。未宣布 T 阶段或四项优化完成。T05 起与 W/L 未开始。未提交、推送或部署。
- 文档：P05/P06 标明 Codex 复审通过。执行者通知门禁是后端 2627 passed、7 skipped；Codex 定向复核是 50 passed，没有重跑完整门禁。两次记录分开写。
- 权威来源：`EVENT_SOURCES`。thread、run、expert 从现有 `thread_events`、`events`、`agent_events` 读取。archive、embedding、learning、notification 标记为 deferred，查询不补造事件。响应 `completeness` 带 `source_not_connected`。历史无法关联为 `historical_unlinked`，legacy 扫描触顶为 `truncated`。
- 关联：重试沿用根 `trace_id`，`attempt` 与 span 分开。这个行为原写在 `bind_event_metadata`，本批用既有研究重试测试保住。没有可信上下文的历史行仍不生成 Envelope。
- 查询入口：`TraceQuery.by_trace`、`by_thread`、`by_task`。HTTP 为 `/api/traces/{trace_id}`、`/api/threads/{thread_id}/trace`、`/api/tasks/{kind}/{task_id}/trace`。先按资源归属授权。同一 `event_id` 只返回一次。专家 run 读取 `agent_events`。这一轮的子任务读取不完整，下一节改正。
- 分页：这一轮用偏移游标，读取量随 cursor 增加，排序键也不一致。复审因此未通过，下一节改为快照键集。任务过滤仍在 SQL 中使用 `job_id` 等 JSON 字段，再加 `LIMIT`。
- 权限与脱敏：普通投影只留白名单字段，嵌套 `token` 等密钥字段去掉，正文改为长度标记。`audit=true` 只校验参数，不授予内部字段；当前 HTTP 调用没有单独的可信审计身份。未知资源、跨用户和已删除线程都是 404，响应不写归属。畸形 Envelope 返回 `malformed_envelope` 或 `unsupported_version`，不回显原文。
- 迁移：没有新迁移。查询计划在 10 万条无关行上包含 Limit，沿用已有 `idx_thread_events_thread_time`，没有为表达式另加索引。
- 定向验证：`python -m pytest tests/test_trace_query.py tests/test_event_envelope.py -q --tb=short`（backend）→ 44 passed，35.81 秒。`python -m pytest tests/integration/test_trace_query_postgres.py::test_postgres_task_query_limits_unrelated_history_and_explains -q --tb=short` → 1 passed，14.35 秒。隔离 PostgreSQL，没有真实通知。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2631 passed、7 skipped，1621.83 秒。前端 50 个文件、347 passed，18.25 秒。`tsc -b && vite build` 通过，922 毫秒。不含 Playwright。代码冻结后 `git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。
- 已知限制：最终授权检查与实际网络发送之间仍有一次竞争窗口，外部通知不能承诺恰好一次。`agent_events` 没有 `trace_id`，按 trace 检索时不编造关联。Archive、Embedding、Learning、Notification 的生产事件留到 W。T05 起未开始。分页偏移和子任务漏读已由下一节替换，不以这里的描述为准。
- HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`；本批改动在未提交工作区。

### T02/T03/T04 复审修复（2026-10-16）

- 状态：上一轮复审未通过。本轮修复分页、畸形 Envelope、专家子任务读取。尚未再次复审通过。T05 起与 W/L 未开始。未提交、推送或部署。没有新迁移。
- 分页契约：数据库、合并和投影使用同一排序键 `occurred_at`、`stream_kind`、`stream_id`、`seq`、`event_id`。第一页记录 thread/run/agent 三张表当时的最大 `row_id`，后续页只读这个快照内、排序键严格大于游标的记录。快照之后插入的行，即使时间戳更早，也不进入当前游标；从头再查会包含它，且每条只出现一次。每个来源每页读取 `limit + 1` 行，不随游标变大。legacy、malformed、unsupported 保留原 `occurred_at` 和 `event_id`。任务过滤仍在 SQL 中，legacy 扫描上限仍是 5000。
- 畸形 Envelope：`project_authorized_event` 和 `_trace_of` 遇到数组、null、缺字段、错误嵌套类型和无效字段值时返回兼容标记或跳过关联，不中断同页其他记录。归属不一致仍拒绝该条，不写出对方 owner。HTTP `/api/threads/{id}/trace` 与 `/api/traces/{id}` 覆盖这些形状。
- 专家子任务：`by_task` 对已授权的 `run_id` 与 `task_id` 调用 `_agent_rows`，不读取整个 run，也不给 `agent_events` 编造 Envelope。真实写入器产生的 `agent.task.created` 与 `agent.task.started` 只出现在对应子任务；兄弟任务、其他用户和已删除线程不可读。
- 定向验证：`python -m pytest tests/test_trace_query.py tests/test_event_envelope.py -q --tb=short`（backend）→ 49 passed，26.81 秒。`python -m pytest tests/integration/test_trace_query_postgres.py::test_postgres_task_query_limits_unrelated_history_and_explains tests/integration/test_trace_query_postgres.py::test_postgres_expert_task_isolation_and_deleted_thread -q --tb=short` → 2 passed，15.27 秒。隔离 PostgreSQL。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2636 passed、7 skipped，1571.00 秒。前端 50 个文件、347 passed，37.26 秒。`tsc -b && vite build` 通过，5.61 秒。不含 Playwright。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。当时 `trace_query.py`、`event_projection.py`、`api.py` 及两份轨迹测试相对 HEAD 的差异标识是 `6998a9b7ae49d344cded5fb483f127deda8eec8d`。该标识对应复审前的分页实现，下一节替换了合并规则。
- 复审：畸形 Envelope 与专家子任务读取通过。分页未通过，见下一节。

### 分页合并再修（2026-10-16）

- 状态：只修分页的两项阻塞。畸形 Envelope 与专家子任务读取维持复审通过。分页尚未再次复审通过，不标记 T01–T04 全部复审通过。T05 起与 W/L 未开始。未提交、推送或部署。没有新迁移。
- 拒读补读：每个来源单独推进。来源批次被归属冲突耗尽时，只补读该来源，确定下一条可读候选或该来源已经结束，再和其他来源的候选比较。全局游标不能越过尚未检查的有效记录。每个来源在一次请求里最多再检查 `SOURCE_SEEK_BUDGET`（64）行。预算用尽时返回 `scan_budget`，并给出可继续的游标，不把来源标成结束。
- 跨页去重：同一 `event_id` 的代表是本次查询范围内排序键最小的那一行。只有这一行可以返回。它被拒读时，较晚的副本不代替它。判断来自按 `event_id` 的定点查询，游标只保存各来源的排序位置，不保存历史 id 列表。页大小不改变应返回的唯一事件集合。
- 保留：快照 `row_id` 上界、统一排序键、SQL 任务过滤、legacy 扫描上限 5000。
- 定向验证：`python -m pytest tests/test_trace_query.py tests/test_event_envelope.py tests/integration/test_trace_query_postgres.py -q --tb=line`（backend）→ 58 passed，53.60 秒，无跳过。含 SQLite 与隔离 PostgreSQL。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`。第一次退出码 1：后端 1 failed、2640 passed、7 skipped，1712.12 秒。失败是 `tests/test_learning_v2.py::test_identical_skill_packages_have_independent_owner_grants`，同一技能包两次安装得到不同 `package_digest`。该测试不导入轨迹查询，本轮没有修改技能安装代码；单独重跑 1 passed，1.51 秒。这次前端测试和构建未执行。第二次退出码 0：后端 2641 passed、7 skipped，1564.68 秒；前端 50 个文件、347 passed，18.03 秒；`tsc -b && vite build` 通过，945 毫秒。不含 Playwright。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。本轮三份未跟踪文件的 `git hash-object`：`backend/app/trace_query.py` 为 `3e6c0ea921f6f1bf51c453bf2a95f8d4c0e5b4b6`，`backend/tests/test_trace_query.py` 为 `b2c9f4978da8d291ae68b4701eb662275e56ed4b`，`backend/tests/integration/test_trace_query_postgres.py` 为 `0752753336498661d7e9b755d3db61bd886581ee`。按此顺序把“路径、NUL、blob 摘要”拼起来再做 SHA-1，得到 `34769d6a94933c6cb8617f67be508fc6ccda01e4`。`event_projection.py` 与 `api.py` 本轮未改；上一节的 `6998a9b7ae49d344cded5fb483f127deda8eec8d` 仍是合并规则替换前的差异标识。
- `git diff --check` 退出码 0。工作区有既有 CRLF 提示，没有新增空白错误。
- 复审：拒读补读和跨页去重已关闭。legacy 扫描触顶且尚未精确匹配时仍被当成轨迹不存在，见下一节。

### legacy 截断续读（2026-10-16）

- 状态：只修 legacy 扫描未完成却返回 404。拒读漏事件和跨页重复 `event_id` 维持复审关闭。本项尚未复审通过，不标记分页或 T01–T04 全部复审通过。T05 起与 W/L 未开始。未提交、推送或部署。没有新迁移。
- 生产入口：`TraceQuery.by_trace`。legacy 来源仍先用 `LIKE` 取最多 5000 条候选，再核对 Envelope 里的 `trace_id`。这一窗没有精确匹配、但扫描触顶时，返回空 `events`、`partial` / `truncated` 和 `next_cursor`。游标只多一个是否已经产出过记录的标志，不保存历史 id。客户端用该游标能读到上限之后的目标，位置会前进，不会停在同一游标。扫描结束且整次遍历都没有匹配时，按原约定返回不存在。已产出过记录的后续空页仍是空页，不改成 404。
- 保留：授权、脱敏、快照 `row_id` 上界、统一排序、扫描预算和 legacy 上限 5000。没有放宽或取消上限。
- 定向验证：`python -m pytest tests/test_trace_query.py tests/test_event_envelope.py tests/integration/test_trace_query_postgres.py -q --tb=line`（backend）→ 61 passed，62.86 秒，无跳过。含真实 HTTP `/api/traces/{trace_id}`、上限之后的 `tevt-legacy-5`、候选全部不匹配后的结束，以及跨用户 404。SQLite 与隔离 PostgreSQL 都验证了续读。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2644 passed、7 skipped，1655.13 秒。前端 50 个文件、347 passed，39.42 秒。`tsc -b && vite build` 通过，7.23 秒。不含 Playwright。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。单文件 `git hash-object` 已与复审核对一致，这是核对依据：`backend/app/trace_query.py` 为 `d8cfc03c71265e95796fadba68ba5df31b70a29b`，`backend/tests/test_trace_query.py` 为 `04145586272137e69b750310b8c4479021558f30`，`backend/tests/integration/test_trace_query_postgres.py` 为 `1eadc4823abe6b221dfd1a6a442fe294b8748eea`。组合摘要有两种算法。把“路径、NUL、blob 原始 20 字节”按上述顺序做 SHA-1，得到 `91ee7eaaf507cc998d7c535f741fa9a554d8bb44`。把 blob 的十六进制文本接在 NUL 后面，得到 `60135fd56fd1b273cac39c4b0034cf2171ee30f4`。仓库根目录可复现：

```text
python -c "import hashlib,pathlib; files=['backend/app/trace_query.py','backend/tests/test_trace_query.py','backend/tests/integration/test_trace_query_postgres.py']; raw=hashlib.sha1(); text=hashlib.sha1()
for path in files:
 data=pathlib.Path(path).read_bytes(); blob=hashlib.sha1(b'blob %d\0'%len(data)+data); raw.update(path.encode()+b'\0'+blob.digest()); text.update(path.encode()+b'\0'+blob.hexdigest().encode())
print('raw', raw.hexdigest()); print('text', text.hexdigest())"
```

上一节的 `34769d6a94933c6cb8617f67be508fc6ccda01e4` 是这次续读修复之前的标识。
- 复审：Codex 已通过。定向复核 61 passed，65.77 秒，没有重跑完整门禁或 Playwright。执行者完整门禁仍是上一行的 2644 / 347。T01–T04 本批后端实现因此标记为复审通过。

### T05–T06 轨迹页面（2026-10-16）

- 状态：T01–T04 后端复审已通过。T05.1–T06.4 本批复审通过。续读隔离、`outcome.error.phase` / `outcome.error.retryable` 和专家范围终态判定均已关闭。T07 已接入范围复审通过，见下一节。W01 本批复审通过。W02 已实现、等待复审，见 W02 一节。W03 起与 L 未开始。未提交、推送或部署。没有新迁移。
- 复审记录分开：执行者完整门禁是后端 2644 passed、7 skipped，1579.72 秒，前端 51 个文件、369 passed，16.43 秒，构建 666 毫秒。Codex 本轮只重跑前端 5 个文件、65 passed，6.12 秒，并确认组合标识 `435fa77a1f1770a873e8788419b2ac550bc6f821`。Codex 没有重跑完整门禁、构建或 Playwright，也没有改文件。
- 生产入口：`getTrace` 复用现有 `fetch`。`useTraceTelemetry` 是页面读取入口。有对话时按线程查询；只有专家运行时按专家任务查询。`TrajectoryPage` 的时间线使用这个 Hook。`ExpertRunCard` 仍读取专家运行、任务和成果接口。没有对话也没有专家任务的目标运行，仍使用原有运行轨迹。
- 游标：作为不透明字符串放进查询参数，前端不解析。空 `events` 且有 `next_cursor` 不表示结束，最多自动再读 3 个空页，然后停在可继续的游标。显式刷新和轮询都从无游标重新查询。首屏、轮询和续读写入前核对当前 scope 与请求代次；切换范围、刷新或卸载会使旧请求失效。旧响应和旧异常不改 events、cursor、completeness、error、loading。续读不因范围变化而带着旧游标重新发起。
- 终态：线程或 trace 范围不能由某一次 `run.completed` / `run.failed` / `run.cancelled` 证明已经结束，因此继续有界轮询。只有当前专家任务范围里，`stream_kind=agent` 且 `stream_id` 等于该运行的 `agent.run.completed` / `agent.run.failed` / `agent.run.cancelled` 才停止轮询。没有新事件或达到轮询上限时也会停止。
- 页面：展示 attempt、失败阶段、可重试性、结果未知、父事件引用。失败阶段和可重试性读取 `outcome.error.phase` 与 `outcome.error.retryable`，对应后端 `ExecutionOutcome.to_dict()`。`effect=unknown` 或 `status=reconciliation_required` 显示结果未知。`error` 为 null 时不显示失败阶段和可重试性。父事件不在已加载集合里时只保留“尚未加载”，不编造父子结构。来源未接入、历史无法关联和扫描未完成单独可见。摘要不渲染 payload 原文。
- 定向验证：`npx vitest --run src/__tests__/traceTelemetry.test.ts src/__tests__/TrajectoryPage.test.tsx src/__tests__/EventStreamSummary.test.tsx src/__tests__/api.test.ts src/__tests__/trajectory.test.ts`（frontend）→ 5 个文件、65 passed，3.68 秒。先出现 12 项失败，对应三项回归，再改实现。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2644 passed、7 skipped，1579.72 秒。前端 51 个文件、369 passed，16.43 秒。`tsc -b && vite build` 通过，666 毫秒。不含 Playwright。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。上一轮被拒复审的组合摘要是 `41d7c2ab84333d34c6cf523b16e925c87f2494dd`。本轮同一组十个文件的 `git hash-object`：`frontend/src/api.ts` `249ba3e6cae784efb4aaa0bd7c522b853bc53630`，`frontend/src/types.ts` `1bf9e97649bde987ac9f430eda75093db52fe39e`，`frontend/src/trajectory.ts` `9bda55352b9eda5a9ba4a7fd01ede43a4fea0ffc`，`frontend/src/hooks/useTraceTelemetry.ts` `fd6557ca8cc04a826908d6fd684001e4aed49298`，`frontend/src/components/EventStream.tsx` `c318b7d498df5a02b3255acc9023b6a1c3bc0652`，`frontend/src/pages/TrajectoryPage.tsx` `b4d397896ef715b4009f2f1c38f04748d489179c`，`frontend/src/__tests__/traceTelemetry.test.ts` `e1284adbf25c5884c2e206bef77c62431a4af9b7`，`frontend/src/__tests__/TrajectoryPage.test.tsx` `c938738d833845d3217cc78d6f7728b393503a68`，`frontend/src/__tests__/EventStreamSummary.test.tsx` `192373c24fb578f1519f0e8c3fb9fe2c836a65a9`，`frontend/src/__tests__/api.test.ts` `41719196259963fa4977eebccf5918aeddf63fc1`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `435fa77a1f1770a873e8788419b2ac550bc6f821`。`api.ts`、`EventStream.tsx`、`TrajectoryPage.tsx`、`api.test.ts` 的单文件摘要与被拒版本相同。

### T07 页面主流程（2026-10-16）

- 状态：T07 已接入范围复审通过。Codex 本轮复核生产构建通过（Vite 564 毫秒），`trace-mainline.spec.ts` 7 passed、2.7 分钟，对话页面及线程事件相关测试 4 个文件、51 passed、4.91 秒，`git diff --check` 退出码 0，八文件组合标识与 `049353589b13caf0190430110d4db2ede7be6b84` 一致。Codex 没有重跑完整门禁，没有改文件，没有提交、推送或部署。执行者完整门禁仍是后端 2644 passed、7 skipped，1712.37 秒，前端 51 个文件、369 passed，42.19 秒，构建 636 毫秒；执行者 Playwright 是 7 passed、2.4 分钟。两次记录分开。归档场景证明等待、输入保留和后续发送恢复，不表示原等待回合自动续跑已完整验证。archive、embedding、learning、notification 仍未接入统一轨迹。W05 后回验 T07。W/L 未完成。
- 入口：复用 `frontend/playwright.config.ts`、`scripts/e2e_server.py` 和 `scripts/seed_e2e.py`。隔离数据目录仍由 Playwright 临时创建。第一次启动继承了开发机的 `DEEPSEEK_API_KEY`，现有种子的候选提议得到无效输出，规格没有运行。随后在现有配置里清空 `DATABASE_URL` 以及 OpenAI、Anthropic、DeepSeek 的密钥与地址。通过的那次没有网关，编译和复盘使用 `FixedGoalProgramCompiler`。没有运行深度研究。
- 规格：`frontend/e2e/trace-mainline.spec.ts`。命令在 `frontend` 目录执行：`npm run e2e -- e2e/trace-mainline.spec.ts`。结果 5 passed，44.6 秒。分别是主流程 4.9 秒、失败输入保留 1.0 秒、取消 3.2 秒、跨用户拒绝 1.1 秒、空页续读与刷新切换 27.9 秒。服务启动成功不是通过依据。
- 主流程：页面保存计划后，线程轨迹新增 `plan.document_ready` 且 payload 版本为 2；接口标题含 `ghp_E2ETraceToken123`，轨迹 JSON 和轨迹区域都不含这串原文，工具名显示为 `<redacted-token>`。随后生成预览、确认并激活、勾选完成当天行动、点击“今天先到这里”。复盘接口完成后，页面“今天的复盘”正文与接口 `summary` 一致。同一线程轨迹不出现 `program.activated`、`day.closed`、`review.completed`。从会话点“运行详情”进入轨迹，并确认“重新读取”的请求不带 cursor。
- 其余场景：发送失败时输入框保留原文，页面给出 503。另一份计划从页面取消后，业务状态是 CANCELLED，原计划保存事件仍可读。其他用户线程打开轨迹得到 404，不出现 `foreign-only-marker`。续读线程的前三页是其他 owner 的 envelope，生产查询返回空页和游标；“继续读取”后才出现 `已保存到计划 · v9`。刷新重新从无游标查询，v9 回到按钮之后。切到短线程后不保留 v9，短线程显示历史计划保存和“历史事件缺少可关联的上下文”。
- 未接入来源：页面显示“这些来源尚未接入：archive、embedding、learning、notification”，完整程度为部分。目标程序事件仍只写在 `goal_program_events`，没有被算作已接入轨迹。空页自动跟随的单元测试仍然有效；浏览器验证的是同一条“继续读取”路径在真实空页游标上的行为。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2644 passed、7 skipped，1593.81 秒。前端 51 个文件、369 passed，15.80 秒。`tsc -b && vite build` 通过，492 毫秒。这次门禁不含 Playwright。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。本批三个文件的 `git hash-object`：`frontend/e2e/trace-mainline.spec.ts` `dec3314fec9ff439a8245e679c395e50ecb3561a`，`frontend/playwright.config.ts` `1aba3e591a13e829395f0391f46a7d643f6d3617`，`scripts/seed_e2e.py` `1f9cd70baffbb63075d08ee32c0991326dc568aa`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `723349b2a19c67fd480dee9476c231a020fd2e6c`。这是补齐前的标识，单独保留。
- 补齐后的页面测试：先 `npm run build`，再在 `frontend` 目录执行 `npm run e2e -- e2e/trace-mainline.spec.ts`。7 passed，2.4 分钟。主流程 5.5 秒、失败输入保留 939 毫秒、取消 3.1 秒、跨用户拒绝 882 毫秒、空页续读 27.1 秒、页面创建并经 Agent 读取复盘 23.9 秒、长会话归档 1.2 分钟。无模型服务仍只跑失败输入保留；确定性模型只替换脚本化服务的单次模型尝试。
- Agent 主链路：从空白对话输入创建计划，经现有 AgentLoop、`publish_plan_document`、`preview_goal_plan`、批准 `activate_goal_plan`、批准 `complete_action`、批准 `close_day`，再由对话调用 `get_today_tasks` 读出复盘摘要。断言计划版本 actor 为 model、程序为 ACTIVE、当天行动为 COMPLETED、复盘摘要与页面一致，以及 `tool.execution.finished` 的 trace 身份能在 `/api/traces/{trace_id}` 找到同一事件。没有用预置计划代替创建，也没有直接追加成功事件。目标程序事件仍不在统一轨迹中。
- 长会话归档：隔离会话写入足够长的历史，由页面发送触发既有归档。页面出现“正在整理历史”，已发送文本留在消息列表或输入框。测试通过脚本化服务自己的释放接口写入闸门，摘要模型才返回；不删除历史。完成后归档序号前进、任务为 COMPLETED，并再次从页面发送得到回答。archive、embedding、learning、notification 仍显示未接入，完整程度为部分。没有提前实现 W，也没有伪造这些事件。
- 补齐后的阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2644 passed、7 skipped，1712.37 秒。前端 51 个文件、369 passed，42.19 秒。`tsc -b && vite build` 通过，636 毫秒。不含 Playwright。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。
- 补齐后的内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。八个文件的 `git hash-object`：`frontend/e2e/trace-mainline.spec.ts` `2d34d25baa5b937231bb59b82bb7599e08874bac`，`frontend/playwright.config.ts` `a128f9eef86141d5b66476b72c62cb2dc51b560a`，`scripts/seed_e2e.py` `e9fee94a87c17a1d3765f38bb1e511b790bc1457`，`scripts/e2e_server.py` `b5ca93d49e60e9b97496f253e3534a971b875e43`，`scripts/serve.py` `6b8919bb53d96570cbdf5360b094b04bb2ae08a5`，`scripts/e2e_scripted_model.py` `40c29fe4dade7f469cbab1dd0734af985b515522`，`frontend/src/hooks/useThreadTelemetry.ts` `d80982b046a212d77beed4d2a410016e5e2cd802`，`frontend/src/pages/ChatPage.tsx` `4a9b94c20f36020fc4e4127d31f54331e6769d2e`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `049353589b13caf0190430110d4db2ede7be6b84`。

### W01 最小租约契约（2026-10-17）

- 状态：W01 本批复审通过，父任务已勾选。执行者完整门禁是后端 2648 passed、7 skipped，1732.50 秒，前端 51 个文件、369 passed，构建 896 毫秒。Codex 定向复核是 `tests/test_lease_interface.py` 与 `tests/test_task_runtime.py` 19 passed、16.34 秒，组合标识与 `b1915a3b2b1ff7c607241178c85a0532633512cb` 一致，`git diff --check` 退出码 0。Codex 没有重跑 PostgreSQL 集成、完整门禁或 Playwright，没有改文件。两次记录分开。这次唯一领取验证不是独立 PostgreSQL 连接上的并发验证。W02 见下一节。未提交、推送或部署。
- 入口：复用 `backend/app/task_runtime.py` 的 `TaskRuntime`。行为证明写在 `backend/tests/test_lease_interface.py`。Archive、Embedding、Learning、Notification 没有被搬到同一张表，也没有补空方法。
- 可复用：任务行上的领取、续租、租约校验、完成、过期恢复和取消请求。业务 owner 来自任务所属会话或来源回合，worker owner 是领取时写入 `lease_owner` 的执行者，二者分开。epoch 在领取时加一，旧 epoch 不能续租或完成。
- 领域保留：Archive 保留 `source_hash`、覆盖游标和 `RETRY_WAIT` / `DEAD_LETTER` 恢复。Embedding 保留复合身份、profile 和在领取内部收回过期行。Learning 保留 `lease_token`、检查点和预算；它的 `owner_id` 是业务 owner。Notification 保留 `dispatch_key` 与 PENDING 到 SENDING 的投递状态，不把它改成租约。
- 不适用：不为 Archive 补 `finish` / `request_cancel`，不为 Embedding 补独立 `recover_expired` / `request_cancel`，不为 Learning 补整数 epoch 或租约取消，不为 Notification 补公开租约接口。方法名存在不是通过依据；`isinstance` 只说明任务运行时具备这些名字。
- 契约：`claim` 要求非空 worker owner、正时长、可运行或已过期的运行中行，成功后为 RUNNING、epoch 与尝试次数各加一，活租约期间第二次领取返回空。`heartbeat` 只延长当前 epoch 的 `lease_until`。`require_lease` 要求 RUNNING、同一 worker、同一 epoch、租约未过期。`finish` 写入该类型允许的状态并清租约，第二次抛 `LeaseLost`。`recover_expired` 只处理已过期的 RUNNING，成功一次，旧 token 随后不能续租或完成。`request_cancel` 只记录研究、专家和回合任务的取消时间，不改状态和 epoch；之后完成会抛 `TaskCancelled`，取消终态可以写一次。
- 定向验证：`python -m pytest -q tests/test_lease_interface.py tests/test_task_runtime.py`（backend）→ 19 passed，20.87 秒。覆盖唯一领取、过期恢复一次、旧 epoch 拒绝、再次领取后的旧 epoch 围栏、有效取消和重复完成。原有 Archive 围栏测试保留。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`backend/app/task_runtime.py` `193359fa15870148a3e893d947ddefe42e194fcc`，`backend/tests/test_lease_interface.py` `eed63078586c1a0cbe922c3e7b4975a4592eaa83`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `b1915a3b2b1ff7c607241178c85a0532633512cb`。`task_runtime.py` 工作副本是 CRLF；`git hash-object` 按仓库换行规则规范化。直接对 CRLF 字节做 blob 摘要会得到 `a62e7b017195e9cc90ee7fe67f29c50260def038`，不是本记录使用的标识。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2648 passed、7 skipped，1732.50 秒。前端 51 个文件、369 passed，21.97 秒。`tsc -b && vite build` 通过，896 毫秒。不含 Playwright。比上一阶段多出的 4 个后端测试就是本批租约行为测试。门禁期间没有再改这两个代码文件。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。

### W02 Archive 恢复与事件（2026-10-17）

- 状态：W02 第一次复审未通过。过期后、恢复或接管前，旧执行者仍能续租、提交和写失败。该写入条件已补上。随后按 PostgreSQL 生产路径复审通过；SQLite 等待写锁的时间参数问题不在该通过范围内。未提交、推送或部署。没有新迁移，没有通用 Worker 框架。
- 入口：复用 `ConversationArchiver.claim`、`heartbeat`、`_commit`、`recover_expired` 和 `ManagedArchiveWorker` 启动时的恢复。恢复仍把未耗尽的过期任务放回 `RETRY_WAIT`，耗尽的放进 `DEAD_LETTER` 并写 `finished_at`，epoch 加一，覆盖游标不动。
- 提交围栏：续租、失败和完成的 UPDATE 同时核对 worker owner、epoch、`source_hash`、`prompt_version`、`tokenizer_version`，以及租约仍未到期。PostgreSQL 在这条 UPDATE 里用 `clock_timestamp()`；SQLite 在同一条 UPDATE 里比较文本时间。不能只在写入前另做一次读取。租约已过期且尚未恢复时，续租不延长 `lease_until`，提交会回滚摘要、覆盖游标、完成状态和 `archive.committed`，失败处理不改任务或归档状态，也不写 `archive.failed`。版本或来源变化后，旧执行者同样不能续租或提交。覆盖游标只在完成更新成功的同一事务里前进。
- 事件：新建任务时，若能对应到本线程、本 owner 的真实回合，写入 `archive.queued`。领取、恢复、失败和提交在状态更新成功的同一事务里写入对应事件，并沿用该回合。没有回合就不编造事件，也不编造执行上下文；已有上下文由现有绑定器读取。心跳不写轨迹。`archive` 来源改为已接入 `thread_events`。embedding、learning、notification 仍未接入。
- 定向验证：`python -m pytest -q tests/test_archive_events.py tests/test_lease_interface.py tests/test_memory_archive.py tests/test_trace_query.py tests/integration/test_archive_lease_postgres.py`（backend）→ 74 passed，59.77 秒。其中 SQLite 3 个新测试覆盖恢复不推进游标、提交后轨迹可见、版本变化拒绝、事件写入失败则恢复回滚。隔离 PostgreSQL 覆盖 A 失联后 B 接管、A 续租和提交被拒绝，以及双恢复只有一次 `DEAD_LETTER` 且 `finished_at` 与 `updated_at` 相同。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`backend/app/memory_archive.py` `7e7220e24a1d439c102819dbc4184774aeedd8de`，`backend/app/event_metadata.py` `4446578e61482281b7df8eb99a1f1db373b90068`，`backend/tests/test_archive_events.py` `87a2c9737c5e753c2cc228e9f634c535d0ef5c22`，`backend/tests/integration/test_archive_lease_postgres.py` `ca946d9d81a7cc58d4d398e1ac9e248fde7995ce`，`frontend/src/trajectory.ts` `23403d140e64beab40f04ee956d2eaefd5349615`，`frontend/src/__tests__/trajectory.test.ts` `71531b0cdff62e2a00cc824e9b81830cb7fc2df9`，`frontend/e2e/trace-mainline.spec.ts` `85428182a43be9e8f94b818392af048708fc2f1f`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `9f841da42f8c958811a96da09b9cff0f3864acc4`。
- 阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2653 passed、7 skipped，1721.98 秒。前端 51 个文件、370 passed，40.77 秒。`tsc -b && vite build` 通过，5.38 秒。不含 Playwright。比上一阶段多出的 5 个后端测试和 1 个前端测试来自本批。通知 PostgreSQL 测试有一条既有连接池析构警告，不是失败。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。这一段和上面的 74 passed、组合标识 `9f841da42f8c958811a96da09b9cff0f3864acc4` 是第一次送审记录。Codex 复核这五个文件实际是 77 passed、70.42 秒，比 74 多 3 个；Codex 没有重跑完整门禁。
- 过期围栏回归：同一组五个文件在修复后是 79 passed，74.54 秒。多出的 2 个是 SQLite 与隔离 PostgreSQL 上“过期但尚未恢复”的续租、提交和失败写入拒绝。正常续租、有效提交和原有接管测试仍在。
- 修复后的内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。同一组七个文件的 `git hash-object`：`backend/app/memory_archive.py` `0526083126b97cb9d64540e3335af0179477e3c1`，`backend/app/event_metadata.py` `4446578e61482281b7df8eb99a1f1db373b90068`，`backend/tests/test_archive_events.py` `bb80dd81e5cb564b75ef55066045655e80300ed2`，`backend/tests/integration/test_archive_lease_postgres.py` `8925834a86b52131f1baf1451b8019a407177959`，`frontend/src/trajectory.ts` `23403d140e64beab40f04ee956d2eaefd5349615`，`frontend/src/__tests__/trajectory.test.ts` `71531b0cdff62e2a00cc824e9b81830cb7fc2df9`，`frontend/e2e/trace-mainline.spec.ts` `85428182a43be9e8f94b818392af048708fc2f1f`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `f6b4bf133e0eaa1cc77745942e4438dd4141c975`。后四个文件与第一次送审相同。
- 修复后的阶段门禁：仓库根目录 `python scripts/test_all.py`，退出码 0。后端 2655 passed、7 skipped，1758.53 秒。前端 51 个文件、370 passed，21.47 秒。`tsc -b && vite build` 通过，713 毫秒。不含 Playwright。比第一次送审多出的 2 个后端测试就是这次过期围栏回归。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。

### 数据库测试策略调整（2026-10-17）

用户明确确认不再使用 SQLite 测试，数据库相关测试统一使用隔离 PostgreSQL。已同步主开发文档与 README；当时只更新要求，未迁移测试代码、E2E 或完整门禁脚本，未执行新测试。既有混合数据库门禁仍是历史结果，不得改称 PostgreSQL-only 验证。七个页面场景的后续切换记在下一节，不表示全库已经迁移。

W02 按 PostgreSQL 生产路径复审通过；SQLite 等待写锁期间的时间参数问题退出本阶段验收范围，并非已修复。Codex 上轮复核为五文件 79 passed、72.11 秒，含隔离 PostgreSQL；七文件摘要 f6b4bf133e0eaa1cc77745942e4438dd4141c975。本次未改该批源码。后续 W03 新增测试使用隔离 PostgreSQL，旧测试按模块迁移并保留断言，W05 后仍须回验 T07。未提交、推送或部署。

### 七场景页面 E2E 改用隔离 PostgreSQL（2026-10-17）

- 状态：只切换 `frontend/e2e/trace-mainline.spec.ts` 的七个现有场景。业务调用链仍是生产 AgentLoop、工具执行、审批和领域服务。模型边界仍只替换 `RoutedModelGateway._execute_attempt`，确定性脚本在 `scripts/e2e_scripted_model.py`，没有调用付费模型。没有新迁移，没有改场景断言。其余库测试和 `scripts/test_all.py` 没有迁移，本批没有重跑完整门禁。W03 与 L 未开始。未提交、推送或部署。
- 数据库：两个页面服务各用一个库，`better_agent_e2e_plain_test` 与 `better_agent_e2e_scripted_test`。容器、目标检查和 `alembic upgrade head` 复用 `backend/tests/integration/compose.postgres.yaml`、`db_target_guard.py` 和现有 Alembic。目标不是测试库、Docker 起不来或迁移失败时，启动直接失败，不回退 SQLite。运行结束后删除该临时容器。
- 构建：`frontend` 目录 `npm run build`（`tsc -b && vite build`），退出码 0，Vite 594 毫秒。
- 页面：`frontend` 目录 `npm run e2e -- e2e/trace-mainline.spec.ts`，退出码 0，7 passed，2.0 分钟。主链路 5.4 秒，失败草稿 983 毫秒，取消 2.4 秒，跨用户 971 毫秒，空页续读 6.0 秒，页面复盘 14.7 秒，长会话归档 1.1 分钟。长会话仍只证明等待、输入保留和后续发送恢复，不表示原等待回合自动续跑已完整验证。测试通过后的服务关闭阶段，连接池遇到数据库正在关闭；这不是场景失败。结束后没有残留的 `better-agent-e2e-pg` 容器。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`scripts/e2e_postgres.py` `1dd1d6861707321c5cc6fb221fa4c0b9724ff13e`，`scripts/e2e_server.py` `e264496e6d7934bddb7e05583593983b89fdf594`，`scripts/serve.py` `9988b4b997ef0aefa95cbf756fbc9bc2135d345b`，`frontend/playwright.config.ts` `6a795304657e7d88715be0462e8093a6daab5b69`，`frontend/e2e/postgres-teardown.ts` `156322035bc30700c8245236b28703ba274bd199`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `775e303cb86df3217e577b4c6155e9d8b67d9ca4`。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。这一段是第一次送审记录。Codex 复核七个场景通过：生产构建 Vite 488 毫秒，Playwright 7 passed、1.9 分钟，退出码 0，两个测试库各自有页面数据，非测试库、Docker 失败和迁移失败都会拒绝继续，运行后无残留容器，五文件标识一致。Codex 没有重跑完整门禁。清理返回码未检查，以及关闭时连接池仍在连接，不在这次通过范围内。

### 七场景清理返回码与关闭顺序（2026-10-17）

- 状态：保留上面七个场景在 PostgreSQL 上通过的记录。本批只补测试设施收尾。正常 teardown 检查 `docker compose down` 的返回码，失败时写出诊断并以退出码 1 结束测试命令。启动失败后的清理仍是尽力而为，原启动错误继续向外抛出。页面服务先经现有 Uvicorn 生命周期停止 Worker 并关闭连接池，然后再删除容器。没有新清理框架，没有改场景断言，没有迁移其余库测试。未提交、推送或部署。
- 故障注入：`python -m pytest -q tests/test_e2e_postgres_cleanup.py`（backend）→ 5 passed，6.68 秒。覆盖 Docker 清理返回码 1、服务器仍在运行时不删除容器、容器只在服务器停止后删除、停止被打断时仍删除容器并保持失败，以及启动失败时清理返回码 1 不替换原来的启动错误。这些测试不启动 Docker。
- 页面：`frontend` 目录 `npm run e2e -- e2e/trace-mainline.spec.ts`，退出码 0，7 passed，2.0 分钟。两个页面服务都先出现 `Application shutdown complete`，再结束进程。输出里没有“数据库正在关闭”，没有连接池重连。结束后没有 `better-agent-e2e-pg` 容器。长会话仍只证明等待、输入保留和后续发送恢复。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`scripts/e2e_postgres.py` `1b752f59d764153242847636653beb0baab3ac8c`，`scripts/serve.py` `28e11b70416a9f5cb6cba08bd0b018c88a8b395a`，`frontend/playwright.config.ts` `fe02ce1a951e692816f8418b69e1f0c59d163b80`，`backend/tests/test_e2e_postgres_cleanup.py` `7d5c088598ce80ae69ee588ddd5bdfa3e1761b73`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `f5da8affe08785af5de9f4ea33df50e55f31dffa`。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。
- 复审：本批已通过。执行者记录是故障注入 5 passed、6.68 秒，以及上面的页面 7 passed、2.0 分钟、退出码 0。Codex 独立验证是故障注入 5 passed、6.72 秒，生产构建 Vite 715 毫秒，同一组页面 7 passed、2.0 分钟、退出码 0；两个服务都完成 `Application shutdown complete` 后退出，没有数据库关闭竞争或连接池重连，没有本次容器残留，四文件标识一致，`git diff --check` 通过。Codex 没有改源码，没有重跑完整门禁，没有提交、推送或部署。通过范围是这七个场景及其收尾设施，不表示其余数据库测试已迁移。测试设施收尾到此结束。

### W03 Embedding 执行权与轨迹（2026-10-17）

- 状态：W03.1–W03.4 已实现、等待复审。父任务未勾选。沿用现有 `EmbeddingWorker`，没有迁到 TaskRuntime，没有通用 Worker 框架，没有新迁移。向量只在当前 revision、当前 content hash、仍启用的 profile，以及租约所有者、epoch 和数据库时钟都匹配时写入。领取、过期接管和提交写入已有 `thread_events`，来源 `embedding` 改为已接入；learning 与 notification 仍延期，完整度保持 partial。没有关联到未删除对话真实回合时不编造轨迹。假 provider，没有付费调用。W04 起与 L 未开始。未提交、推送或部署。
- 费用边界：provider 调用发生在租约和来源检查之后、向量写入之前。调用已经返回、提交前租约失效时，本次向量回滚，任务保持 RUNNING，不写 `embedding.committed`。远端请求可能已经发生，后续执行者可以再次调用。不承诺远端 exactly-once。`attempts` 在领取时已经加一，被丢弃的调用仍占用一次尝试。
- 定向测试：`python -m pytest -q tests/integration/test_embedding_worker_faults.py tests/integration/test_postgres_queue_and_embeddings.py`（backend）→ 24 passed，14.44 秒。覆盖旧 revision、停用 profile、错误维度、双 Worker 领取、租约在发送前失效、外部请求返回后提交前中断、真实回合上的领取/接管/提交事件，以及事件写入失败时领取回滚。前端 `npm test -- --run src/__tests__/trajectory.test.ts` → 1 个文件、22 passed，1.70 秒。页面文案常量已改为“这些来源尚未接入：learning、notification”。本批没有重跑 Playwright。
- 阶段门禁：第一次 `python scripts/test_all.py` 在后端 91% 处失败。原因是读取尝试次数的方法名叫 `_attempt`，被既有快照调用点扫描当成模型尝试。已改名为 `_recorded_attempt`，没有放宽扫描规则。重跑 `python scripts/test_all.py` 退出码 0：后端 2666 passed、7 skipped，1627.75 秒（0:27:07）；前端 51 个文件、370 passed，19.58 秒；`tsc -b && vite build` 632 毫秒。该门禁不含 Playwright。旧 SQLite 测试仍在套件里，尚未按模块迁到 PostgreSQL。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`backend/app/embedding_worker.py` `35bc3ec4cca37c94fe49053dafa2a67a9b51e835`，`backend/app/event_metadata.py` `5f4038b975ffecd1888102681c2207cd9b5c94d2`，`backend/tests/integration/test_embedding_worker_faults.py` `41dd9965d4f5a31721cb47ffa17ad50ddaa8088a`，`frontend/src/trajectory.ts` `8ffb2fa220f1e6f575cd4755c227e9eb4c9a4fa9`，`frontend/src/__tests__/trajectory.test.ts` `cd98a0b982cee780a2adfe8e06a715f7ba125460`，`frontend/e2e/trace-mainline.spec.ts` `0867c3f133758470cf07bbf1ed6bb74b9e15fce3`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `4a40264b55919778c63c785ddabf63541e07706d`。文档不在该标识内。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。这一段是第一次送审记录。

### W03 提交时的来源与 profile 锁（2026-10-17）

- 状态：W03 第一次复审未通过。租约围栏有效，但 `INSERT … SELECT` 里的 `profile.active` 没有锁住 profile 行。向量行锁等待期间，另一事务可以停用 profile 并提交；锁释放后，Worker 仍用等待前的检查结果覆盖向量并把任务标成 COMPLETED。该竞争已关闭，本批复审通过。执行者完整门禁是后端 2668 passed、7 skipped，1741.22 秒，前端 370 passed，构建 635 毫秒，其中仍含旧 SQLite 测试。Codex 定向复核是两个 PostgreSQL 文件 26 passed、17.21 秒，组合标识 `8d615dc95bf46634e013fe7236b1c46d59928254`。Codex 没有重跑完整门禁或 Playwright，没有改源码。未提交、推送或部署。
- 修复：provider 调用仍然在锁之外。提交事务先锁本任务，再按固定顺序锁 memory entry、然后锁 profile，重新核对当前 revision 和启用状态，之后才写向量、完成任务和 `embedding.committed`。这两把锁保持到事务结束，所以向量行上的等待不能沿用加锁前的检查。最后一次 UPDATE 仍用数据库时钟复核租约。来源不再当前、profile 已停用或租约失效时，向量、完成状态和提交事件一起回滚。
- Codex 独立验证：两个 PostgreSQL 文件 24 passed、23.75 秒；前端轨迹文案 22 passed、1.60 秒；六文件标识与 `4a40264b55919778c63c785ddabf63541e07706d` 一致；`git diff --check` 通过。Codex 没有重跑完整门禁或 Playwright，没有改源码。
- 定向回归：`python -m pytest -q tests/integration/test_embedding_worker_faults.py tests/integration/test_postgres_queue_and_embeddings.py`（backend）→ 26 passed，15.96 秒。新增的两个用例先锁住已有向量行，让提交里的 upsert 等待，再分别停用 profile 或改当前 revision。来源变更若能在等待期间提交，写入不得完成；若被提交锁挡住，则写入先完成，来源变更排在其后。假 provider，没有付费调用。
- 阶段门禁：`python scripts/test_all.py` 退出码 0。后端 2668 passed、7 skipped，1741.22 秒（0:29:01）；前端 51 个文件、370 passed，18.50 秒；`tsc -b && vite build` 635 毫秒。该门禁不含 Playwright。旧 SQLite 测试仍在套件里，尚未迁移。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`backend/app/embedding_worker.py` `f75f8ad5ef287c1ca8d0413d07ae01c1409c0ae5`，`backend/app/event_metadata.py` `5f4038b975ffecd1888102681c2207cd9b5c94d2`，`backend/tests/integration/test_embedding_worker_faults.py` `33eeafd2e998d80b4e4f0b7fc0ed5cde5a42f786`，`frontend/src/trajectory.ts` `8ffb2fa220f1e6f575cd4755c227e9eb4c9a4fa9`，`frontend/src/__tests__/trajectory.test.ts` `cd98a0b982cee780a2adfe8e06a715f7ba125460`，`frontend/e2e/trace-mainline.spec.ts` `0867c3f133758470cf07bbf1ed6bb74b9e15fce3`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `8d615dc95bf46634e013fe7236b1c46d59928254`。文档不在该标识内。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。

### W04 Learning 执行权与恢复（2026-10-17）

- 状态：W04.1–W04.4 已实现。这一段是第一次送审记录。随后第一次复审未通过，见下一节。沿用现有 `LearningService` 与 pipeline，没有迁到 TaskRuntime，没有通用 Worker 框架，没有整数 epoch，没有心跳方法，没有 `request_cancel`，没有新迁移。执行权是领取时发出的 `lease_token`；checkpoint 的 `NO_CHANGE` 恢复会另发一枚。业务身份是任务行上已保存的 `owner_id`。`dispatched_at`、预算预留、来源校验、canary 和采用事务保持原样。W05 与 L 未开始。未提交、推送或部署。
- 恢复：`claim()` 在暂停判断之前清扫过期的 RUNNING。尚未标出发送的任务回到 QUEUED，暂停时也不会被再次领取。已经标出发送但结果未落盘的任务写成 UNKNOWN，不再领取，因此不会自动再次付费。PostgreSQL 用 `clock_timestamp()` 判断租约；SQLite 仍用绑定的当前时间。领取、完成、派发和放弃都把“租约仍有效”写进同一条语句。旧 token 不能提交。
- 轨迹：只有来源是未删除对话里的真实回合时，才在同一事务写入 `learning.claimed` 或 `learning.recovered`。载荷含任务、已保存的 owner、状态、来源和是否已派发，不含课程正文。来源 `learning` 改为已接入；notification 仍延期，完整度保持 partial。页面延期文案改为“这些来源尚未接入：notification”。本批没有重跑 Playwright。
- 并发采用：派发竞争的失败者若收到“remote learning request already dispatched”，直接抛出，不再把胜者的任务收成 REJECTED。checkpoint 恢复 `PENDING_APPROVAL` 时不重新派发、不重新生成。
- 定向测试：`python -m pytest -q tests/integration/test_learning_v3_postgres.py tests/integration/test_learning_v2_postgres.py`（backend）→ 17 passed，76.86 秒。覆盖暂停 policy、来源变化、租约失效后按已保存身份恢复、已派发重启为 UNKNOWN、checkpoint 恢复，以及并发采用只发送并采用一次。`python -m pytest -q tests/integration/test_learning_v3_postgres.py::test_concurrent_adoption_sends_and_adopts_once tests/test_learning_pipeline_v3.py` → 28 passed，30.42 秒。前端 `npm test -- --run src/__tests__/trajectory.test.ts` → 1 个文件、22 passed，1.71 秒。假 provider；真实 `dispatch` 只用于计数预算根和发送标记，没有付费调用。
- 阶段门禁：`python scripts/test_all.py` 退出码 0。后端 2674 passed、7 skipped，1809.10 秒（0:30:09）；前端 51 个文件、370 passed，22.07 秒；`tsc -b && vite build` 967 毫秒。该门禁不含 Playwright。旧 SQLite 测试仍在套件里，尚未迁移。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`backend/app/learning.py` `96a36de24bfb7edc8f4ce2cae4bf1b76e620b38c`，`backend/app/learning_pipeline.py` `38a5865fa2498cc60688cb5dceae3ebdfb643849`，`backend/app/event_metadata.py` `9732a4d02eafcf073489d99112b90e1662c999b1`，`backend/tests/integration/test_learning_v3_postgres.py` `03f89c49cdb3b2619afafa0ad0ce78bfc93efd3f`，`frontend/src/trajectory.ts` `3ac82cb270d43c2a26f5561b1595f464ebdc8b59`，`frontend/src/__tests__/trajectory.test.ts` `6fc80f9ff7cf9c881d422db9a663220f0728077a`，`frontend/e2e/trace-mainline.spec.ts` `7d2d9c03f4f685b7e20b93cb35f53100ed292c4c`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `373b8c22a0daac03c78b049cb18d7081b8b85f99`。文档不在该标识内。`git diff --check` 退出码 0；工作区有既有 CRLF 提示，没有新增空白错误。

### W04 采用事务与 UNKNOWN 恢复（2026-10-18）

- 状态：W04 第一次复审未通过，父任务仍未勾选。记忆采用和 UNKNOWN 投影随后已由 Codex 复审通过。技能采用的缺口见下一节。没有新的通用 Worker 框架，没有新迁移，没有把 Learning 迁到 TaskRuntime。W05 与 L 未开始。未提交、推送或部署。
- 采用：模型调用结束后，记忆采用事务先锁提案，再锁 learning policy，然后锁任务。写入记忆和 `PROMOTED` 记录之后，再用数据库时钟复核 `lease_token` 和 policy。任一校验失败时，记忆、已接受提案和成功采用记录一起回滚。锁不跨越模型调用。普通执行和 checkpoint 恢复都把当前任务交给这条边界。这次送审没有把技能启用放进同一事务；该缺口随后被复现，见下一节。
- 轨迹：已派发且结果未落盘的 `learning.recovered` 使用 `reconciliation_required`、`effect=unknown`，`reconciliation_ref` 是原任务 id，且不可自动重试。未派发、恢复为 QUEUED 的分支仍是可重试失败，不标成结果未知。
- 定向测试：`python -m pytest -q tests/integration/test_learning_v3_postgres.py`（backend）→ 15 passed，46.03 秒。覆盖生成期间租约过期、生成期间暂停 policy、采用等待提案锁期间租约失效、checkpoint 恢复时的过期租约，以及 QUEUED 与 UNKNOWN 两种恢复投影。正常并发采用仍在这组里通过。前端 `npm test -- --run src/__tests__/trajectory.test.ts` → 1 个文件、22 passed，1.48 秒，UNKNOWN 恢复展示“结果未知”且不能重试。假 provider，没有付费调用。本批没有重跑 Playwright。
- 阶段门禁：`python scripts/test_all.py` 退出码 0。后端 2678 passed、7 skipped、1 warning，1596.65 秒（0:26:36）；前端 51 个文件、370 passed，15.89 秒；`tsc -b && vite build` 523 毫秒。该门禁不含 Playwright。警告来自通知测试收尾时连接池析构，没有失败用例。旧 SQLite 测试仍在套件里，尚未迁移。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`backend/app/learning.py` `1229044eba494e722959dd06a9d862e33e82d81f`，`backend/app/learning_pipeline.py` `9619356ed44b49438c108788d7b4b9e01e0087c9`，`backend/app/learning_promotion.py` `0714469f4b35d89cd6c31b0dfd0a10aa85837f72`，`backend/app/learning_targets.py` `44cff9edf3ac38d6ec785b1c90e582408a25cda7`，`backend/app/event_metadata.py` `c6cd0e92ec735c8b8eea764bd52c7524918f0425`，`backend/tests/integration/test_learning_v3_postgres.py` `8418d58119c249319ccbdde993bfe2f3df4b8f8e`，`frontend/src/__tests__/trajectory.test.ts` `d263a647d33818e2b822a1dd03f699e9d062ec2f`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `6514201f934b8521d388ced4d9c019194e75df21`。文档不在该标识内。

### W04 技能采用执行权（2026-10-18）

- 状态：记忆采用和 UNKNOWN 投影保持通过。技能采用随后已由 Codex 复审通过。W04 父任务仍未勾选。技能启用、默认版本切换和 `PROMOTED` 记录在同一事务里，绑定本次 `lease_token`，并在写入末端用数据库时钟复核 policy。失效时这三项一起回滚。锁顺序是技能行、learning policy、任务。没有新的通用框架，没有新迁移。W05 与 L 未开始。未提交、推送或部署。
- 行为：这次送审时，批准、canary 启动和稳定发布仍由 EvolutionService 在自己的事务里提交。该缺口随后补上，见下一节。
- 定向测试：`python -m pytest -q tests/integration/test_learning_v3_postgres.py tests/test_learning_v3_fixes.py::test_resume_uses_saved_candidate_and_does_not_regenerate tests/test_learning_promotion.py`（backend）→ 72 passed，74.04 秒。新增的隔离 PostgreSQL 用例先真实到达 `PENDING_APPROVAL`，再 `resume(approve=True)`。覆盖采用前租约过期、采用前暂停 policy、等待技能行锁期间租约失效，以及一次正常批准会启用技能并切换默认版本。假评估和假生成器，没有付费调用。本批没有重跑 Playwright。
- 阶段门禁：`python scripts/test_all.py` 退出码 0。后端 2682 passed、7 skipped、1 warning，1648.99 秒（0:27:28）；前端 51 个文件、370 passed，23.80 秒；`tsc -b && vite build` 6.62 秒。该门禁不含 Playwright。警告仍是通知测试收尾时的连接池析构，没有失败用例。旧 SQLite 测试仍在套件里，尚未迁移。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`backend/app/learning_promotion.py` `4731bb669706707b0d0f98c419238eea7897c949`，`backend/app/learning_targets.py` `b9a236e40ea38ffaedc32149cc2f855ec5484b1b`，`backend/tests/integration/test_learning_v3_postgres.py` `f43203f94898b93c619b975892f3fb40a7f2fe31`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `7ff5ae7b13e19f6b96a14909166aff9ecb87fd62`。文档不在该标识内。Codex 复核两个 Learning PostgreSQL 文件 25 passed、90.06 秒，标识一致。

### W04 行为发布执行权（2026-10-18）

- 状态：记忆采用、UNKNOWN 投影和技能采用保持通过。W04 父任务仍未勾选。W05 与 L 未开始。没有新的通用框架，没有新迁移，没有把 Learning 迁到 TaskRuntime。未提交、推送或部署。Codex 随后未通过这一节：候选 `experience_ids_json` 里不是任务主来源的证据可以在发布提交前被撤销，稳定发布仍然成功。修复见下一节。下面的 2689 门禁和六文件标识保留为该次送审记录。
- 批准和 canary：Learning 发起的批准与 canary 启动共用一个 EvolutionService 事务。事务先锁候选行，再核对本次 `lease_token`、当前 policy 和任务来源，然后才写审批、启用 canary 和 `CANARY_STARTED`。原有审批与发布门槛仍在。失效时这三项一起回滚。锁不跨越模型调用。
- 稳定发布：发生在后续 `advance_canaries`，不再使用已经结束的生成租约。持久授权依据是此前在有效租约下提交的 `CANARY_STARTED`。实际发布事务先锁候选行，再复核当前 policy 是否允许 prompt、来源是否仍然有效，以及 EvolutionService 原有的发布条件，然后才切换 stable 并写入 `PROMOTED`。安全回滚不读取 Learning 暂停状态，暂停时仍会回滚。
- 定向测试：`python -m pytest -q tests/integration/test_learning_v3_postgres.py tests/integration/test_learning_v2_postgres.py tests/test_learning_promotion.py tests/test_learning_v3_fixes.py`（backend）→ 104 passed，151.09 秒。新增隔离 PostgreSQL 用例使用假模型和假评估，真实 `PENDING_APPROVAL → resume(approve=True)`。覆盖正常批准后启动 canary、随后样本满足时切换 stable，以及采用前租约过期、暂停 policy、来源撤销、等待候选行锁期间租约失效。稳定发布等待候选行锁期间暂停 policy 不会切换 stable。暂停 policy 后，不安全 canary 仍会回滚。没有付费调用。本批没有重跑 Playwright。
- 阶段门禁：`python scripts/test_all.py` 退出码 0。后端 2689 passed、7 skipped，1589.06 秒（0:26:29）；前端 51 个文件、370 passed，15.39 秒；`tsc -b && vite build` 614 毫秒。该门禁不含 Playwright。旧 SQLite 测试仍在套件里，尚未迁移。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`backend/app/learning.py` `d3ad73a4ad6899c459ea3af8bb6f247948b90b21`，`backend/app/learning_pipeline.py` `2ed83f1096b8384bd4481e92973cfcd399af0eab`，`backend/app/learning_promotion.py` `0e05dbca4bc469b54b2d5bb59efce1e3756d3098`，`backend/app/learning_targets.py` `9190366238d4769822b672ac6119865d1d549b37`，`backend/app/evolution.py` `6ec6be23a7cd05c96455ea5fe5078cd1b1df39a8`，`backend/tests/integration/test_learning_v3_postgres.py` `466c242af9364fd652074c389b9a05dfeef756a5`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `741a8cc04a7e82c647730a0cad9f32eac34a2b32`。文档不在该标识内。

### W04 行为证据完整来源锁（2026-10-18）

- 状态：记忆采用、UNKNOWN 投影和技能采用保持通过。上一轮行为发布被 Codex 退回，因为只锁了 Learning 任务的主来源。W04 父任务仍未勾选。W05 与 L 未开始。没有新的通用框架，没有新迁移，没有把 Learning 迁到 TaskRuntime。未提交、推送或部署。
- 事务：批准、canary 启动和稳定发布使用调用方已经打开的写入连接。候选行锁定后，按经验编号顺序对 `experience_ids_json` 的全部行执行 `FOR UPDATE`，并保持到提交。候选合同带 `content_authorization_id` 时，同一连接再锁授权行，沿用原有的缺失、撤销、过期和范围校验。写入末端再次核对。任务主来源的原有检查仍在，但排在整组证据锁之后。安全回滚不读取这些来源，暂停后仍可回滚。
- 定向测试：`python -m pytest -q tests/integration/test_learning_v3_postgres.py tests/integration/test_learning_v2_postgres.py tests/test_learning_promotion.py tests/test_learning_v3_fixes.py`（backend）→ 107 passed，182.64 秒。新增隔离 PostgreSQL 用例使用假模型和假评估。非主来源先撤销并提交时，批准与 canary 不发生，稳定发布不切换 stable，部署保持 ACTIVE，成功记录没有 `PROMOTED`。发布先持有来源锁时，撤销事务等到发布提交之后；最终 stable 已切换、部署为 `PROMOTED`、成功记录为 `CANARY_STARTED` 与 `PROMOTED`，该证据随后才是 `DELETED`。暂停后的不安全 canary 仍会回滚。没有付费调用。任务主来源若先被撤销，也会被同一条证据校验拒绝，任务记为 REJECTED，不再因逃出的 LearningConflict 变成 UNKNOWN；审批、canary 和 stable 仍然不变。本批没有重跑 Playwright。
- 阶段门禁：`python scripts/test_all.py` 退出码 0。后端 2692 passed、7 skipped，1871.67 秒（0:31:11）；前端 51 个文件、370 passed，48.06 秒；`tsc -b && vite build` 7.24 秒。该门禁不含 Playwright，没有 warning。旧 SQLite 测试仍在套件里，尚未迁移。上一轮 2689 与六文件标识 `741a8cc04a7e82c647730a0cad9f32eac34a2b32` 保留为被退回的送审记录。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`backend/app/evolution.py` `5420dd819fdb9f771daa20fac4dc77761d735d2c`，`backend/app/learning.py` `3778c19887c40c9b4bfb0307122f84bf75dc1000`，`backend/app/learning_promotion.py` `60a3a2b8512cb1c7929d2b4163dedaaeca2baf33`，`backend/tests/integration/test_learning_v3_postgres.py` `f2e078889097dfdbb4f8160bd4f54ac70bb1cf55`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `1588c3ba45dad2d13409477a4ec569822ab28252`。`backend/app/learning_pipeline.py` 仍是 `2ed83f1096b8384bd4481e92973cfcd399af0eab`，`backend/app/learning_targets.py` 仍是 `9190366238d4769822b672ac6119865d1d549b37`。文档不在该标识内。Codex 随后通过了这一节的完整证据锁，四文件标识一致；W04 仍因发布拒绝后的调度缺口未通过，见下一节。

### W04 发布拒绝后的 canary 调度（2026-10-18）

- 状态：W04 本批复审通过。记忆采用、UNKNOWN 投影、技能采用、完整证据锁和本轮 canary 调度均已关闭。执行者阶段门禁是后端 2693 passed、7 skipped，1580.01 秒，前端 51 个文件、370 passed，17.02 秒，构建 648 毫秒。两文件组合标识 `0ddcddcd0865d8a8824c572e2608a82776544420`。Codex 本轮宣布复审通过。本指令没有另附定向测试计数；不把上一轮完整证据锁的 35 passed、114.69 秒改写成这次通过记录。Codex 没有在本轮通过说明里重跑完整门禁或 Playwright。执行者门禁与 Codex 本轮结论分开记录。没有新的通用框架，没有新迁移。未提交、推送或部署。

### W05 通知保留现有执行权（2026-10-19）

- 决定：通知不迁到 TaskRuntime。现有领取已经是执行权：渠道 `FOR UPDATE`、唯一 `dispatch_key`，先落成 `SENDING` 再发 HTTP。`SENDING`、`UNKNOWN`、`SENT` 阻止自动重发。租约过期再领取会变成再发一次，所以没有 `lease_token`、心跳、epoch 或新表。
- 重启：`recover_interrupted_sends` 把同一行从 `SENDING` 改为 `UNKNOWN`，不新增投递，不调用 provider。研究 Worker 启动前和进程启动后各调用一次，第二次没有 `SENDING` 行时为空。未确认结果的显式重试仍要确认重复并带上原投递。`FAILED` 与 `RETRY_WAIT` 的自动重试保持原规则。另一个进程在 POST 期间启动，可能把这次发送标成 UNKNOWN；原进程写回时匹配不到 `SENDING`，不会覆盖成 SENT。
- 事件：只有研究报告所在的未删除对话、且来源回合仍属于该对话时，才在同一事务写入 `notification.claimed`、`notification.sent`、`notification.unknown` 或 `notification.failed`。测试发送不写事件。载荷不含标题、正文、密钥或 URL。`notification.unknown` 投影为需要核对、效果未知、不可自动重试。来源 `notification` 改为已接入。
- 页面：延期句不再出现。带历史无法关联上下文的种子页仍是部分。本轮新建的计划页和长会话页是完整。未知结果的标题是“通知结果未知”。
- 验证：通知 PostgreSQL、既有 SQLite 通知测试和一条 Trace 授权测试共 54 passed、41.32 秒。SQLite 测试未迁移，不作为验收。前端轨迹文案 22 passed。`npm run e2e -- e2e/trace-mainline.spec.ts` 完整一次 7 passed、2.4 分钟。同命令此前两次停在长会话既有的 90 秒忙碌等待，没有改 `sendUntilArchive`，也没有降低 `history_min_turns`。阶段门禁退出码 0：后端 2696 passed、7 skipped，1784.06 秒（0:29:44）；前端 51 个文件、370 passed，21.30 秒；构建 5.71 秒。门禁不含 Playwright，摘要没有 warning。`git diff --check` 退出码 0，只有换行警告。
- 九文件按 `backend/app/notifications.py`、`backend/app/research/worker.py`、`backend/app/startup.py`、`backend/app/event_metadata.py`、`backend/tests/integration/test_notification_postgres.py`、`backend/tests/test_trace_query.py`、`frontend/src/trajectory.ts`、`frontend/src/__tests__/trajectory.test.ts`、`frontend/e2e/trace-mainline.spec.ts` 顺序组合，标识 `f1a10d20a70e9f7d2ce9af543fdfe9db59aead2c`。没有新迁移。假 provider。HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。
- 状态：W05 父任务未勾选，等待复审。L 未开始。未提交、推送或部署。不表示四项优化整体完成。
- 调度：`REJECTED` 只表示这一次发布被拒绝。候选仍有 `ACTIVE` 部署时继续进入安全检查。`PROMOTED` 和 `ROLLED_BACK` 以及部署已经离开 `ACTIVE` 才停止调度。来源失效仍会拒绝发布。暂停或来源撤销不阻止安全回滚。回滚成功后不再重复记录。
- 定向测试：`python -m pytest -q tests/integration/test_learning_v3_postgres.py tests/integration/test_learning_v2_postgres.py tests/test_learning_promotion.py tests/test_learning_v3_fixes.py`（backend）→ 108 passed，159.40 秒。新增隔离 PostgreSQL 连续流程：启动 canary、撤销非主来源、发布得到 `REJECTED` 且部署仍为 `ACTIVE`、随后写入不安全样本、再次调度得到一次 `ROLLED_BACK`。stable 不变，没有 `PROMOTED`，第二次调度不再写入回滚。正常发布和暂停后的不安全回滚仍通过。假模型和假评估，没有付费调用。本批没有重跑 Playwright。
- 阶段门禁：`python scripts/test_all.py` 退出码 0。后端 2693 passed、7 skipped，1580.01 秒（0:26:20）；前端 51 个文件、370 passed，17.02 秒；`tsc -b && vite build` 648 毫秒。该门禁不含 Playwright，没有 warning。旧 SQLite 测试仍在套件里，尚未迁移。完整证据锁的 2692 与四文件标识 `1588c3ba45dad2d13409477a4ec569822ab28252` 保留为 Codex 已通过的送审记录。
- 内容标识：HEAD 仍是 `eb840fbce227f5a69576afffa845000362473e6c`。`git hash-object`：`backend/app/learning_pipeline.py` `267590e3e2b8e5742a827c912cc1b65e0c4137a4`，`backend/tests/integration/test_learning_v3_postgres.py` `4fe86d6f0f60a34d144c7c4e9b4ad31eb9138947`。按此顺序把“路径、NUL、blob 原始 20 字节”做 SHA-1，得到 `0ddcddcd0865d8a8824c572e2608a82776544420`。`backend/app/evolution.py` 仍是 `5420dd819fdb9f771daa20fac4dc77761d735d2c`，`backend/app/learning.py` 仍是 `3778c19887c40c9b4bfb0307122f84bf75dc1000`，`backend/app/learning_promotion.py` 仍是 `60a3a2b8512cb1c7929d2b4163dedaaeca2baf33`，`backend/app/learning_targets.py` 仍是 `9190366238d4769822b672ac6119865d1d549b37`。文档不在该标识内。
