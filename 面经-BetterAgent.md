# Better Agent 项目面经

> 使用原则：以下回答以当前仓库可验证实现为准。面试时应按自己的真实职责调整“我负责/我主导”等动词；没有真实线上数据的收益只讲架构结果和测量方案。

## 1. 项目简介（简历可用）

面向本地单用户场景构建 Personal Agent Runtime，以状态机和 ReAct 驱动“澄清、计划、执行、复盘”闭环，并通过 PostgreSQL 持久化版本化计划、Checkpoint、追加式事件、幂等工具回执和执行预算；配套建设多模型路由与成本账本、pgvector HNSW 三层记忆、证据优先研究、受控专家协作和可评测回滚的持续改进机制。

## 2. 简历 Bullet

- **可恢复智能体编排：** 针对模型行为不确定、长任务易中断且难以审计的问题，将任务生命周期收敛为外层状态机与步骤内 ReAct 双层控制，统一持久化计划版本、预算、事件和 Checkpoint，并在预算耗尽或副作用不确定时主动阻断，使任务具备显式审批、断点恢复和轨迹追溯能力。
- **模型与成本治理：** 针对多供应商调用逻辑散落、重试与切换难区分、并发调用可能透支预算的问题，建设基于能力与角色的配置化路由，将逻辑 Invocation 与物理 Attempt 分账记录，只对白名单故障显式切换；以不可变价格快照和 `RESERVE/CHARGE/RELEASE` 流水完成调用前预留、调用后结算与余额释放。
- **上下文、记忆与证据研究：** 针对长对话上下文膨胀、跨项目记忆污染以及搜索摘要直接生成导致的引用错误，构建短期、经历、长期三层记忆，按 Owner 与 Scope 在 SQL 层隔离，以 pgvector HNSW 语义召回结合 PostgreSQL FTS/`pg_trgm` 词法召回和故障降级，并在 Token Budget 内生成可复现 Context Pin；深度研究按规划、检索、逐源取证、缺口反思、分节写作和引用/覆盖审计执行，使结论可回溯到已接纳来源，证据不足时显式部分完成。
- **受控并发协作：** 针对复杂任务中单模型视角单一、并行 Worker 故障后可能产生脑裂写入的问题，设计 Coordinator 与 Researcher、Planner、Critic 的 Fan-out/Fan-in 协作，通过不可变上下文快照和结构化 Artifact 隔离专家输出，并使用 Lease、Heartbeat 与 Epoch Fencing 支持超时接管、迟到结果拒绝和部分失败降级汇总。
- **可验证持续改进：** 针对运行经验无法稳定转化为系统改进、在线自动改 Prompt 风险过高的问题，将可见失败轨迹归纳为不可执行候选，采用同题配对、匿名 Judge、确定性门禁和 Bootstrap 置信区间比较质量、成本与延迟，再经人工审批、Canary 暴露、晋升与回滚更新版本化行为 Bundle。

## 3. 高频面试问题

### 3.1 你这个 Agent 系统的全流程是怎样的？

**第一人称口播：** 我把全流程拆成对话控制平面和任务执行平面。用户消息先落库，对话 Worker 拼装历史、计划、目标、记忆和 Skill 上下文，再由模型决定直接回答、结构化追问、深度研究或生成计划。需要执行的任务不会立即调用工具，而是先生成不可变计划版本并等待审批。计划获批后物化为 Run，外层状态机控制生命周期，内层逐 Step 跑 ReAct。模型可以继续、调用工具、等待外部结果、完成步骤或请求阻断；Runtime 会校验预算、工具权限和写审批。全部步骤完成后进入 Reflection，沉淀记忆候选和可见运行经验，后续才可能进入受控 Evolution。事件、Checkpoint、模型账本和工具回执都落到 PostgreSQL，因此 UI 重连或进程重启不会丢失权威状态。

**追问 1：为什么要分两个平面？** 我的考虑是对话响应和任务执行的生命周期不同。对话侧强调快速首响、澄清和意图路由，任务侧强调审批、长时运行、恢复和副作用控制。如果混在一个循环里，用户问一句简单问题也要承担完整执行器复杂度，长任务又容易依赖短连接和进程内状态。分层后，对话平面只负责把用户意图变成明确的回答、研究任务或计划 Artifact；执行平面只接受已经固化和审批的输入。两者通过数据库中的 Thread、Plan、Run 和 Context Snapshot 连接，既减少耦合，也让每层可以独立重试、观测和测试。

**追问 2：一次请求的权威数据在哪里？** 我没有把内存中的 Python 对象或前端状态当事实源。Thread、Turn、Message、Plan Version、Run、Checkpoint、工具回执和模型调用账本都持久化在 PostgreSQL，事件表以追加方式记录轨迹。业务表回答“现在是什么状态”，事件回答“如何走到这个状态”，Checkpoint 回答“从哪里继续”。前端通过 API 和 SSE 读取这些数据，只是投影；Markdown 记忆文件同样只是数据库的可重建投影。这种边界让重启恢复和审计有稳定依据，也避免 UI 丢包反向改变任务事实。

### 3.2 你是怎么使用 ReAct 的？

**第一人称口播：** 我把 ReAct 放在获批计划的单个 Step 内，而不是让它决定整个任务生命周期。每次迭代把当前 Step、上一次 Observation 和迭代号交给模型，模型只允许返回有限几类外显决策：继续、调用工具、等待结果、完成步骤或阻断。工具结果会规范化成 Observation 回填下一轮；步骤完成后通过计划服务标记完成，并重置下一个 Step 的迭代预算。我不持久化隐藏思维链，只记录决策类型、摘要、工具请求和可见 Observation。这样既保留模型根据真实结果动态调整的能力，也把 ReAct 限制在状态机、工具白名单、审批和预算之内，避免模型自行越权或无限循环。

**追问 1：Observation 里放什么？** Observation 是 Runtime 可审计的外部事实，不是模型的私有推理。它可以是工具成功后的结构化结果、工具失败的标准错误、用户拒绝写操作后的拒绝状态、等待外部结果恢复后的完成信息，或者上一轮模型要求继续时给出的公开进度摘要。工具结果会包含状态、数据、错误类别和调用绑定信息，但敏感参数不会直接写进预算或公共事件。下一轮模型基于这些可见事实决策，因此重启后只要恢复 Checkpoint 和回执，就能继续协议，而不需要还原模型脑内过程。

**追问 2：ReAct 循环怎么结束？** 正常结束有两层：模型先返回完成当前步骤，Runtime 再判断计划是否还有未完成步骤；全部完成后才从 EXECUTING 转到 REFLECTING。异常结束则由确定性门禁控制，例如每 Step 的 ReAct 预算耗尽、未知工具、工具结构错误、写操作审批缺失、不确定副作用或模型明确阻断。此时 Runtime 保存 Checkpoint，记录阻断原因和可恢复状态，再进入 BLOCKED。用户可以追加预算、补充信息或完成外部 reconciliation 后恢复。这样“结束”不是模型一句完成就算完成，而是模型决策与运行时状态共同确认。

### 3.3 为什么要用 ReAct，不直接一次性生成完整答案或固定工作流？

**第一人称口播：** 一次性生成适合纯文本问答，但只要任务依赖工具结果，模型在调用前无法知道真实 Observation，提前生成后续步骤会建立在假设上。固定工作流适合结构完全稳定的场景，但通用个人 Agent 的局部路径经常要根据搜索、文件读取或审批结果变化。纯 ReAct 又缺乏整体边界，容易循环、越权和难以展示。因此我采用“计划骨架 + Step 内 ReAct”：计划提供人可读、可版本化、可审批的粗粒度约束，ReAct 负责在单步内适应外部结果，外层状态机负责安全与生命周期。这是灵活性和确定性之间的工程折中。

**追问 1：什么情况下你不会用 ReAct？** 如果任务是一次确定性计算、单个只读查询或固定 ETL，标准函数或 DAG 更简单、成本更低，也更容易验证，我不会为了 Agent 化而强行引入 ReAct。对话平面也支持直接回答，不需要每条消息都物化 Run。只有当任务需要多轮工具交互、结果会影响下一动作、执行时间较长或存在人工审批节点时，ReAct 才能提供明显价值。这个边界很重要，因为 ReAct 会引入模型成本、非确定性和更多恢复状态，必须由问题复杂度证明它值得存在。

**追问 2：计划和 ReAct 冲突怎么办？** 计划是获批的权威执行边界，ReAct 不能随意改写它。模型若发现步骤不可行，可以返回阻断或形成新的计划修订建议，但修订会创建新 Plan Version，并通过 expected version 做并发校验；需要用户再次审批后才能执行。模型不能用工具调用偷偷扩展目标，也不能把当前 Step 的 Observation 当成新增权限。这样动态适应发生在批准边界内，真正改变目标、步骤或高风险动作时重新进入明确的控制流程。

### 3.4 状态机是怎么设计的？为什么不能只靠 Prompt？

**第一人称口播：** 状态机定义了 RECEIVED、CLARIFYING、PLANNING、AWAITING_APPROVAL、EXECUTING、AWAITING_OUTCOME、REFLECTING、BLOCKED 和几个终态，并用显式邻接表限制迁移。例如 RECEIVED 只能进入澄清或规划，未批准的计划不能进入执行，完成后不能回到执行。BLOCKED 会保存原来的 resume state，只有满足恢复条件才回到对应阶段。Prompt 只能影响模型建议，不能提供事务级不变量；模型可能输出格式错误、被提示注入或在重试时给出不同答案。状态迁移在 Python 代码和数据库事务中校验，非法转换直接失败，所以安全规则不依赖模型“记得遵守”。

**追问 1：BLOCKED 和 FAILED 有什么区别？** BLOCKED 表示当前条件不满足但存在明确恢复路径，例如缺少用户信息、ReAct 预算耗尽、等待外部 reconciliation 或临时能力不可用。它会保存 `resume_state` 和 Checkpoint，用户补信息、加预算或处理不确定动作后可以恢复。FAILED 是终态，表示按照当前 Run 协议已经不可继续，不能简单 resume。区分这两者可以避免把可修复问题永久终止，也防止对真正失败的任务盲目重试。面试中我会强调，错误分类的目标不是让状态更多，而是把恢复语义写清楚。

**追问 2：计划版本为什么不可变？** 如果原地修改计划，正在执行的 Worker、用户看到的审批页面和恢复时读到的内容可能不是同一份，无法证明某个工具动作到底基于哪个版本。项目每次修订都创建新版本，记录父版本和版本号，Run 与 Checkpoint 固定 `plan_version_id`。修订接口带 expected version，相当于 CAS；旧客户端提交会得到冲突而不是覆盖新计划。不可变版本也让事件轨迹可以准确回答“谁在什么时候批准了哪一版、哪一步基于哪一版完成”。

### 3.5 Checkpoint、追加式事件和幂等回执如何实现中断恢复？

**第一人称口播：** 我把恢复拆成三个互补机制。业务表保存当前权威状态；追加式事件按 Run 和序号记录状态变化、预算告警、模型与工具结果，提供审计轨迹；Checkpoint 保存恢复所需的最小快照，包括状态、计划版本、Step、ReAct 迭代、剩余预算、Observation、待审批动作、Artifact 引用和已应用记忆版本。重启后扫描非终态 Run，读取最新 Checkpoint，并从它的 `last_event_seq` 之后补齐事件。对于已完成工具调用，先查持久化回执并复用，不再次执行。这样恢复的是公开协议状态，而不是试图恢复无法验证的隐藏思维。

**追问 1：为什么 Event 不能替代 Checkpoint？** 理论上可以从第一条事件全量重放，但随着轨迹增长，恢复时间和投影逻辑复杂度都会上升，而且模型输出、外部回执等事件的演进兼容更难。Checkpoint 把某一时刻可恢复的派生状态固化，Event 只需从该序号之后增量应用。反过来，只有 Checkpoint 又会丢失详细审计和投影重建依据。所以我把 Event 看成历史账本，Checkpoint 看成加速恢复的锚点，业务表看成在线查询的当前投影，三者职责不同。

**追问 2：工具执行后崩溃，结果还没落库怎么办？** 这是最危险的窗口，因为超时或进程崩溃不能证明外部副作用没有发生。我不会直接重试 WRITE。执行前先用逻辑动作键抢占 Claim，并尽量把外部 idempotency key 传给连接器；若恢复时发现 Claim 仍是 RUNNING 且没有确定回执，就标记 reconciliation required 并阻断 Run。只有查询外部状态、拿到幂等回执或人工确认后才能继续。这个策略牺牲了一点自动恢复率，但避免重复扣款、重复发送或重复写文件等更严重问题。

### 3.6 模型路由和故障切换是怎么做的？

**第一人称口播：** 我把供应商配置抽象成版本化 Profile，把业务用途抽象成角色，例如 conversation、planner、executor、researcher、judge。每个角色声明硬能力需求，像 streaming、tool calling 或 JSON object；Router 先过滤不满足能力的模型，再按策略中冻结的候选优先级选择。一次逻辑请求只创建一个 Invocation，具体供应商调用记录为多个 Attempt。只有超时、限流、服务端错误和供应商不可用等白名单错误才允许 retry 或进入下一个 Profile；认证、请求结构、预算和取消不会静默 fallback。每次选择、尝试和切换都记事件和原因，因此路由是确定、显式、可审计的。

**追问 1：为什么认证错误不切换？** 认证错误通常说明配置或权限问题，不是模型临时不可用。如果静默切到另一个供应商，系统表面上成功了，却掩盖了凭证失效、环境变量配置错误或越权问题，也可能意外把数据发送到未预期的供应商。结构错误和安全拒绝同理，换模型不应该绕过协议错误。因此 fallback 只处理被定义为瞬时基础设施故障的类别，其他错误快速暴露给控制面。这个选择让可用性略保守，但故障含义和数据边界更清楚。

**追问 2：Invocation/Attempt 分账有什么实际价值？** 如果每次 retry 都当成独立请求，业务成功率、成本和延迟都会被扭曲，也无法知道一次用户动作经历了多少供应商切换。Invocation 冻结逻辑输入、上下文、工具 Schema 和路由策略，Attempt 记录具体 Profile、序号、reason、开始/首 Token/结束时间、usage、错误和成本。这样可以同时统计“用户请求是否成功”和“哪个模型尝试质量差”，也能重放评测绑定、追踪最终选中 Attempt，并通过 idempotency key 防止同一逻辑请求被重复创建。

### 3.7 成本账本为什么要用 RESERVE、CHARGE、RELEASE？

**第一人称口播：** 如果只在模型返回后扣实际费用，并发请求开始时都可能看到余额充足，最终总费用超过预算。我的做法是在 Attempt 开始前根据上下文窗口、最大输出 Token 和冻结价格快照估算最坏成本，在同一事务里检查预算并追加 RESERVE，同时增加 reserved 投影。调用结束后根据供应商 usage 计算 CHARGE，多余预留追加 RELEASE，再原子更新 reserved 和 charged。流水永不覆盖，预算表只是快速聚合。即使 usage 缺失，也显式记录 partial 或 unavailable 状态，而不是把未知成本当零。这样预算控制发生在调用之前，历史成本也不会因价格配置变化而漂移。

**追问 1：预留太保守会不会浪费预算？** 会降低短时间内的并发利用率，这是最坏成本预留的代价。当前方案优先保证硬预算不被穿透，适合个人 Agent 和评测任务。优化方向可以是基于真实历史分位数做分层预留、为不同角色设置更合理的最大输出、或允许软预算和硬预算两级门禁，但必须保留超额结算策略和可解释性。不能简单用平均成本预留，因为长尾请求在高并发下仍可能导致超卖。当前仓库实现的是保守版本，利用率收益需要基于真实调用分布再测。

**追问 2：调用失败还收费吗？** 是否收费取决于供应商是否产生了可计费 usage，不能把失败统一视为零成本。Attempt 完成时会读取 usage 并按价格快照结算；usage 完整则计算真实成本，缺失时使用已预留金额形成保守的部分估算状态。未使用的部分才 RELEASE。账本和 Attempt 都保留 cost status，所以报表可以区分确定成本与未知成本。这样不会在失败率高时低估支出，也为后续和供应商账单对账保留依据。

### 3.8 你是如何评估不同模型的质量、成本和延迟的？

**第一人称口播：** 我使用同题配对评测，而不是让 baseline 和 candidate 各跑一批不同问题。每条 case 两边使用相同工具和上下文快照，并平衡执行先后；输出会随机映射成 left/right，独立质量 Judge 只根据输入和 rubric 判断胜负，安全 Judge 单独给布尔结论。每条 case 同时得到候选相对基线的质量差、TTFT 差和微美元成本差。对 HOLDOUT 的配对差值用固定种子做两千次 Bootstrap，计算 95% 置信区间；主目标只有在区间下界达到阈值时通过。同时还要满足安全集零失败、确定性规则通过、非平局样本充分和总评测预算没有超限。

**追问 1：为什么要匿名 Judge？** 如果 Judge 看到模型名、供应商或 baseline/candidate 标签，可能带入先验偏好，形成位置或品牌偏差。项目用稳定 Hash 决定哪边放 left，并在结果中保存 `left_is_baseline`，Judge 只看到左右文本和 rubric，评完后再映射回候选胜负。执行顺序也做平衡，避免总是先跑的一边受冷启动或缓存影响。匿名不能消除所有 Judge 偏差，但至少把模型身份和固定位置这两类可控偏差隔离出来。

**追问 2：Bootstrap CI 怎么解释？** 我把每条相同 case 上的候选相对基线差值作为配对样本，例如 TTFT 是 baseline 减 candidate，成本也是 baseline 减 candidate，质量胜负编码为一、零、负一。然后有放回抽样这些差值并计算均值，重复两千次，取 2.5% 和 97.5% 分位数作为 95% 区间。若主目标阈值是零，只有区间下界仍不小于零才认为改进足够稳定。它比只看平均值更谨慎，也保留了配对设计对 case 难度差异的抵消作用。

### 3.9 你的三层记忆系统是怎么实现的？

**第一人称口播：** 我把记忆分为短期、经历和长期三层。短期是当前线程最近消息和活跃计划，保证连续对话；线程过长后，归档器把旧消息压缩成带来源范围的 Episode，形成经历记忆；长期记忆只保存稳定偏好、约束和事实，来自用户明确记住或带证据的 Proposal 确认，不让模型自动把一次推测永久化。长期条目有 Entry 与 Revision，编辑和回滚都保留版本，PostgreSQL 是权威源，Markdown 只是可重建投影。读取时统一经过 Owner、Scope、HNSW 语义召回、FTS/`pg_trgm` 词法召回、Token Budget、脱敏和 Context Pin，因此写入治理与读取治理是同一套系统。

**追问 1：短期记忆怎样变成经历记忆？** 归档器会先对 Thread 做租约式 reserve，避免多个 Worker 同时归档同一段消息，然后选择需要压缩的旧消息范围，调用 summarizer 或确定性降级逻辑生成摘要。Episode 保存 message range、摘要、来源和时间，成功后再投影；最近一段消息继续保留为短期上下文。这个过程不会直接写长期偏好，因为一次经历可能包含临时安排、错误理解或敏感数据。经历到长期还需要 Proposal、证据和用户确认，从而把“发生过”与“长期为真”分开。

**追问 2：长期记忆冲突怎么办？** 长期条目用 current revision 指向当前内容，编辑接口要求提供 base revision。若用户页面基于旧版本修改，而后台或另一个操作已经产生新版本，CAS 校验会抛出冲突，不允许静默覆盖。回滚同样需要基于当前 revision 发起，最终指向历史内容但保留新的审计动作。对于相同 Scope 和规范化 fingerprint 的重复写入，系统会去重；Proposal 还绑定 evidence hash 和 idempotency key。这样既能修正错误记忆，也能解释某个历史 Invocation 当时使用的是哪一版。

### 3.10 上下文是怎么做的？如何避免 Token 爆炸和跨项目污染？

**第一人称口播：** 上下文不是把数据库里所有内容拼起来，而是按优先级建立可审计快照。对话侧先取系统约束、最近消息、活跃计划和目标，再向统一 Memory Provider 请求相关记忆。Provider 把 owner 和 user/current-project Scope 写进检索 SQL，优先通过 pgvector HNSW 做余弦语义召回，同时执行 PostgreSQL FTS 与 `pg_trgm` 词法召回；语义不可用或没有合格候选时明确降级。候选按 pinned、语义分数、词法命中、importance 和稳定 ID 排序，语义记忆与经历记忆分别受 Token Budget 约束。最后保存 revision IDs、episode IDs、tokenizer/renderer 版本、预算和 rendered hash 的 Context Pin，使调用可复现。

**追问 1：为什么 Scope 必须先于相关性排序？** 如果先在全库做相关性 Top-K，再过滤 Scope，跨项目敏感内容已经参与候选计算，工程上容易因为过滤遗漏、日志或缓存而泄漏；同时高相关的错误项目内容还会挤占正确项目候选。先把 owner 和 user/current-project Scope 约束写入 HNSW 与词法检索 SQL，把候选宇宙限定后再排序，安全边界更明确。这个原则也适用于多租户 RAG：权限过滤应是召回阶段的硬条件，而不是生成前的软提示。

**追问 2：为什么要做 HNSW 与词法混合召回？** 只做词法检索容易漏掉同义改写，只做向量检索又可能弱化精确标识、版本号和专有名词。项目把 1024 维长期记忆向量保存在 PostgreSQL 的 pgvector 中，用 HNSW 加速余弦召回，同时保留 FTS 与 `pg_trgm`。二者共同命中时形成 hybrid 结果；Embedding 未配置、Profile 不匹配、调用失败或语义分数低时转为词法降级，并记录 retrieval mode、fallback reason、候选数和覆盖率。这样既扩展语义覆盖，也不让向量服务故障阻断正常对话。

### 3.11 深度研究是怎么实现的？

**第一人称口播：** 我把深度研究设计为证据优先的多阶段流水线。模型先根据主题生成章节和多个 Query，Retriever 并发取回网页或本地来源；程序按 canonical URL 和 content hash 去重并过滤过短内容。随后模型逐来源提炼带 `source_id` 和 relevance 的 Evidence。若覆盖不足，只允许基于已有证据和已用 Query 反思并补搜一轮。接着为每个章节分配 Evidence ID，分节写作时只能使用这些证据，引用标记统一解析为来源序号。最终做章节覆盖、来源存在性、未知引用和主题要求审计，必要时基于现有证据修复一次，仍不通过就明确阻断。

**追问 1：并发和超时怎么控制？** 多 Query 检索和逐来源提炼都通过 `asyncio.Semaphore(4)` 限制并发，避免一次研究把供应商或本机资源打满。单次检索、提炼、普通模型阶段和 repair 阶段分别设置超时；取消事件会与正在执行的检索任务竞争，收到取消后主动 cancel 并收集异常，防止后台继续消耗。部分 Query 失败会记录 diagnostics，只要仍有足够来源可以继续；如果最终没有章节、来源或 Evidence，则抛出证据不足，而不是生成看似完整的空壳报告。

**追问 2：如何防止引用幻觉？** 写作 Prompt 不是唯一防线。每条 Evidence 都绑定已接纳 Source ID，章节只拿分配给自己的 Evidence。正文使用受限引用标记，渲染时会检查 ID 是否属于当前来源闭包，未知 ID 直接抛错。摘要和核心要点若没有合法引用会被替换或丢弃。最终确定性审计要求每个章节正文至少有合法引用，主题显式要求也必须由章节覆盖。模型审计失败后，repair 只能看到缺口和已有 Evidence，不能凭空引入新来源。这样引用正确性主要由程序约束，而不是相信模型自觉。

### 3.12 Multi-Agent 协作是怎么实现的？

**第一人称口播：** 我采用 Coordinator 加三个只读专家的受控协作，而不是多个 Agent 共享一段聊天记录互相自由发消息。Coordinator 第一次领取任务后基于同一个不可变 Context Snapshot fan-out 出 Researcher、Planner 和 Critic 子任务，每个子任务有稳定 child key、角色、目标、输出 schema 和预算单位。Worker 分别执行并提交结构化 Artifact。全部子任务进入终态后，父 Coordinator 被重新排队，读取成功 Artifact 和失败角色，生成综合结论。专家不能直接写 Plan、Memory 或安全配置，最终权威状态仍由主 Runtime 和用户审批控制，从架构上减少写冲突和权限扩散。

**追问 1：为什么不是让 Agent 互相对话？** 自由对话看起来灵活，但消息会指数增长，容易出现循环、角色漂移、Prompt 注入传播和共享状态覆盖，也很难恢复到确定位置。当前任务更适合数据流模型：父任务给不可变输入，子任务返回 schema 化 Artifact，Coordinator fan-in。这样每个结果都可校验、可缓存、可独立重试，失败角色也能明确标识。若未来需要辩论，可以把“上一轮 Artifact”作为下一轮显式输入，而不是开放无限制聊天室，仍然保留轮次、预算和权限边界。

**追问 2：部分专家失败怎么办？** Join Policy 支持 ALL_SUCCESS 和 ALL_DONE。默认专家协作使用 ALL_DONE，意味着所有子任务进入终态后 Coordinator 就能继续，成功 Artifact 被汇总，失败角色放入 `failed_roles`，最终结果标记 incomplete；只有全部专家失败才让整体失败。对于安全敏感、必须全员通过的任务可选 ALL_SUCCESS，任一子任务失败就阻断父任务。关键是降级语义公开，不把少一个 Critic 的结果包装成完整共识，同时事件中保留每个子任务的状态和错误码。

### 3.13 Lease、Heartbeat 和 Epoch Fencing 分别解决什么问题？

**第一人称口播：** Lease 解决任务独占的时间边界：Worker claim 后拥有到 `lease_until`，过期即可接管。Heartbeat 解决长任务正常执行时租约会自然过期的问题，Worker 周期性续约并更新 attempt 心跳。Epoch Fencing 解决旧 Worker 复活的问题：每次重新 claim 都把 `lease_epoch` 加一，所有 Checkpoint、完成、失败和 Artifact 提交必须同时匹配 owner、未过期 lease 和 epoch。即使旧 Worker 因网络暂停后恢复，它持有的 epoch 已落后，提交会被拒绝并只记录结果 Hash。这三者组合才能真正避免脑裂写入，单靠状态字段或 owner 字符串都不够。

**追问 1：为什么 Heartbeat 不能替代 Epoch？** Heartbeat 只能说明某个 Worker 最近还活着，无法阻止网络分区后的旧 Worker 在租约过期、任务已被别人接管后迟到提交。如果只检查 owner，owner 重用或状态竞争也可能误通过；只检查时间，客户端时钟和事务时序也会带来边界问题。Epoch 是数据库在每次所有权变更时单调增加的 fencing token，新 owner 永远持有更大值。权威写入以当前行中的 epoch 为准，旧执行者无论何时回来都无法跨越这个代际边界。

**追问 2：Fan-out 重试会不会重复创建子任务？** Fan-out 不是简单循环 insert。父任务下的每个子任务有唯一 child key，服务在事务中先读取已有 children，并把角色、目标、schema、priority、budget 和 join policy 与本次请求规范化比较。完全一致就返回原任务，实现幂等重放；同一 child key 但 payload 改变则报冲突，不会悄悄产生两套含义。系统还限制最大子任务数、最大深度和总预算单位，避免 Coordinator 重试或模型异常造成任务树爆炸。

### 3.14 你说的自进化是怎么做的？会不会失控？

**第一人称口播：** 我更准确地称它为受控持续改进。Run 结束后，Experience Observer 只从可见事件、错误和结果中抽取模式，不读取隐藏思维链，也不直接改系统行为。相同失败模式达到条件后生成不可执行 Candidate，Candidate 指向基础 Bundle 和目标 Bundle，并冻结内容摘要、权限差异和各类 digest。随后用 baseline/candidate 同题配对、独立质量与安全 Judge、确定性规则和 Bootstrap CI 做离线评测。只有报告通过、人工审批绑定一致、Canary 样本和安全门禁满足后才能晋升 Stable；安全失败自动切回 champion，人工也能回滚。因此自动化负责发现和验证，发布权仍被门禁控制。

**追问 1：为什么 Candidate 不能直接生效？** 运行失败样本可能是偶发网络问题、用户输入异常或评测偏差，模型提出的修复也可能优化一个 case 却破坏其他能力。Candidate 只是一份不可执行提案，可以被审计和比较，不具备写安全策略或切换稳定通道的权限。审批还绑定 Candidate、评测报告、权限差异和目标 Bundle 的 digest，防止评测后内容被替换。这个设计把“生成建议”和“改变生产行为”分离，相当于软件发布中的代码变更、测试报告和部署审批分权。

**追问 2：Canary 如何分流和回滚？** 激活 Canary 后，通过稳定 assignment hash 将 Run 分配到 champion 或 challenger，同一 assignment key 保持稳定，Exposure 记录 cohort、Bundle、成功、安全和质量结果。达到最小 challenger 样本数，并在高门槛配置下同时满足 champion 对照样本、没有待定安全结论、没有安全失败和执行失败，才允许 promote。如果 challenger 出现明确安全失败，完成 Exposure 的事务会把 Canary 状态改为 rolled back，并将 canary/stable channel 指回 champion；回滚事件和原因也会持久化，保证恢复和审计一致。

### 3.15 这个项目最大的技术难点、边界和后续优化是什么？

**第一人称口播：** 最大难点不是写一个模型循环，而是在不确定模型、外部副作用和进程故障之间建立确定性边界。我重点解决了三类一致性：计划版本与执行状态一致，工具动作与审批/回执一致，模型调用与成本/评测账本一致。当前边界也很明确：这是 PostgreSQL 驱动的本地单用户系统，任务队列、租约和业务状态仍在同一服务内，不依赖 Redis/Celery；语义检索已经使用 pgvector HNSW，但规模与参数收益仍需真实数据验证；多 Agent 是受控只读专家，Evolution 只允许受约束的 Prompt 或路由策略候选，不会自主修改核心代码和安全策略。

**追问 1：如果扩展到多机，你先改哪里？** 当前 PostgreSQL 已经承担权威状态、队列、Lease、账本和事件，我会先把 Worker 进程和唤醒通道独立出来，而不是重做事实源。消息队列只负责降低轮询和跨机通知延迟，任务所有权仍由 PostgreSQL 的租约、行锁和 Epoch Fencing 决定，不能只依赖消息队列的 at-least-once 语义。随后再把大 Artifact 迁到对象存储并在数据库保留内容 Hash，补充分区、连接池、积压与接管时延指标。模型、成本和 Evolution 的不可变账本可以继续沿用。

**追问 2：你会怎样证明这些设计真的有效？** 我会分三层验证。第一层是确定性单测和不变量测试，覆盖非法迁移、旧版本冲突、审批参数变化、重复 Claim、账本配平和旧 epoch 拒绝。第二层是故障注入，在模型首 Token、工具副作用、回执提交、Checkpoint 和 Worker Heartbeat 前后杀进程，测恢复成功率、重复副作用率和接管时延。第三层是真实 workload 评测，记录 Invocation/Attempt 的成功率、fallback、TTFT、P95、成本，以及记忆 Recall@K、研究引用正确率和 Canary 安全结果。没有这些数据前，我只陈述代码可验证的机制，不宣称线上百分比收益。

## 4. 源码证据索引

| 主题 | 关键路径与内部符号 | 对应问题 |
| --- | --- | --- |
| 状态机 | `backend/app/domain.py`：`AgentState`、`StateMachine.transition` | 3.1、3.4 |
| 计划版本 | `backend/app/domain.py`：`PlanVersionService.create/revise/approve` | 3.1、3.4 |
| ReAct Runtime | `backend/app/runtime.py`：`AgentRuntime._execute_locked/_execute_step`、`RuntimeConfig` | 3.2、3.3 |
| Checkpoint | `backend/app/domain.py`：`CheckpointStore`；`runtime.py`：`_save_checkpoint` | 3.5 |
| 工具授权与 Claim | `backend/app/tools.py`：`ToolRegistry.authorize/_claim_execution/_mark_reconciliation` | 3.5 |
| 审批绑定 | `backend/app/domain.py`：`ApprovalService`、`normalized_params_hash` | 3.5 |
| 路由与账本 | `backend/app/model_control.py`：`ModelRouter`、`ModelControlStore`、`RoutedModelGateway` | 3.6 |
| 供应商调用 | `backend/app/model_gateway.py`：`ModelGateway`、`GatewayError` | 3.6 |
| 成本流水 | `backend/app/costs.py`：`CostService.reserve_attempt/settle_attempt` | 3.7 |
| 配对评测 | `backend/app/real_evaluation.py`：`evaluate_paired`、`_paired_statistics`、`_bootstrap_ci` | 3.8 |
| 三层记忆 | `backend/app/memory_v2.py`：`MemoryStore`、`MemoryContextProvider` | 3.9、3.10 |
| 向量索引与降级 | `backend/app/embedding_worker.py`；`backend/alembic/versions/20260905_0001_enable_postgres_extensions.py`；`MemoryContextProvider.select` | 3.9、3.10 |
| 经历归档 | `backend/app/memory_archive.py`：`ConversationArchiver` | 3.9 |
| Context 组装 | `backend/app/context.py`：`ContextAssembler` | 3.10 |
| 深度研究 | `backend/app/research/engine.py`：`ResearchEngine.run_research/_retrieve/_distill` | 3.11 |
| 多 Agent | `backend/app/agents.py`：`AgentTaskService`、`ManagedAgentWorker` | 3.12、3.13 |
| Experience | `backend/app/experience_observer.py`：`ExperienceObserver` | 3.14 |
| Evolution | `backend/app/evolution.py`：`EvolutionService`、`EvolutionCandidateGenerator` | 3.14 |

## 5. 高风险 Claim 清单

| Claim 类型 | 风险点 | 面试前必须补齐 |
| --- | --- | --- |
| Ownership | “自研、建设、构建、设计”可能被理解为个人端到端主导 | 明确本人负责模块、协作者和设计决策边界 |
| Metric | 当前描述没有线上质量、延迟、成本下降百分比 | 准备真实测试报告或坚持使用“待测/代码已支持” |
| Architecture | Checkpoint、Fencing、成本账本容易被追问事务窗口 | 能画正常时序、崩溃窗口和恢复路径 |
| Result | “实现中断恢复/去重”不等于所有外部工具 exactly-once | 明确 at-least-once、幂等键和 reconciliation 边界 |
| Evolution | “自进化”容易被理解为自动改代码并上线 | 主动表述为受控 Candidate、评测、审批、Canary、回滚 |
| Retrieval | 已接入 pgvector HNSW，但缺少真实 Recall@K、延迟和索引调参结果 | 准确表述为“HNSW 语义召回优先，FTS/`pg_trgm` 降级”，不要虚构检索指标 |

## 6. 面试前最后检查

- 能在白板上画出完整状态机和一次工具调用时序。
- 能区分 ReAct、React、Plan、State Machine 四个概念。
- 能解释为什么副作用不确定时选择阻断而不是重试。
- 能手写 Invocation/Attempt 与 RESERVE/CHARGE/RELEASE 的关系。
- 能说明记忆写入门槛、Scope 顺序和 Context Pin。
- 能说明深度研究的引用闭包和覆盖审计。
- 能用网络分区例子解释 Epoch Fencing。
- 能说明配对评测、盲评、Bootstrap CI 和安全门禁。
- 能主动讲清当前边界，不把设计方案夸大为生产规模结论。
