# ContextSnapshot Phase 2B：全入口接入开发任务与验收

日期：2026-10-08  
代码核对基线：`37a4e9af39faa781a92db1227e444173e13c2db0`，分支 `front0920`。  
文档状态：待开发任务书。本文中的建议文件、测试编号和命令不是已完成证明。

修订记录：2026-10-08 第 1 次修订。补登目标执行 Runtime、对话执行投影、目标复盘/调整的非工具编译入口与 `LiveSafetyJudge` 三个调用点；把“缺失 owner 不得默认 `local-user`”落实为具体任务；明确对话辅助调用的 span 边界。新增 B06A、T33～T38，未改动原有任务与测试编号。

## 1. 目标、前置条件与范围

Phase 2A 已提供不可变 `ModelInputSnapshot`、持久化、网关冻结、对话与工具内部调用接入。Phase 2B 复用这些能力，完成 Research、专家、Learning、Judge、摘要、引用解析、真实评估、目标执行 Runtime、目标复盘/调整及管理端验证的逐入口接入和动态验收。

完成后，对登记范围内的每一次生产 LLM 网络发送，都能在发送前找到已提交且可校验的 invocation、输入快照及实际执行身份关联。缺少存储、身份不明、关联冲突或授权失败时，不发送请求。

上位约束：[第二阶段开发任务与验收](ContextSnapshot-第二阶段开发任务与验收-2026-09-30.md)。本文细化其中 P11～P14、C01～C06、R02，不重做 P01～P10。

### 1.1 基线不能误读

- `37a4e9a` 已提交并推送 usage 保留及 attempt 并发结算修复；对应定向回归分批共 112 项通过。详细过程见 [独立核对与修复记录](acceptance/context-snapshot-phase2/phase2a/review-usage-2026-10-07.md)。
- 112 项通过不等于 M1 全量验收通过。原 2A 报告仍含 worker 未复跑、历史环境失败及交付状态过时等内容。B00 必须核实、追加最新结论；不能直接沿用旧的“未提交”，也不能只凭 commit 宣布 M1 通过。
- 可先开展 2B 入口盘点和开发；M2 最终验收必须以 M1 证据闭环为前置条件。2B 使用独立提交/PR，不重写历史失败记录。

### 1.2 优先级

1. 最终模型输入完整冻结、可信 owner、调用边界正确、发送前已持久化。
2. 已有关键 Memory / Skill / Prompt / bundle 引用准确且可追溯。
3. 来源定位、历史片段与 dropped reason 逐步完善。`provenance=partial` 可验收，但不能掩盖已知引用冲突或撤销检查缺失。

不包含：PolicyEngine、TaskRuntime、ToolRegistry/DAG 重构；强制为所有后台 Worker 新建 Harness；新审计 UI；学习算法与 JEV 预算调整；生产 Canary 全面开启；逐字复现模型输出；自动归档 URL 指向的外部内容。

JEV、embedding 等非生成式模型请求要在入口清单中单列协议和用途。本期 `ModelRequest → ModelInputSnapshot` 验收针对生成式 LLM，不将非生成式协议强行套入该格式，也不把其记为已完成输入快照覆盖。

## 2. 当前代码与具体接入对象

以下为本次只读核对结果；开发时以符号及调用链定位，行号变化不能成为遗漏理由。

| 入口/文件 | 当前基础 | 2B 要完成的工作 |
|---|---|---|
| `research/live.py`、`research/worker.py` | plan/distill/reflect/curate/audit 等经 `_json`；write/repair 单独请求；Worker 绑定调用上下文；完成后调用 `LiveSafetyJudge`（`judge_research_output`） | 逐业务步骤验证最终输入、owner/bundle；结构修复创建新逻辑调用；Worker 恢复及上下文清理。Worker 两处上下文当前为 `owner_id=turn["owner_id"] if turn else "local-user"`，源 turn 缺失时必须拒绝，不能回落默认 owner |
| `agents.py` | 专家执行（`execute_bundle`）、`synthesize`/`synthesize_user_task` 使用 gateway；Worker 用 ambient context 调用注入的 `safety_judge`（`judge_expert_output`）；上下文来自任务/运行记录 | 专家并发隔离、汇总与 Judge 分离；任务快照只能作为来源。Judge 实现见下方 `LiveSafetyJudge` 行 |
| `evolution.py::LiveSafetyJudge` | 单一实现，`startup.py` 注入三个调用方：专家 Worker（`judge_expert_output`）、Research Worker（`judge_research_output`）、目标 Runtime（`judge_run_output`）；自身不传 context，依赖调用方 ambient context | 三个调用点分别登记 callsite ID 并分别动态验证；每次评判为独立逻辑调用，不继承被评对象的 invocation/快照 |
| `runtime.py::AgentRuntime` + `live_model.py::LiveRuntimeModel` | 生产可达：`/api/goals`、`/api/goals/{id}/messages`、`/api/runs/*`。`needs_clarification/plan/decide/reflect` 经 `_json`（请求 `role/purpose=None`，继承 ambient），另有 `repair_structured_output`；`_model_call` 与 `judge_run_output` 设置的 `ModelCallContext` **未传 owner_id**，落到 dataclass 默认 `local-user` | 逐方法登记与动态验证；owner 从已授权 goal/run 显式传入；JSON 修复为新逻辑调用；共享清单此前漏登，B00 必须补登 |
| `conversation.py::ExecutionMaterializer` | 对话 `propose_execution` 后经 `PlanExecutionCompiler(agent_runtime.model).compile` → `LiveRuntimeModel.plan`；ambient context 已显式取 turn 的 owner/bundle/root（`project_plan_for_execution`），无 Harness | 单独登记，验证快照与 turn 绑定一致、投影重试/恢复遵循现有 claim 幂等语义 |
| `goal_programs.py::_model_context` 无 Harness 分支 | 被 `ManagedGoalReviewWorker._process`（`daily_review`）、`period_review`、`goal_adjustments.py`（`adjust_goal_program`）使用；owner 来自 program 行，PostgreSQL 下自建 `goal_operation` 根预算 | 共享清单第 10 条把这些 purpose 全部归为工具链 / 2A，不准确；拆出非工具入口单独登记和验证，不改复盘/调整业务规则 |
| `learning_agent.py`、`learning_extraction.py`、`learning_prompt.py`、`learning_eval.py` | 多数已经传递 ModelCallContext，经共享网关自动获得冻结 | 验证 job 来源身份、预算/bundle、生成与 Judge 分离；补关键来源 |
| `learning_replay.py`、`evolution.py` | 已有回放与行为评估调用；部分使用 ambient context | 分开 baseline/candidate/selector/Judge；核查默认 owner。当前 `LiveBehaviorRunner._run` 构造上下文时未显式传 owner，不能视为已闭环 |
| `memory_archive.py`、`memory_reference.py` | 归档上下文来自 claim；引用解析有显式 owner | 经真实服务入口验证 claim/owner、超时、错误后上下文还原 |
| `live_model.py` 辅助分类/澄清/无历史回答 | 2A 同网关自动覆盖，部分业务入口尚无单独快照证据。`_process` 只派生一次子 span 并设为 ambient；`_classify_*`、`generate_clarification`、引用解析等不传 context，经 `child_call_context` 得到新 invocation，但 `replace` 保留同一 harness，静态推断为**多个逻辑调用共用一个 span_id** | 逐分支补动态验收，避免“主对话测试通过”等同于辅助入口全部通过；按 3.2 确定 span 边界并以测试断言 |
| `real_evaluation.py` | direct gateway 已传 control_store | 每个实验臂与配对 Judge 独立快照，profile/bundle 与冻结清单一致 |
| `model_admin.py::_verify_live` | 当前 `ModelGateway(profile).complete(...)` 未传 control_store | 改为有存储的 direct，绑定授权 owner 和被验证版本，不经 stable 路由 |
| `startup.py`、`api.py`、`model_capacity_migration.py` | runtime、API fallback 均可能构造 ModelAdminService；容量迁移的 plan/apply 也各构造一次 | 所有生产构造点注入存储；不能只修 startup 漏掉 API fallback；容量迁移构造点须登记是否可能触发 `_verify_live`，不触发则写明排除依据 |
| `model_gateway.py` 无 control_store 分支 | 当前仍允许无来源参数的低层调用 | 生产失败关闭；离线测试/探针必须显式选择；内部 routed attempt 保持单次绑定 |
| `model_control.py::RoutedModelGateway` 无 context 分支 | `complete()` 在调用方未给 context 且无 ambient 时自造 `ModelCallContext(role, purpose)`；`resolved_profile`/`output_limit` 同样回落默认 owner | 生产执行身份缺失时失败关闭，不自造上下文；预算/容量探测类只读 helper 若需默认值，必须单列且不能导致发送 |

共享清单：[callsite-inventory.md](acceptance/context-snapshot-phase2/callsite-inventory.md)。已有清单中的“待建 ModelInputSnapshot”等历史描述需要在 B00 更新；“同网关自动冻结”不能替代逐入口动态测试。现有清单第 1 节**漏登**上表中的目标执行 Runtime、对话执行投影、目标复盘/调整的非工具编译，以及 `LiveSafetyJudge` 的三个调用点；清单中的行号已大面积过时，B00 改为按符号定位。

## 3. 必须遵守的设计规则

### 3.1 执行身份和模型输入仍然分离

- `HarnessExecutionContext` 表示执行身份；`ModelInputSnapshot` 表示模型实际接收的逻辑输入。
- 有可信 Harness：复用第一阶段工厂及 `ModelCallContext.from_harness()`；一个新的逻辑 LLM 调用使用新 child span。
- 没有 Harness 的旧 Worker：允许显式 `ModelCallContext`，owner 来自已授权 job/run/claim/service 身份；不为凑字段捏造 trace、turn 或 task。已有预算和 bundle 必须继承；不存在的字段明确为 null。
- 合法单用户部署可以使用 `local-user`，但必须由可信部署/服务配置明确提供。请求缺失 owner 不能默默触发 dataclass 默认值；不能接受请求体任意指定 owner 后直接使用。
- 上述 owner 规则对网关本身同样适用：`RoutedModelGateway.complete()` 无 context 时不得自造带默认 owner 的上下文；调用方 `ModelCallContext(...)` 未显式给 owner、`... if turn else "local-user"` 一类回落，都视为身份缺失并零发送。当前已知违规点：`runtime.py` 的 `_model_call` 与 `judge_run_output`、`research/worker.py` 两处、`evolution.py::LiveBehaviorRunner._run`。
- 并发业务调用优先显式传 context；保留 `set_call_context()` 的入口必须 `try/finally reset_call_context(token)`，测试同时覆盖异常、取消及两个 owner 并发。

### 3.2 调用边界

| 场景 | invocation / snapshot | 身份与内容规则 |
|---|---|---|
| 同一次逻辑请求的网络 retry/fallback | 复用 | 每个 attempt 从快照独立还原请求，不能修改权威快照 |
| JSON 修复、重新压缩、下一章节、工具续接 | 新建 | 有 Harness 时新 span；清除父 invocation/input snapshot/幂等调用绑定 |
| 生成 → Judge；baseline → candidate → Judge | 各自新建 | 各自记录实际 role/purpose、输入、profile/bundle；不能将被评对象的身份自动作为 Judge 执行身份 |
| Worker 重试/恢复 | 依照现有状态机 | UNKNOWN 不因有快照就自动重发；明确同调用恢复还是新业务调用 |

复用已有 `new_logical_call()` 及第一阶段工厂，不新增第二套派生规则。不能只靠 purpose 是否相同判断调用边界：相同 purpose 的两次章节写作也属于两个逻辑调用。

ambient context 复用规则：一个 ambient context 下发出多个逻辑调用时（对话辅助分类/澄清/引用解析、`LiveRuntimeModel._json` 与修复、`LiveBehaviorRunner` 的作答与 Judge、`LiveSafetyJudge`），`child_call_context` 只负责同一调用内的角色适配，不能当作新逻辑调用的派生手段。有 Harness 时，每个逻辑调用必须经 `new_logical_call()` 获得新 span；无 Harness 时，必须有独立 invocation 且不继承上一调用的快照绑定。若某一现状被有意保留（例如 2A 已接受辅助调用共用 span），必须在清单中写明理由与影响，并以测试断言该现状，不能默认放过。

### 3.3 冻结、事务与来源

- 冻结发生在最终 messages/tools/参数形成之后；估算、协议转换和发送消费冻结内容。
- `ModelInputSnapshot = provider-independent logical input`；`Attempt payload = profile/provider 解析后的实际 HTTP body`。例如快照中的 `max_tokens=null` 保留 null，provider 默认值不回写快照。
- snapshot + invocation + execution context binding 是原子硬要求；Learning Asset 不强制扩展同一事务，但必需引用检查失败必须零网络请求。
- 沿用现有 envelope/provenance schema。source.kind 不支持的新来源使用已有 `other` 及明确 ID/摘要；确需扩展 schema 时单独说明兼容性，不暗改已发布版本。
- `agent_context_snapshots`、`learning_snapshots`、组装期 ContextSnapshot 只提供来源；不能冒充最终 ModelInputSnapshot。
- 外部证据只保存实际发送摘录与可获得的引用/摘要；单有 URL 不宣称保存了远程资源原始字节。
- 普通事件/列表仅记录引用、摘要和计数；快照正文按 owner 授权读取。凭据配置、Authorization header 不进入快照或验收日志。

### 3.4 管理端和低层发送边界

管理端验证仍检查一个明确 profile version，允许它尚未成为 stable；不能为了验证先激活它，也不能临时关闭预算、资产检查或绕过授权。预算/价格不足按现有成本模式明确失败；首个模型的验证不依赖已有 stable 模型。

生产 `.complete()` 缺少 control_store 时必须失败关闭。建议将无存储执行改成显式内部测试/离线构造能力，默认关闭；具体 API 在 B09 固定。HTTP/工具参数不得开启该能力。不能仅根据 `PYTEST_CURRENT_TEST`、测试模块是否已导入或调用栈自动放行。

routed 内部 `_attempt()` 是外层已完成快照绑定后的 transport，不得创建第二份 invocation；必须审计它的全部调用点。禁止新增业务代码通过 `_attempt()` 或直接 HTTP 绕过受控入口。可保留显式低层适配器测试，不要求本期重构全部 provider 类。

## 4. 小任务与开发顺序

每项交付包括代码/文档、可定位 pytest 节点和对应日志；只写“已接入”不能关单。任务 Bxx 与测试 Txx 分开编号。

### B00：核实基线、更新入口清单（对应 P14 的前置工作）

1. 记录 HEAD、工作区、Python、schema head 和 M1 当前状态；检查 2A 剩余失败/未跑项。
2. 搜索 `.complete(`、`ModelGateway(`、`_attempt(`、provider HTTP 发送及网关工厂；追踪 API/startup/Worker 装配，不以字符串匹配数作为调用总数。必须同时追踪注入的 model/judge 对象（如 `LiveRuntimeModel`、`LiveSafetyJudge`、`PlanExecutionCompiler`），不能只从网关调用点反查。
3. 为每个入口分配稳定 callsite ID，记录符号、生产可达条件、owner 来源、网关、上下文方式、目标测试、阶段和未覆盖原因。共享 helper 后面不同业务分支分别登记。
4. 补登第 2 节列出的漏登入口：目标执行 Runtime 各方法与 `judge_run_output`、`ExecutionMaterializer` 执行投影、`daily_review`/`period_review`/`adjust_goal_program` 的非工具分支、`LiveSafetyJudge` 三个调用点；把清单第 10 条拆成工具链（2A）与非工具（2B）两部分。
5. 列出所有生产 `ModelCallContext(...)` 构造点及 owner 来源；未显式给 owner 或带默认值回落的，逐一登记为 B09 的修复项。
6. 修正旧清单状态与过时行号，保留历史报告，追加 M1 对账结论。

验收：T01；每个生产生成式入口均有归属；非生成式、测试桩、离线脚本有明确排除依据。交付更新后的共享清单。

### B01：建立可复用动态验收夹具（P14）

1. 使用真实业务服务、真实网关/control store、真实快照持久化，只在 provider transport 返回固定响应。
2. 在发送钩子中用独立数据库连接读取已提交 invocation/snapshot，验证 owner、摘要、绑定及逻辑请求；协议 body 与冻结请求经对应 adapter 转换后的结果比较。
3. 记录 callsite、invocation、snapshot、attempt 的关联；统计独立网络发送，不只统计已落库成功行。
4. 提供持久化失败、超时、结构错误、取消、资产撤销和上下文交错所需注入点。默认禁止真实外网，不能 mock 掉 `.complete()` 后宣称业务冻结通过。

验收：T02、T03；特意制造未绑定发送时夹具必须失败。建议新建 `tests/snapshot_entrypoint_helpers.py`，避免建设通用测试框架。

### B02：Research 结构化步骤（P11）

1. 核对 `research/worker.py` 的 owner/run/thread/root budget/bundle 来源与恢复流程。
2. 覆盖 `LiveResearchModel.plan/distill/reflect/curate/audit` 等实际存在的方法；开发时将所有 `_json` 调用方展开进清单。
3. JSON 修复保留首次输入快照，新建逻辑调用；网络 retry 保持原调用。
4. 给已知证据摘录、prompt/bundle 增加准确来源引用；仅发送摘录不能标整份来源已 included。

验收：T04～T06。不得修改研究算法或证据评分阈值。

### B03：Research 写作、报告修复与恢复（P11）

1. 覆盖 `write`、报告修复及连续章节调用，冻结实际 policy leaf、evidence、prior_summary。
2. 连续写作即使 purpose 一样也建立新调用；进程内 runtime policy 改变不能重写旧快照。
3. 验证 Worker 失败/取消后释放上下文，恢复按持久化 run 的固定版本执行；UNKNOWN 维持现有人工/恢复处理语义。
4. 覆盖研究完成后的 `judge_research_output`：评判为独立逻辑调用，输入为实际被评报告；源 turn 缺失时拒绝，不回落 `local-user`。

验收：T07、T08、T36；旧快照保持不变，没有跨任务身份泄漏。

### B04：专家 fan-out、汇总与 Judge（P11）

1. 接入 `agents.py` 中专家执行、synthesize 及 Worker 调用 `LiveSafetyJudge` 的 `judge_expert_output` 三类入口，保留原 task/run 关联。
2. 校验 agent_context_snapshots 是来源，最终 prompt 仍由网关冻结。
3. 用不同 owner 的并发任务和同 owner 多专家交错验证隔离；所有专家、汇总、Judge 各有自己的快照。

验收：T09、T10、T36；不改 AgentTask 调度或 fan-out/join 协议。

### B05：学习生成、提取、Prompt 与 Judge（P12）

1. 逐一核对 learning_agent/extraction/prompt/eval 与调用方 pipeline 的 context 传递，owner 来自授权 job，预算来自现有 root。
2. 分别绑定实际 discovery/evidence 输入、候选生成输入、Judge rubric 与被评输出；生成绑定不能泄漏到 Judge。
3. 校验现有候选、Memory revision、Skill version、bundle 引用；已知引用冲突必须阻止发送，未知精细定位允许 partial。
4. 关闭 Learning 可选服务后，其他生产调用仍有快照；不修改 JEV 预算、候选算法、门禁阈值或资产发布权限。

验收：T11、T12、T23、T24。

### B06：回放、Evolution 与真实评估（P12）

1. 在 learning_replay/evolution/real_evaluation 中分别登记 selector、baseline、candidate、quality/safety/paired Judge。
2. 修正默认 owner 依赖：调整 `LiveBehaviorRunner` 等调用接口，从已授权评估任务显式传入；同时更新调用方和测试，禁止从不可信 case 文本取 owner。
3. 每个实验臂使用自身固定版本；Judge 采用评估协议规定的版本，不能被 candidate prompt 覆盖。快照记录实际输入，不改变实验随机化或评分规则。
4. 验证真实评估 direct gateway 的 control_store 未丢失，异常/取消不污染下一 case 的 ambient context。

验收：T13～T15；不以本期快照验收声称学习效果或生产 Canary 已通过。

### B06A：目标执行 Runtime、执行投影与目标复盘（P11/P12）

1. `AgentRuntime` 经 `LiveRuntimeModel` 的 `needs_clarification/plan/decide/reflect` 及 `repair_structured_output` 逐方法登记和接入；`_model_call` 与 `judge_run_output` 的上下文从已授权 goal/run 显式取 owner，不再依赖 dataclass 默认值。
2. `_json` 首次请求与 JSON 修复为两个逻辑调用；网络 retry 保持同一调用；runtime 的 `context_snapshot`（组装期 ContextSnapshot）只作为来源，不能冒充最终输入快照。
3. `ExecutionMaterializer` 的 `project_plan_for_execution`：快照绑定 turn 的 owner/bundle/root；投影 claim 重试与恢复遵循现有幂等语义，不重复发送已持久化成功的投影。
4. `goal_programs._model_context` 无 Harness 分支：`ManagedGoalReviewWorker`（`daily_review`）、`period_review`、`adjust_goal_program` 各自登记；owner 来自 program 行，沿用现有 `goal_operation` 根预算规则；编译与修复分属两个逻辑调用；Worker 异常/取消后 ambient context 复位。
5. 不修改目标 Runtime 状态机、ReAct 迭代预算、复盘/调整业务规则或审批流程。

验收：T33～T35、T36（`judge_run_output`）、T37。

### B07：摘要、引用解析与对话辅助入口（P12）

1. 归档摘要沿用 claim.owner_id/thread 和实际选中的消息；归档 claim 重试不擅自复用其他调用快照。
2. 引用解析验证 owner、实际候选列表及最终裁剪输入；超时/取消后上下文恢复。
3. 补齐 live_model 中分类、澄清、无历史回答及其修复分支；复用 2A 逻辑，不复制冻结代码。
4. 按 3.2 的 ambient 复用规则确定对话辅助调用的 span 边界：先用测试核实当前是否多个逻辑调用共用一个 span_id；若是，改为经 `new_logical_call()` 派生，或在清单中登记有意保留的理由并断言现状。

验收：T16～T18、T38。空内容或规则短路未触发 LLM 的分支单独标为零调用，不能冒充正向覆盖。

### B08：管理端模型验证受控化（P13）

1. 修改 ModelAdminService 的依赖注入及 `_verify_live`，复用真实 control_store、成本服务与现有验证权限。
2. 同时修正 startup 和 API service fallback 构造点；核对 `model_capacity_migration.py` 两处构造是否可能触发验证，可能则同样注入，不可能则在清单写明排除依据；避免依赖循环，不另造无成本约束的简化 store。
3. 使用可信 service owner、明确验证 purpose 和 `registered_profile_version_id`；没有业务 run/bundle 时保持 null，不能伪造 stable。
4. 保留 verification_status 的既有成功/失败协议；快照/准入失败记明确错误且不发送。验证结果不改变 stable channel。

验收：T19～T22。测试包括系统尚无 stable 模型、未激活版本、跨 owner、缺凭据、存储失败及预算拒绝。

### B09：关闭生产无存储旁路（P13）

1. 固定显式离线/低层构造 API；生产默认 `.complete()` 无 store 立即失败。将必要 provider 单测/离线脚本迁移到显式模式。
2. 复核 routed `_execute_http_attempt` 仅在已有受控调用内执行；保留一次 invocation/快照绑定。
3. 审计 eval.py、验收脚本及 API 可达 helper，离线标签不能覆盖实际生产可达路径。
4. 加入静态清单检查辅助发现新增旁路，并用 startup/API 动态测试证明默认失败关闭。
5. 执行身份缺失失败关闭：`RoutedModelGateway.complete()` 在无显式 context 且无 ambient 时拒绝发送，不再自造 `ModelCallContext(role, purpose)`；修复 B00 第 5 步登记的全部未显式 owner 或默认回落的构造点。是否让 `ModelCallContext.owner_id` 去掉默认值，由开发者评估影响面后在本任务固定，结论写入清单。
6. 静态检查覆盖新增的生产 `ModelCallContext(...)` 未显式给 owner 的情况。

验收：T25～T28、T37。离线开关不从用户 body、query、工具参数读取；存储异常不能转入 offline 分支。

### B10：统一身份、来源和失败回归（P11～P13）

1. 对以上入口逐项执行两 owner、身份缺失、关键来源冲突、retry/repair 边界测试；共享代码可参数化，真实业务调用方不能遗漏。
2. 重跑 usage/asset_revoked 和 attempt 并发结算保护，防止扩大 direct 使用范围后破坏记账。
3. 对 direct/routed 响应后撤销语义的既有差异显式登记：本期硬门禁要求每次发送前检查，不因“快照全覆盖”声称两者响应后行为已统一。若要统一，另立缺陷和专项测试，不能静默豁免 usage。

验收：T23、T24、T29、T30；保留 failed/cancelled 的已提交快照，不伪造成功或自动重发 UNKNOWN。

### B11：PostgreSQL 集成与兼容验证（P14）

1. 新建并校验隔离测试库，禁止向开发库运行自动清表的 integration fixture。
2. 用真实 PG 覆盖新增管理端绑定、后台 owner 隔离、持久化失败零发送，以及原快照不可变和并发结算测试。
3. 原则上无需新表或迁移；确有 schema 需求先说明必要性，新增 revision，不改 0025 等已发布迁移。历史无快照行保持 legacy，不补造输入。

验收：T31；数据库级约束、事务与读取证据齐全；Docker 不可用记 blocked/未执行，不以 SQLite 替代通过。

### B12：全入口覆盖率对账（P14）

1. 清单每个生产入口映射到实际收集到的 pytest node，禁止只登记不存在的名字或通配符结果。
2. 正向测试必须发生至少一次 provider 发送；无存储/身份冲突等反向测试必须零发送。
3. 对所有发送逐次断言先有有效绑定，再汇总 invocation 覆盖率；不能过滤掉未绑定请求后再计算 100%。
4. partial/complete 来源单独统计，列出 partial 原因和关键来源状态，不设“完整来源 100%”为硬门禁。

验收：T32；清单漏项、零样本、孤立发送或 unresolved 测试节点均失败。

### B13：M2 验收与交付（P14）

1. 跑新增专项、受影响业务、2A/第一阶段回归及隔离 PG 测试，逐组保留命令、退出码和失败日志。
2. 补齐 M1 未关闭证据；区分基线失败、新增失败、环境阻塞，不凭“历史就失败”豁免相关行为。
3. 生成 `phase2b/acceptance-report.md`、`evidence.json`、`logs/` 和脱敏关联样例；独立提交/PR。
4. 只有第 7 节全部满足，才能标记 M2 完成。

建议交付批次：B00～B04（Research/专家）→ B05～B07、B06A（学习/目标 Runtime/辅助）→ B08～B09（管理/旁路）→ B10～B13（收口验收）。每批可独立 review；B01 的夹具供后续复用，B09 应在各构造点迁移后启用默认拒绝，避免半迁移版本投入生产。B09 第 5 步的失败关闭会直接影响 B06A 的目标 Runtime，必须在 B06A 合并后启用。

## 5. 自动化测试用例

所有测试默认 mock provider transport，不消费付费模型。每个 T 编号至少对应一个实际 pytest 节点；参数化数量由 collect-only 实测。

| ID | 操作/故障注入 | 必须断言 |
|---|---|---|
| T01 | 静态枚举并追踪生产装配 | 所有生成式入口有稳定 ID、owner 来源、测试；排除项有理由 |
| T02 | 在真实业务 transport 回调中独立读取数据库 | 发送前快照/invocation 已提交，摘要、身份、最终输入可验证；正向发送数 > 0 |
| T03 | 分别注入 snapshot、invocation、执行绑定写入失败 | 核心事务无半条记录、零发送；错误不能触发无存储 fallback |
| T04 | 参数化调用 Research 各结构化业务步骤 | 每步有快照且 role/purpose/owner 正确，来源仅包含实际发送内容 |
| T05 | Research 首次返回非法 JSON，业务层修复 | 两个 invocation/快照；第二份含修复输入；有 Harness 时新 span；第一份不变 |
| T06 | Research 同逻辑请求网络超时后 retry | 多 attempt 共用同一快照，第二次独立还原请求 |
| T07 | 写两个章节再修复报告，中途更改外部输入对象 | 三次逻辑调用独立；旧输入不变；固定 bundle 不随 stable 改变 |
| T08 | Worker 取消/异常/恢复及 UNKNOWN | 上下文 reset，身份来自持久化 run；UNKNOWN 不自动重发 |
| T09 | 专家 fan-out 后 synthesize 和 judge | 各自有快照；任务快照只作来源；汇总看到实际专家结果 |
| T10 | 两 owner 专家任务交错，含一个异常 | 快照、预算、任务和上下文不串线，跨 owner 读取拒绝 |
| T11 | 学习生成/提取/Prompt 各真实入口 | 绑定授权 job owner/root/bundle；已有关键来源准确 |
| T12 | 生成候选后调用 LLM Judge | Judge 独立 invocation/快照；输入含 rubric 和实际候选输出，未沿用生成绑定 |
| T13 | 同一 case 的 baseline/candidate/selector/Judge | 每个逻辑调用独立，固定实验臂版本准确，Judge 不受 candidate 覆盖 |
| T14 | Evolution 由非 local-user owner 发起 | 持久化 owner 和授权任务一致；缺失身份明确拒绝；无默认 owner 泄漏 |
| T15 | 真实评估 API 触发两个 profile 及配对判断 | direct 仍受控；invocation/attempt 关联正确，case 间无污染 |
| T16 | 归档 claim 生成摘要后失败重试 | 快照内容等于实际选取消息，owner/thread 正确，恢复遵循现有幂等规则 |
| T17 | 记忆引用解析遇超时/取消及另一个 owner 请求 | 最终候选输入冻结；取消正确结束；后一请求无上下文残留 |
| T18 | 参数化辅助分类/澄清/无历史回答及修复 | 每个确有发送的业务分支有独立绑定；短路分支明确零调用 |
| T19 | 管理端验证未激活版本，系统无 stable | 发送指定 profile；可信 owner/验证 purpose；有快照，不激活 channel |
| T20 | 管理端传入另一 owner 的 version | 授权失败，零发送，不泄漏快照正文 |
| T21 | 管理端缺存储/快照写失败/缺凭据/预算拒绝 | 各场景零发送，状态不为 VERIFIED，错误可区分且不含凭据 |
| T22 | startup 与 API fallback 两种 ModelAdminService 构造 | 两条路径都注入受控依赖，不能漏掉 fallback |
| T23 | 关闭可选 learning 再调用 Research/管理/对话 | 快照机制仍工作；无已知必需依赖时不被错误阻断 |
| T24 | partial 来源、已知引用冲突、发送前资产撤销 | partial 可发送；后两者零发送，旧快照不改成新资产版本 |
| T25 | 生产默认无 store gateway.complete | configuration 类错误，零网络；传 provenance 与否均不能绕过 |
| T26 | 显式离线/低层 adapter 测试 | 仅显式模式可执行；不计入生产覆盖；普通 API 无开启参数 |
| T27 | routed 内部 _attempt 和普通 direct 各调用一次 | routed 不重复绑定；direct 有自身一份快照 |
| T28 | 请求体/query/tool 参数试图开启 offline | 不改变服务端装配，仍执行受控路径或拒绝 |
| T29 | asset_revoked 响应拒绝、无/部分 usage、正常成功 | 沿用 2A D08～D12，实际 usage 保留，失败输出不被选择，无不安全重试 |
| T30 | 两事务 finish_attempt；结算后事件写失败 | 每预算维度只扣一次、事件一次；异常整体回滚后可重试结算 |
| T31 | 真实 PG 的新增入口及原迁移/约束/隔离回归 | 独立连接见已提交绑定；失败零发送；不可换绑/篡改；历史不伪造 |
| T32 | 入口清单、transport 记录、pytest 收集结果对账 | 无遗漏/未登记节点；发送全可追踪；零样本不能算覆盖率 100% |
| T33 | 经 `/api/goals` 与 `/api/runs/*` 驱动目标 Runtime 的澄清、计划、决策、反思，并让一次 `_json` 返回非法 JSON | 每个逻辑调用独立 invocation/快照；修复为新调用，首份不变；owner 等于已授权 goal/run 的 owner（用非 `local-user` owner 验证）；组装期 context_snapshot 只作来源 |
| T34 | 对话 `propose_execution` 触发执行投影，再模拟投影 claim 失败后重试 | 快照绑定 turn 的 owner/bundle/root；重试不重复发送已成功投影；失败保留已提交快照 |
| T35 | 复盘 Worker 执行 `daily_review`、`period_review`，API 触发 `adjust_goal_program`，其中一次编译需要修复，一次抛异常 | 非工具分支各自有快照；owner 来自 program 行；编译与修复为两个调用；`goal_operation` 根预算规则不变；异常后 ambient context 复位，下一任务无残留 |
| T36 | `LiveSafetyJudge` 分别经专家 Worker、Research Worker、目标 Runtime 调用 | 三个 callsite 各有独立 invocation/快照，role 为 `judge_safety`，purpose 分别正确；不继承被评调用的 invocation 或快照；owner 来自对应 run |
| T37 | 生产装配下：无 context 调用 routed `complete()`；构造缺 owner 的上下文；Research 源 turn 缺失；`LiveBehaviorRunner` 未给 owner | 均为身份缺失类错误且零发送；不写成功 invocation；不出现 `owner_id=local-user` 的回落记录 |
| T38 | 同一 turn 内先后触发至少两个对话辅助调用（如计划保存分类与引用解析）再主调用 | 每个逻辑调用 invocation 不同；span 边界符合 B07 第 4 步的结论（新 span，或清单登记的有意共用），断言与结论一致 |

跨入口共用断言：冻结后修改源消息/工具嵌套对象不改变发送；逻辑 max_tokens 与 provider 默认值不混写；普通事件不含正文/凭据；跨 owner 读取拒绝。通用网关规则复用 2A 测试，不为每个入口复制一套全协议测试。

## 6. 测试组织与执行

建议新增以下文件，开发者可合并，但实际节点必须登记：

- `tests/test_snapshot_research_entries.py`：T04～T08。
- `tests/test_snapshot_agent_entries.py`：T09～T10、T36。
- `tests/test_snapshot_learning_entries.py`：T11～T15、T23～T24。
- `tests/test_snapshot_goal_runtime_entries.py`：T33～T35。
- `tests/test_snapshot_auxiliary_entries.py`：T16～T18、T38。
- `tests/test_snapshot_admin_entries.py`：T19～T22。
- `tests/test_snapshot_production_boundary.py`：T01～T03、T25～T28、T32、T37。
- `tests/integration/test_snapshot_entrypoints_postgres.py`：T31。

T29/T30 复用已存在的 `test_snapshot_gateway.py` 和 `integration/test_attempt_settlement_postgres.py`，追加场景时更新节点映射。

新增文件落地后，在 `D:/RAG/better/backend` 执行（当前不是已通过命令）：

```powershell
python -m pytest tests/test_snapshot_research_entries.py tests/test_snapshot_agent_entries.py tests/test_snapshot_learning_entries.py tests/test_snapshot_goal_runtime_entries.py tests/test_snapshot_auxiliary_entries.py tests/test_snapshot_admin_entries.py tests/test_snapshot_production_boundary.py -q --tb=short
python -m pytest tests/test_model_input_snapshot.py tests/test_model_input_snapshot_store.py tests/test_snapshot_gateway.py tests/test_snapshot_flow.py tests/test_snapshot_recovery.py -q --tb=short
python -m pytest tests/test_execution_context.py tests/test_harness_context_flow.py tests/test_harness_context_approval.py tests/test_model_control.py tests/test_model_gateway.py tests/test_routed_model_gateway.py tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py tests/test_conversation.py tests/test_conversation_worker.py tests/test_conversation_protocol_v2.py tests/test_goal_programs.py tests/test_goal_program_compiler.py -q --tb=short
```

按受影响模块复跑已有业务测试；最低集合为：

```powershell
python -m pytest tests/test_research_engine.py tests/test_research_service.py tests/test_research_api.py tests/test_agent_tasks.py tests/test_agent_worker.py tests/test_agent_api.py tests/test_model_admin_api.py tests/test_startup_contract.py -q --tb=short
python -m pytest tests/test_runtime.py tests/test_runtime_prompt_policy.py tests/test_materializer.py tests/test_plan_execution_projection.py tests/test_goal_reviews.py tests/test_goal_period_review.py tests/test_goal_adjustments.py tests/test_goal_adjustment_api.py -q --tb=short
python -m pytest tests/test_learning_agent.py tests/test_learning_eval.py tests/test_learning_judge_reliability.py tests/test_learning_pipeline_v3.py tests/test_learning_v3_wiring.py tests/test_learning_v3_fixes.py tests/test_evolution.py tests/test_evolution_api.py tests/test_real_evaluation.py tests/test_evaluation_api.py tests/test_memory_archive.py tests/test_memory_reference.py tests/test_memory_reference_gateway.py -q --tb=short
```

PostgreSQL：用新建隔离库设置 `TEST_DATABASE_URL`，必须先经已有 `db_target_guard` 验证。日志不写 URL 凭据；fixture 会清表，不能指向开发或生产库。

```powershell
python -m pytest tests/integration/test_snapshot_entrypoints_postgres.py tests/integration/test_model_input_snapshot_postgres.py tests/integration/test_harness_context_postgres.py tests/integration/test_chat_goal_tools_postgres.py tests/integration/test_goal_tool_recovery_postgres.py tests/integration/test_root_task_budgets.py tests/integration/test_attempt_settlement_postgres.py -q --tb=short
```

测试所需 COST_MODE/开关由 fixture 或执行配置显式设置，报告记录实际值；不能切换成本模式后将原失败隐去。开发中若新增受影响模块，应追加测试集合及理由；未执行范围必须写明。

## 7. M2 完成门禁与证据

全部满足才可宣称 Phase 2B 完成：

1. M1 已核实闭环，B00～B13 及 B06A 完成，T01～T38 对应实际测试通过。
2. 所有登记的生产生成式入口有正向动态证据及必要失败证据，没有未登记的网络旁路。
3. 在验收执行集合内，**发送前有效快照绑定覆盖率 = 100%**；每条发送可追踪到 invocation/attempt。分母来自独立 transport 记录，而非仅筛选有 snapshot_id 的行。
4. 入口覆盖率 = 已完成动态验证的生产入口数 / 已登记生产入口总数 = 100%；开关关闭、没触发发送或未测分支不能算已覆盖。
5. 以唯一 invocation 分别统计 provenance complete/partial，列出部分来源原因。来源完整率与输入冻结率分开报告，不要求来源完整率 100%。
6. owner、预算、固定版本、修复/重试、审批恢复、撤销和成本记账无新增退化；存储失败零发送；生产路径不存在默认 `local-user` 的 owner 回落，执行身份缺失零发送。
7. 隔离 PostgreSQL 测试及相关 2A/第一阶段回归通过；环境阻塞或相关未解释失败意味着门禁未通过。
8. 生产缺 store 默认拒绝；离线执行明确隔离；管理端未激活 profile 验证仍受控。

证据输出到 `docs/acceptance/context-snapshot-phase2/phase2b/`：

| 文件 | 必需内容 |
|---|---|
| `acceptance-report.md` | 代码版本、范围、M1/M2 状态、各门禁结论、失败和未执行项、已知限制 |
| `evidence.json` | commit/dirty、schema、环境、入口→任务→T 编号→pytest node、测试批次、coverage 计数与来源统计 |
| `logs/` | 每轮完整命令、时间、退出码、passed/failed/skipped、脱敏输出；保留失败轮次 |
| `binding-examples.json` | 脱敏 callsite/invocation/execution digest/snapshot ID+digest/attempt/profile 关联；无用户正文与凭据 |

结构化计数至少包含：production_entrypoints、tested_entrypoints、observed_sends、sends_with_valid_committed_binding、unique_invocations、provenance_complete、provenance_partial、unmapped_nodes。上述值必须从实际证据计算，不能手填一组 100%。

交付说明必须区分“自动化测试中全入口已覆盖”和“生产长期观测已覆盖”；本任务书不要求付费真实模型冒烟，若额外执行，单列调用数、模型、费用和限制，也不能替代故障注入测试。
