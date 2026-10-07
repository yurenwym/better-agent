# ContextSnapshot Phase 2A 修复任务与复验

日期：2026-10-06

状态：待修复、待复验。本文不表示修复已经实施或验收通过。

依据：[第二阶段开发任务书](ContextSnapshot-第二阶段开发任务与验收-2026-09-30.md)、[2026-10-06 独立复核报告](acceptance/context-snapshot-phase2/phase2a/review-2026-10-06.md)。复核基线为 HEAD `dcfc54e` 之后的未提交工作区，不是该 commit 本身已包含第二阶段实现。

## 1. 目标与范围

修复独立复核中已复现的三个问题，并补齐被现有测试遗漏的真实调用路径。完成后重新判断 Phase 2A / M1 是否通过。

| 问题 | 优先级 | 已复现行为 |
|---|---|---|
| F01：受控 direct 网关遗漏资产撤销检查 | P1 | 首次请求失败后撤销 bundle，第二次仍发送并成功 |
| F03：无 harness 时幂等身份比较不完整 | P2 | 相同幂等键、相同输入，将 run_id 从 A 改成 B，仍被判为重放 |
| F02：真实编译器 JSON 修复复用原 span | P2 | 两个 invocation、两个 snapshot，仅一个不同 span |

本轮仅修复 Phase 2A。保持快照与执行身份分离、核心三者原子提交、逻辑输入与 provider payload 分离、partial provenance 可验收等既定设计。不要顺带迁移 Phase 2B、重构 PolicyEngine/TaskRuntime，或调整 JEV 预算。

实施顺序：`R00 → R01 → R02 → R03 → R04 → R05 → R06 → R07 → R08`。

## 2. 具体任务

### R00：保留基线与补充失败测试

- 记录当前 HEAD、工作区 diff、测试环境及已有验收日志；保留历史失败记录。
- 把复核中的 F01/F02/F03 诊断转成自动化回归测试，先证明测试能捕获当前问题。
- 不修改原验收阈值，也不通过降低断言强度使测试通过。
- 验收：本文 D02、K02、S01 在修复前能分别定位三个缺陷；已有测试通过结果不作为缺陷不存在的证据。

### R01：抽取最小的发送前资产检查

改动位置：`backend/app/model_control.py`、`backend/app/model_gateway.py`；复用现有 `learning_assets` 和 pinned prompt 检查接口。

- 核对 routed 当前检查集合，区分必需资产检查与特定调用才适用的检查。
- 提供 direct/routed 共用的内部检查方法，以已绑定 invocation、owner、bundle 为输入，不重新解析最新 stable 或学习策略。
- 可选 Learning 服务未配置时，无此依赖的调用仍应工作；已经声明必需的检查不能失败后被忽略。
- 保留现有预算准入与计费逻辑，不在本任务设计通用策略引擎。
- 验收：D03、D04、D07；两个网关不各自维护一份可分化的撤销规则。

### R02：在 direct 每次发送前执行检查并结束失败调用

改动位置：`backend/app/model_gateway.py` 的 `complete()` attempt 循环，必要时同步 routed 的错误结束路径。

- 首次发送与每次 retry 均执行 R01 的检查；routed fallback 同样保留逐 attempt 检查。
- 不只在快照创建时检查一次。调用方的 attempt 回调若发生在检查之后、发送之前，应调整顺序或在回调后复查，避免回调撤销资产却仍发送。
- 检查失败不得重试或 fallback，不替换原快照，不发送网络请求。
- 如果尚未 start_attempt，结束 invocation 即可；如果 attempt 已创建，应按现有状态机结束该 attempt，并结束 invocation。不得留下无后续处理的 RUNNING/STARTED 状态或未释放的预算预留。
- 对瞬时检查完成后发生的外部并发撤销，沿用现有系统一致性边界；本任务不承诺数据库检查与远程 HTTP 之间具有跨系统原子性。
- 验收：D01～D07；同时断言发送次数、快照不变和最终状态，不能只断言服务抛异常。

### R03：明确幂等调用的身份比较契约

改动位置：`backend/app/model_control.py` 的 `_existing_invocation()`、`_resolve_existing_invocation()` 和身份比较辅助方法。

- 查询并比较现有持久化公共身份字段：owner_id、run_id、thread_id、turn_id、agent_task_id、root_budget_id、runtime_bundle_id，以及 role/purpose。
- 对所有字段严格比较，包括 null 与非 null；比较数据库别名时明确 agent_task_id 对应 task_id。
- 双方有 harness 时额外比较 execution_context_digest；仅一方有 harness 时拒绝作为相同调用重放。
- 双方无 harness 时仍必须逐字段比较，不能返回空摘要后跳过全部执行身份检查。
- 保留 request_digest 和 context_snapshot_digest 比较；不能仅因输入相同就合并不同执行身份。
- snapshot ID 是记录标识，不是内容相同的判定标准；新候选快照 ID 不同不应使完全相同的幂等请求误报冲突。
- 验收：K01～K04；定义不依赖业务调用方自觉传入完整 harness。

### R04：补齐幂等冲突、重放及并发测试

改动位置：`tests/test_model_input_snapshot_store.py`、`tests/integration/test_model_input_snapshot_postgres.py`。

- 对 R03 的身份字段做参数化测试，每次只改变一个字段；准备合法关联数据，确保失败来自身份冲突而不是外键。
- 分别覆盖 null→值、值→null、值 A→值 B。
- 验证完全相同的调用保持原有 InvocationReplayError 协议，不新增 invocation/snapshot、不发送。
- 真实 PostgreSQL 并发覆盖相同与不同身份；使用同步屏障形成重叠，不用顺序调用冒充并发。
- 验收：K01～K06；败者返回领域级 replay/conflict，不泄漏驱动唯一键错误，无孤立快照记录。

### R05：在编译器真实逻辑调用边界分配身份

改动位置：`backend/app/goal_program_compiler.py`、`backend/app/goal_programs.py`，必要时调整 `model_control.py` 的调用上下文辅助接口。

目标关系：

```text
原 Tool span
├─ 首次编译 span A → invocation A → snapshot A
│  ├─ attempt 1
│  └─ attempt 2（网络 retry；仍属于 A）
└─ JSON 修复 span B → invocation B → snapshot B
```

- Tool Context 作为可信父上下文；首次编译和修复分别派生 child span，不能从上一次 attempt 对象随意拼身份。
- 继承 owner、trace、budget、bundle 和任务关联；两次调用 parent_span_id 均指向原 Tool span。
- 新逻辑调用不能继承父调用的 invocation_id、idempotency_key、input_snapshot_id 或 context_snapshot_digest；如业务已有明确的持久化幂等协议，应为该逻辑调用派生稳定、独立的键。
- 使用明确的创建/派生 API 表达“新逻辑调用”，不再仅凭 purpose 是否改变推断边界。同 purpose 可以是不同调用。
- 网关内部网络 retry/fallback 不再创建逻辑调用身份，继续共享原 invocation、snapshot 和 span。
- 如调整 ambient ContextVar，必须通过 try/finally 恢复，异常不得污染后续调用。
- 保持无 harness 的旧独立编译入口兼容，不伪造工具身份，不改变其既有路由和授权语义。
- 验收：S01～S05，首轮合法输出路径也需回归。

### R06：使用真实业务路径补齐验收测试

改动位置：`tests/test_snapshot_flow.py`、`tests/test_snapshot_recovery.py`。

- I06 使用真实 GoalProgramCompiler 与生产 Context 绑定路径；网络返回可用测试桩，不能用自定义 _Compiler 替代被验证的身份分配代码。
- A05/A06 同时覆盖 direct/routed，在网关外观察网络次数及调用状态，不仅直接执行 assert_request_active。
- I05 通过真实 Memory/Skill 服务更新版本并走实际选择/组装入口，检查新旧快照正文、版本引用及旧快照摘要。
- Memory/Skill 更新遵循现有版本固定规则：使用新 turn/run 获取新版本；不要要求同一固定版本运行中自动切换资产。
- 保持 provenance=partial 可通过；已提供的引用应准确，不要求为了本次修复补齐全部片段定位。
- 验收：B01～B03，以及 D/S 对应测试。测试节点登记与实际断言分别审查。

### R07：复跑 Phase 2A 与相关回归

- 执行快照类型、存储、gateway、flow、recovery 专项。
- 执行第一阶段 harness 链路/审批、模型控制/direct/routed 网关、编译器和 GoalProgram 服务、对话/worker 相关回归。
- 在新建隔离 PostgreSQL 测试库运行快照及受影响的第一阶段持久化测试；先经过 db_target_guard，绝不使用开发库。
- 模型使用 mock transport/响应桩即可验证本轮契约，无需真实付费请求。
- 原报告中的租约用例本次独立复核已通过，应保留历史环境问题记录，但不能继续写成“当前必然无法执行”。
- 验收：记录命令、起止时间、退出码、passed/failed/skipped/deselected 数量。若仍有失败，先区分产品问题与环境问题；未经证据支持不得归因为环境后宣称全绿。

### R08：更新验收报告和交付状态

- 原报告暂标“Phase 2A 主体完成，M1 待复验”，追加独立复核及修复记录，保留历史结果。
- 修复后更新任务→用例→实际 pytest 节点映射，并逐条检查断言是否满足契约；节点可解析不等于语义覆盖完成。
- 更新 evidence JSON、脱敏链路样本，样本应能展示编译/修复两个 span 及 retry 共用同一快照。
- 记录本轮实际 commit 或未提交工作区状态；代码验收与 PR/合并状态分别报告，不将未提交描述为已交付。
- Phase 2B 仍标未实施/未验收，不把其管理端旁路当作本轮必须完成的任务。
- 验收：第 4 节全部满足后，才能更新 M1 为通过。

## 3. 新增/加强的测试用例

### 3.1 发送前撤销 D

| ID | 场景 | 必须断言 |
|---|---|---|
| D01 | direct：快照提交后、首次发送前撤销已使用资产 | 网络 0 次；invocation 正确结束；快照未改写 |
| D02 | direct：第一次 timeout 后撤销资产，再进入 retry | 网络总计 1 次；不得成功，不重试绕过撤销 |
| D03 | direct：资产始终有效，首次失败后重试成功 | 网络 2 次；一个 invocation/snapshot；输入一致 |
| D04 | 未配置可选 Learning 服务，调用无该检查依赖 | 正常保存快照并发送 |
| D05 | 检查报资产不一致或必需检查无法完成 | 零新增发送；领域错误明确；无悬挂 RUNNING/STARTED 或预算预留 |
| D06 | attempt 回调撤销资产 | 真正发送前被拦截；不能只检查回调前状态 |
| D07 | routed 的首次发送、retry/fallback 撤销场景 | 原有效行为保留，共用检查后无回归 |

### 3.2 幂等身份 K

| ID | 场景 | 必须断言 |
|---|---|---|
| K01 | 同键、同输入、同公共身份与同 harness | 返回既有 replay 协议；不新增记录、不发送 |
| K02 | 无 harness，同键同输入，逐个修改公共身份字段 | 每种差异均为 InvocationIdempotencyConflict；null 差异亦拒绝 |
| K03 | 相同键，先有 harness 后无 harness，或反向 | 两个方向均拒绝作为相同调用重放 |
| K04 | 公共列相同但 harness trace/span 不同；或输入/来源摘要不同 | 明确冲突，不复用旧结果 |
| K05 | PostgreSQL：并发相同键和相同语义 | 最多一个调用创建成功；另一方 replay；没有多余快照 |
| K06 | PostgreSQL：并发相同键但不同执行身份 | 一方成功，另一方领域冲突；不覆盖、不泄漏数据库约束错误 |

### 3.3 编译与修复身份 S

| ID | 场景 | 必须断言 |
|---|---|---|
| S01 | 真实编译器先返回非法 JSON，再返回合法结果 | 2 invocation、2 snapshot、2 不同 span；父 span 均为 Tool span；旧快照未追加修复消息 |
| S02 | 首次编译网络 timeout 后重试成功 | 1 invocation/snapshot/span，2 attempts |
| S03 | 首轮返回非法 JSON，修复调用再发生网络 retry | 两个逻辑调用 span；修复的多个 attempt 共用自身快照与身份 |
| S04 | 编译失败退出后同协程发起另一调用 | ambient Context 恢复，无 invocation/snapshot/span 串用 |
| S05 | 审批恢复后编译；中途 stable 变化；无 harness 独立编译 | 原 Tool 身份及固定 bundle 不漂移；新 LLM 有 child span；旧独立入口兼容 |

### 3.4 真实来源及证据 B

| ID | 场景 | 必须断言 |
|---|---|---|
| B01 | 实际 Memory 更新，新的 turn 重新选取并发送 | 新快照记录实际选用内容/版本；旧快照正文、引用、摘要不变 |
| B02 | 实际 Skill 更新并授权新版本，新的 run 建立绑定 | 新旧快照引用各自固定版本；被撤销的版本不能因已有快照而继续发送 |
| B03 | 同一输入带 partial 来源，含已知关键引用 | 可正常冻结和发送；已知引用准确，未知部分不伪报 complete |

以上 21 项为验收场景，不是预计 pytest 数量；参数化展开后的真实数量在报告另行统计。

## 4. 修复完成门禁

必须全部满足：

1. F01/F02/F03 的原始复现已转为自动化测试，并在修复后通过。
2. direct/routed 必需资产检查逐 attempt 生效，撤销后没有新增网络请求；失败调用状态和预算预留处理完整。
3. 新逻辑调用创建独立 span/invocation/snapshot；网络重试保持原身份，审批恢复继续继承原 Tool Context。
4. 幂等比较覆盖无 harness、单边 harness、null 差异及 PostgreSQL 并发；没有身份不同却被视为 replay 的情况。
5. I06/A05/A06/I05 的证据来自实际被验收路径，不能只靠辅助函数或替身业务实现。
6. 原 Phase 2A 专项及相关回归通过；任何未执行、失败或环境受限项明确列出，不以历史通过数替代当前结果。
7. 验收报告、节点映射、evidence 与实际测试结果一致；代码验收状态和提交/PR 状态分别注明。

## 5. 建议复验命令与记录

在 `backend` 目录执行，新增场景优先补进现有专项文件；如新增文件，必须将其加入执行清单。

```powershell
python -m pytest tests/test_model_input_snapshot.py tests/test_model_input_snapshot_store.py tests/test_snapshot_gateway.py tests/test_snapshot_flow.py tests/test_snapshot_recovery.py -q --tb=short
python -m pytest tests/test_execution_context.py tests/test_harness_context_flow.py tests/test_harness_context_approval.py tests/test_model_control.py tests/test_model_gateway.py tests/test_routed_model_gateway.py tests/test_goal_program_compiler.py tests/test_goal_programs.py tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py tests/test_conversation.py tests/test_conversation_worker.py tests/test_conversation_protocol_v2.py -q --tb=short
```

设置并验证隔离测试库 `TEST_DATABASE_URL` 后：

```powershell
python -m pytest tests/integration/test_model_input_snapshot_postgres.py tests/integration/test_harness_context_postgres.py tests/integration/test_chat_goal_tools_postgres.py tests/integration/test_goal_tool_recovery_postgres.py tests/integration/test_root_task_budgets.py -q --tb=short
```

若环境要求按文件分组，保留分组命令和全部结果，不改变测试断言。禁止在报告或日志中输出数据库密码和模型凭据。

证据保存到 `docs/acceptance/context-snapshot-phase2/phase2a/` 下独立修复目录，建议包含：修复前失败日志、修复后专项及回归日志、缺陷→测试节点映射、修复验收报告、脱敏链路样本。引用已有独立复核报告，不覆盖其原始结论。
