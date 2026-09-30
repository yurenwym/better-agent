# HarnessExecutionContext 第一阶段开发任务与验收

日期：2026-09-30

代码核对基线：`733866b`
文档状态：待开发。下文接口、字段和测试均为实施要求，不表示代码已经具备。

## 1. 目标与范围

只打通以下最小链路：

```text
对话 Turn 的可信执行上下文
  └─ LLM 调用 Context
       └─ Tool 调用 Context（可能等待审批）
            └─ Tool 内部 LLM 调用 Context
```

新增 HarnessExecutionContext，为 ModelCallContext、ToolExecutionContext 提供统一来源；保持现有业务返回、审批规则、预算计费、工具幂等与恢复行为。

本阶段必须交付：

1. 不可变的公共 Context，含 root_task_id。
2. create_root_context()、create_child_context() 与两个 from_harness() 转换 API。
3. 对话、工具、工具内部 LLM 的 Context 传递。
4. 审批前持久化原工具 Context，重启后可恢复，并重新验证当前授权。
5. 必要的持久化迁移、关联记录及自动化测试。

本阶段不做：PolicyEngine、TaskRuntime、ToolRegistry 职责拆分；Tool Search；DAG；通用 Sub-Agent Tool；统一 Event Envelope；完整 ContextSnapshot；全量 Research/Learning/Goal Review 迁移；可观测平台、采样器和 Trace UI；JEV 预算调整。

允许在现有工具执行入口增加 Context 参数传递或字段校验，但不得借此重写 ToolRegistry 的鉴权、执行和 reconciliation 流程。

## 2. 当前代码接入点

| 文件 | 当前实现 | 本阶段动作 |
|---|---|---|
| backend/app/conversation.py | 多处直接创建 ModelCallContext，对话 worker 设置 gateway Context | 优先改 route_and_respond 路径及其必要续接路径 |
| backend/app/model_control.py | ModelCallContext；child_call_context；模型调用入库 | 添加公共 Context 适配与关联信息；避免重复创建 span |
| backend/app/model_gateway.py | complete() 从显式参数或 ContextVar 获取上下文 | 确认显式/隐式调用均正确传递，调用结束恢复 ContextVar |
| backend/app/tools.py | ToolExecutionContext；execute_async 接收可信 Context | 添加 from_harness 与必要透传，保持执行语义 |
| backend/app/chat_tools.py | context()、_request_approval()、resume()；ChatToolCallStore | 同一次调用复用 Context；审批前保存，恢复时读取 |
| backend/app/goal_tools.py | _preview_goal_plan 将预算与 bundle 传给计划服务 | 同时传递工具派生的 Context |
| backend/app/goal_programs.py | preview()、_model_context() 构造编译调用身份 | 已有工具 Context 时不得另建身份、预算根或版本 |
| backend/app/db.py、backend/alembic/versions/ | SQLite 测试迁移和 PostgreSQL Alembic | 增量增加所需记录字段，不能修改历史迁移 |

注意：普通对话没有必然对应的 AgentTask；agent_runs 与其他 run 也不是同一命名空间。不能为了满足新字段人为创建任务或混用 ID。

## 3. 数据契约

建议新增 backend/app/execution_context.py，具体模块名可按仓库风格调整。

```python
@dataclass(frozen=True)
class HarnessExecutionContext:
    owner_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    root_budget_id: str | None = None
    runtime_bundle_id: str | None = None
    project_id: str | None = None
```

规则：

- owner_id 非空，由服务端已验证身份提供；新链路不得静默回退为 local-user。
- trace/span 使用服务端生成的 UUID hex 即可；不要求接入外部 tracing SDK。
- 普通对话没有 task 时，task_id、parent_task_id、root_task_id 均可为空。
- 有真实根任务时 root_task_id 等于根 task_id；后代继承，不能使用预算 ID 或 turn ID 代替。
- trace 树与任务树相互独立：一次模型子调用只增加 span，不意味着创建 task。
- runtime_bundle_id 和 root_budget_id 在子调用中原样继承；首次绑定由原有服务负责。
- 项目、会话、turn、run 的引用按入口语义校验；审批续接不能把原工具调用的 turn/run 换成 continuation turn/run。
- None 仅表示该字段确实不适用。生产对话原本要求预算或版本绑定时，不得借可空类型绕过要求。
- 对象不包含密钥、审批凭证、完整消息、数据库连接或服务实例。
- RunSnapshot 继续负责状态与检查点；ContextSnapshot 继续表示模型输入。本阶段不将它们合并。

## 4. API 与调用边界

### 4.1 创建根与子 Context

```python
create_root_context(*, owner_id, thread_id=None, turn_id=None,
                    run_id=None, task_id=None, project_id=None,
                    root_budget_id=None, runtime_bundle_id=None)
create_child_context(parent: HarnessExecutionContext)
```

- create_root_context：只在可信入口调用；生成 trace_id、span_id，parent_span_id 为空；task_id 存在时绑定 root_task_id。
- create_child_context：所有业务身份字段及 trace_id 原样继承；仅生成新 span_id，并令 parent_span_id = parent.span_id。
- 第一阶段不需要业务侧传入子 task 覆盖参数。真实 Sub-Agent 任务创建 API 留到后续阶段。
- 不提供通用 **overrides 或从用户 JSON 直接创建身份的接口。
- Python dataclass 不作为安全沙箱；通过调用边界、工厂校验、代码检查和测试约束业务用法。

### 4.2 类型适配

```python
ModelCallContext.from_harness(harness, *, role, purpose,
                              invocation_id=None, idempotency_key=None, ...)
ToolExecutionContext.from_harness(harness, *, tool_call_id)
```

- 两个 from_harness 都是转换，不生成 trace/span，不创建任务或预算。
- role、purpose、tool_call_id、invocation_id 等专用字段仍保留在各自类型中。
- 转换 API 不接受 owner、预算根、bundle 或 trace 覆盖参数。
- 可通过持有 harness 字段和只读访问器实现兼容；如果保留旧字段，必须检查双份字段一致，不能出现两个身份来源。
- 原有非本阶段调用者可保留旧构造入口，但最小链路不得继续手工拼装公共身份。

### 4.3 span 的唯一创建责任

```python
turn_ctx = load_or_create_turn_context(turn)
llm_ctx = create_child_context(turn_ctx)
model_ctx = ModelCallContext.from_harness(llm_ctx, role='conversation', purpose='route_and_respond')
# 模型选定一次工具调用后：
tool_ctx = create_child_context(llm_ctx)
execution_ctx = ToolExecutionContext.from_harness(tool_ctx, tool_call_id=call.id)
# 工具内部需要编译计划：
compile_ctx = create_child_context(tool_ctx)
compile_model_ctx = ModelCallContext.from_harness(compile_ctx, role='planner', purpose='compile_goal_program')
```

同一次逻辑调用只创建一次 Context。context(call_id)、_request_approval()、execute() 不得各自生成一份 span；仅内存缓存不足以支持进程重启。

- gateway 与 routed gateway 只消费已创建的 Context，不能每经过一层包装就创建一个 span。
- 现有 child_call_context() 的 role/purpose 适配不得变成隐式重复建 span 的地方。
- 两次真正独立的模型调用即使 role/purpose 相同，也有不同 span。
- 同一 invocation 内网络重试保留该逻辑调用 span，attempt 仍用现有标识区分。
- 工具结果回到对话后的下一次模型调用从 turn Context 创建新 span；上一轮工具关联保留在现有调用记录中，本阶段不构建 DAG trace。

## 5. 持久化与审批恢复

建议采用现有记录的增量字段，避免新建通用任务平台：

| 记录 | 建议新增 | 用途 |
|---|---|---|
| turns | execution_context_json + execution_context_digest，历史记录可空 | 固定对话根 Context，重启不换 trace |
| turn_tool_calls | execution_context_json + execution_context_digest，历史记录可空 | 固定原工具调用 Context，与 tool_call_id 关联 |
| model_invocations | execution_context_json + execution_context_digest，历史记录可空 | 核对每次模型调用所属 Context |

以上 JSON 使用版本化 envelope：schema_version + context；不得把授权 binding 与执行身份混为一份可随意修改的数据。开发前核实实际表名和所有 INSERT/SELECT、导出及快照读取入口；保持对旧记录的兼容。

### 5.1 摘要与不可覆盖约束

三个表统一保存 execution_context_json 和 execution_context_digest。摘要由服务端统一函数计算：

```python
# envelope 经过 schema 校验；所有可选字段显式保留为 null。
canonical = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
execution_context_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

- 摘要覆盖完整 schema_version + context，不含摘要字段本身；不含审批状态、resume 时间或授权 binding。
- 相同结构的 JSON 不因空白或键顺序变化而改变摘要；不同 schema 版本或字段值必须重新计算。
- 新记录的 JSON 与 digest 必须在同一事务写入，两者不可一空一非空；历史记录允许两者同时为空。
- 写入、幂等比较及恢复读取均校验摘要；拒绝外部提交的摘要作为可信结果。
- 同一调用 Context 写入后不可覆盖成另一份；重复写入先验证原 JSON/digest 和字段绑定，再比较新旧摘要及规范化内容。相同则幂等，不同则冲突，不能采用最后写入者覆盖。
- 摘要用于一致性、审计和幂等比较，不是签名或授权凭证，不能代替用户权限与数据库关联校验。
- execution_context_digest 与已有 context_snapshot_digest 含义不同：前者标识执行身份与关联，后者标识模型输入上下文；不得相互替代。

### 5.2 JSON 与既有字段的一致性

所有双份身份字段必须在写入及恢复读取时检查，任意冲突直接拒绝，不自动修复 JSON 或覆盖数据库列。空值也参与比较，不把空字符串当作 None。

| 记录 | 必须一致的绑定 |
|---|---|
| model_invocations | context.owner_id/run_id/thread_id/turn_id/runtime_bundle_id/root_budget_id 分别等于同名列；context.task_id 按明确映射等于 agent_task_id |
| turns | context.turn_id 等于本行 id；context.thread_id 等于 thread_id；owner 从所属 thread 的可信记录校验；预算、bundle 与已有绑定列一致 |
| turn_tool_calls | context.turn_id/thread_id 等于同名列；context.run_id 等于原 turn 对应的 chat_run_id；owner、预算、bundle 通过原 turn/thread 校验；工具调用 ID 由本行 id 与 ToolExecutionContext.tool_call_id 绑定 |

不存在对应列的字段通过可信关联记录校验，不为凑齐同名字段扩展新的任务平台。root_task_id 等预留字段遵守第 3 节不变量；本阶段不能据此宣称已验证未来跨 Runtime 的任务树。

插入时从同一份已校验 Context 派生 JSON、digest 和对应列；调用方提供了冲突的旧参数也必须拒绝。现有列分多条 SQL 更新时，整个写入必须处于同一事务，完成一致性检查后才可提交或发起外部调用。

### 5.3 审批恢复与逻辑 span

这里的 span_id 标识 Tool 的逻辑调用身份，不要求一个同步计时 span 或进程资源在等待人工审批期间持续存活。

```text
原 Tool logical context 不变
  trace_id / tool span_id / parent_span_id 不变
审批恢复动作
  可记录 resume event，关联原 tool_call_id、trace_id、span_id
后续真正新增的内部 LLM 调用
  新建 child span，parent_span_id = 原 tool span_id
```

resume event 不修改原 Context 和 digest；复用已有模型结果不创建新的 LLM span。将来接入 OpenTelemetry 等系统时，实际执行片段的计时 span 与持久化逻辑身份的映射另行定义，不将审批等待时间误报为同步工具执行耗时。本阶段不引入该映射或 SDK。


审批恢复顺序：

1. 按可信用户范围加载原调用、原 turn/thread、审批记录和持久化 Context。
2. 校验 schema 版本、必填字段、重算的 execution_context_digest，以及第 5.2 节全部列值和关联绑定。
3. 校验预算根、bundle 与原始执行绑定相符；存在但失效时不得替换成新预算或当前稳定版本。
4. 校验工具名、规范化参数 hash、原审批 binding；MCP 仍执行现有 server/config/definition 校验。
5. 按当前状态重新检查工具可用性、权限和审批有效性，以及原有执行准入限制。
6. 通过 ToolExecutionContext.from_harness 恢复原 Tool logical context，保留原 trace/span/parent span 及 digest；恢复动作可追加 resume event。
7. 进入现有幂等执行/结果恢复路径；工具内部新发生的 LLM 调用再创建子 span。

禁止用当前 continuation turn 的预算、版本或身份填补原记录；原始工具 Context 不因恢复动作发生变化。

历史数据策略：

- 历史完成记录的 Context 允许为空；历史只读查看继续可用。
- 新链路创建的记录必须同时写入 Context 和 digest；任一写入失败不得进入可执行的待审批状态。
- 对旧的待审批调用，默认保守拒绝执行并提示重新发起，建议错误码 LEGACY_CONTEXT_MISSING。
- 不通过临时拼装 Context 自动兼容旧审批；如将来要恢复旧审批，另立经过证明的迁移任务。
- 未识别版本、损坏 JSON、身份冲突分别提供明确失败原因，不回退到默认 owner。

## 6. 逐步开发任务

任务顺序：T00 → T01 → T02 → T03 → T04 → T05 → T06 → T07 → T08。

### T00：冻结最小链路和现有行为

- [ ] 枚举 route_and_respond → ChatToolExecutor → 计划 preview/compile 的全部 Context 创建位置。
- [ ] 记录普通工具、需审批 WRITE 工具、工具内部 LLM 各一条调用链。
- [ ] 核实当前 migrations head、表字段和实际测试环境；不要使用开发数据库跑集成迁移。
- [ ] 运行现有相关测试，保存通过/失败基线，区分历史问题。

交付：接入点清单与测试基线。验收：能说明每个边界是谁创建身份、谁保存、谁恢复。

### T01：新增公共 Context 和工厂 API

- [ ] 实现不可变数据结构、root/child 创建 API、版本化序列化及可信记录反序列化。
- [ ] 实现字段和继承不变量校验；拒绝未知格式和身份注入。
- [ ] 明确没有 task 时三项 task 字段为空；普通子调用保留任务身份。

交付：execution_context.py 与单元测试。验收：U01～U06 通过。

### T02：添加类型适配并保留旧接口兼容

- [ ] ModelCallContext.from_harness 与 ToolExecutionContext.from_harness。
- [ ] 保留 role/purpose、调用 ID、授权等已有专用语义。
- [ ] 整理 child_call_context 与 gateway 包装层，保证适配不额外创建 span。
- [ ] 公共 Context 与旧字段并存时，发生冲突立即拒绝。

交付：两个适配 API 与网关兼容测试。验收：U07～U10；原有模型路由/工具测试无新增失败。

### T03：增加最小持久化支持

- [ ] 增量 Alembic migration；同步 SQLite 测试迁移和 PostgreSQL schema head。
- [ ] turn、工具调用、模型调用同时读写 Context 与 execution_context_digest；统一规范化摘要函数，快照转换可读旧记录。
- [ ] 实现第 5.2 节的 JSON/数据库列/关联一致性校验，覆盖写入和恢复读取；拒绝任一字段冲突。
- [ ] 同一调用重复写入时验证原 digest，再比较规范化内容与摘要；相同 Context 幂等，不同 Context 拒绝。
- [ ] 审批和工具调用的保存使用一致事务；失败时不得留下可执行的孤立审批。
- [ ] 对公共 API 的新字段做明确选择，避免通过序列化顺带暴露内部授权数据。

交付：迁移和存储读写测试。验收：P01～P04。

### T04：接入对话 → LLM → Tool

- [ ] 从可信 turn/thread、既有 budget/bundle 记录创建并持久化 turn Context。
- [ ] 对话中的每次逻辑 LLM 调用派生子 Context。
- [ ] 模型输出工具调用后创建工具 Context，并在执行/申请审批之前固定保存。
- [ ] context(call_id)、执行与审批共用同一份记录。
- [ ] 显式 Context 优先；使用 ContextVar 时必须 try/finally reset，并覆盖异常和取消。

交付：READ 工具完整链路。验收：I01、I02、I04、I05。

### T05：接入 Tool 内部 LLM

- [ ] 在 goal_tools → GoalProgramService.preview → 编译调用传递可信 Context。
- [ ] 内部 LLM 从工具 Context 创建子 span；通过 from_harness 生成 ModelCallContext。
- [ ] 有原预算根时不得创建 goal_operation 等替代预算根。
- [ ] 保留原 bundle；验证实际请求路由使用该绑定，而非只给日志填入旧版本 ID。
- [ ] 独立发起的非工具计划编译入口维持原行为，不强制全项目迁移。

交付：真实服务路径、受控模型响应的集成测试。验收：I03、I04、I06。

### T06：审批恢复原 Context

- [ ] 按第 5 节顺序改造 resume()，不从当前执行器重新拼装身份。
- [ ] 进程重启后只依赖数据库原记录恢复；保留原 run/turn/trace/span/parent span 和 digest；resume event 与原逻辑 Context 分离。
- [ ] 当前权限撤销、工具禁用、MCP 定义变更时维持原拒绝规则。
- [ ] 旧审批无 Context、记录损坏或绑定冲突时明确失败。
- [ ] 保留已有结果复用、执行 claim 和 UNKNOWN/reconciliation 行为；不为恢复增加自动重试。

交付：审批重启恢复与拒绝路径测试。验收：A01～A09。

### T07：完整回归与证据整理

- [ ] 跑新增单元测试、服务集成测试、隔离 PostgreSQL 测试。
- [ ] 跑既有 Chat/Goal Tool 审批、恢复、模型路由和预算继承回归。
- [ ] 用固定响应模型驱动真实业务链，检查数据库中每一层 Context；不得用伪造链路替代。
- [ ] 可选真实模型 smoke；单独标注环境和结果，不将其设为开发验收的必要条件。
- [ ] 保存测试命令、退出码、结果、DB schema head 与 commit；所有未执行项写明原因。

交付：docs/acceptance/harness-context-phase1/ 下的验收报告。验收：第 9 节门禁全部满足。

### T08：复核范围与交付

- [ ] 检查最小链路已不存在手工拼装公共身份的生产代码。
- [ ] 检查其他 Runtime 未被顺带迁移，未新增 PolicyEngine/TaskRuntime 重构。
- [ ] 更新接入说明、历史待审批兼容策略和恢复操作说明。
- [ ] 交付代码差异、迁移、测试结果和明确剩余范围。

交付：可评审改动。验收：无需依赖下一阶段重构即可独立运行。

## 7. 测试用例

### 7.1 Context 与适配单元测试

建议新增 backend/tests/test_execution_context.py。

| ID | 输入与操作 | 预期断言 |
|---|---|---|
| U01 | 同一 owner 创建两个根 | trace/span 均不同；parent span 为空 |
| U02 | 从根派生子，再派生孙 | owner、trace、预算、bundle、root_task 一致；每层新 span；父指针正确 |
| U03 | 普通对话无 task | task/parent_task/root_task 都为空，不制造任务 |
| U04 | 根有 task_id | root_task_id 等于根任务；子 span 不改变 task 树 |
| U05 | 空 owner、外部覆盖身份、格式错误 | 明确拒绝；不回退默认 owner |
| U06 | 序列化后恢复；改变 JSON 空白/键序；修改字段；未知 schema | 合法字段保持一致；空白/键序不改变 digest；修改字段改变 digest；未知版本拒绝 |
| U07 | 同一 harness 转换为 Model/Tool Context | span/trace 不变，专用字段正确 |
| U08 | 转换时尝试覆盖预算、owner、bundle | API 不接受或显式拒绝 |
| U09 | 同 role/purpose 两次独立模型调用 | 两个 span；一次 invocation 内网络重试不新建逻辑 span |
| U10 | routed gateway → direct gateway 包装 | 公共字段不丢失，无额外伪造 span；旧调用兼容 |

### 7.2 业务链集成测试

建议新增 backend/tests/test_harness_context_flow.py，复用已有服务 fixtures 与模型 stub。

| ID | 场景与步骤 | 预期断言 |
|---|---|---|
| I01 | 新对话 → LLM → READ Tool | 记录 trace 一致；LLM 为 turn 子 span，Tool 为 LLM 子 span；业务结果正确 |
| I02 | 同轮调用两个工具，再调用 LLM | 两工具 span 不同且父 LLM 相同；下一次 LLM 新 span；预算根一致 |
| I03 | 调用 activate_goal_plan 的 preview/compile 路径 | 内部模型 span 的父节点为工具；owner、预算、bundle 一致；无新预算根 |
| I04 | 两个 owner 同时运行，中间强制交错 await | 输出和记录互不串身份、预算、版本、trace |
| I05 | 模型或工具抛错/取消，再执行另一请求 | ContextVar 已 reset；第二请求不会继承前一个请求 |
| I06 | 起始绑定 bundle A，随后 stable 切 B，再调用内部模型 | 内部请求仍使用 A 的适用配置；审计也记录 A |

### 7.3 审批与故障恢复测试

扩展 test_chat_goal_tools.py、test_goal_tool_recovery.py；关键用例同时在 PostgreSQL 运行。

| ID | 场景与步骤 | 预期断言 |
|---|---|---|
| A01 | WRITE 申请审批后保存记录，重建服务实例再批准；记录 resume event；随后调用内部 LLM | 原 Context、digest、tool_call_id、turn/run、trace/span/parent span 不变；resume event 关联原调用；新内部 LLM 创建 child span；业务副作用一次 |
| A02 | continuation 环境携带另一 budget/bundle/trace | 原调用仍读取持久化原 Context，不接受新值覆盖 |
| A03 | 等待期间撤销工具权限或禁用工具 | 恢复被拒绝；handler 和内部 LLM 均未调用 |
| A04 | 修改 params、调用关联或 MCP 定义版本 | hash/binding/关联校验拒绝；零业务副作用 |
| A05 | 另一 owner 尝试批准或恢复 | 拒绝，不能读取或执行其他 owner 的原调用 |
| A06 | 原预算已失效；原 bundle 不可用 | 依原有准入规则停止；不创建替代根、不降级到 stable |
| A07 | 旧待审批记录缺少 Context；JSON 损坏或版本未知 | 明确错误，不执行；旧完成记录仍可查看 |
| A08 | 同一审批重复恢复；执行后返回前崩溃 | 复用既有结果/claim/recover_result；不重复写入 |
| A09 | WRITE 超时且提交结果不明确 | 进入原 UNKNOWN/reconciliation 路径；不因新 Context 自动重试 |

### 7.4 数据库测试

建议新增 backend/tests/integration/test_harness_context_postgres.py。

| ID | 操作 | 预期断言 |
|---|---|---|
| P01 | 从现有 head 升级、空库升级 | 两条路径成功；旧数据可读；schema head 一致 |
| P02 | 保存并重读三个表的 Context/digest；检查旧记录及单边为空 | 摘要可重算、字段与 schema 完整；旧记录两者同时为空可读；新记录单边为空拒绝 |
| P03 | 参数化覆盖三个表：同调用并发保存相同/不同 Context；逐一传入与 JSON 冲突的既有列值或可信关联；包括 null/非 null 冲突 | 原 digest 有效且规范化内容相同才幂等；不同 Context 或任一第 5.2 节绑定冲突立即拒绝；不静默覆盖、不产生部分记录 |
| P04 | 审批/JSON/digest 写入中注入失败；读取恢复前分别篡改 JSON、digest、既有列；再构造 JSON 与 digest 同步修改但与原记录绑定冲突 | 事务失败无可执行孤立审批；摘要不符拒绝；即使摘要匹配，第 5.2 节任一列值或关联冲突仍拒绝；工具和内部 LLM 调用数为零；合法重试绑定唯一 |

单元测试使用 stub 是验证 Context 传播，不得标注为真实模型效果评测。关键数据库测试必须经过隔离测试库 guard，禁止使用开发/生产库。

## 8. 推荐执行命令

从 backend 目录执行。以下含计划新增测试文件，开发完成后才可执行；实际环境变量和测试库配置遵循仓库既有 integration fixture。

```powershell
python -m pytest tests/test_execution_context.py tests/test_harness_context_flow.py -q
python -m pytest tests/test_model_control.py tests/test_model_gateway.py tests/test_routed_model_gateway.py -q
python -m pytest tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py -q
python -m pytest tests/test_conversation.py tests/test_conversation_worker.py tests/test_conversation_protocol_v2.py -q
python -m pytest tests/integration/test_harness_context_postgres.py tests/integration/test_chat_goal_tools_postgres.py tests/integration/test_goal_tool_recovery_postgres.py tests/integration/test_root_task_budgets.py -q
```

记录 passed/failed/skipped，不能把因数据库未配置而跳过的测试算作通过。不要为了文档中的命令调整现有测试库安全限制。

## 9. 最终验收门禁

- [ ] U01～U10、I01～I06、A01～A09、P01～P04 均有对应自动化用例且通过。
- [ ] 新链路每次 LLM 与 Tool 的公共身份字段均可从持久化记录验证；JSON/digest 成对保存、重算一致，JSON 与已有列及原记录关联全部一致。
- [ ] trace 内 span 无重复、无自指；每个非根 span 的父节点可定位。
- [ ] 多用户并发无串上下文；外部参数不能影响可信身份。
- [ ] 审批重启恢复不换 owner、原 turn/run、budget、bundle 或 trace lineage。
- [ ] 当前权限撤销仍能阻止执行，恢复原 Context 不等于恢复旧授权。
- [ ] WRITE 幂等、预算继承、超时 reconciliation、模型路由无新增回归。
- [ ] 迁移在隔离 PostgreSQL 验证，历史缺 Context 的待审批记录按明确策略处理。
- [ ] 不以本阶段完成宣称全项目统一 Context、完整 Trace 平台或全链路 ContextSnapshot 已完成。

## 10. 开发注意事项

1. 最危险的遗漏是只补 trace 字段，却仍在工具内部新建预算或读取当前默认 bundle；验收必须检查实际调用和账本。
2. trace_id 不代替幂等键，span_id 不代替 tool_call_id 或 invocation_id。
3. 原 Tool logical context 和 digest 在恢复时不变；resume event 单独记录，新增内部 LLM 才建 child span。逻辑 span 身份不等于跨审批等待持续运行的同步计时 span。
4. 旧入口兼容是过渡安排，应列出未迁移入口，禁止把默默回退当作已覆盖。
5. 本文仅是开发任务书；未经实现与实测，不把勾选项标为完成，也不修改既有验收结果。
