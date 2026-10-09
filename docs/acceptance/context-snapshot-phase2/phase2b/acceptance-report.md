# ContextSnapshot Phase 2B 验收报告

## 2026-10-09 M2 最终复验（当前权威结论）

**M1 / M2 验收通过。T01–T38：38 pass / 0 partial / 0 pending。** 下方阶段性状态均为历史，不能覆盖本节。测试基于 `1a12532` 后的工作区增量；本次交付提交包含代码、测试和证据。未执行付费模型调用，无 Runtime 重构或数据库迁移。

### 实际结果

| 最终选用批次 | 模式 | 结果 |
|---|---|---|
| m2-complete entries | observe | 61 passed |
| m2-complete snapshot | observe | 122 passed |
| m2-complete postgres | observe | 93 passed |
| m2-complete m1 | enforce | 37 passed |
| m2-r1 harness | observe | 267 passed |
| m2-r1 business | observe | 273 passed |
| m2-fixed learning | observe | 236 passed |

合计 **1089 个不同测试节点通过，0 failed / 0 skipped**。入口和快照另有 enforce 复跑（60 + 122 passed），不混入上述去重总数。测试内按职责显式覆盖模式：纯 usage/结算测试使用 observe，预算拒绝测试使用 enforce；不能把整组描述为每个节点都 enforce。

- B00/B12：21 个已登记生成式**业务入口组**全部有正向发送证据；不是 21 个原始代码调用点。AST 调用候选、HTTP/网关装配、共享 Judge 和动态分派人工追踪，映射见 `callsite-contracts.json`。
- `m2-complete-bindings.ndjson` 捕获 **598 次发送 / 598 次发送前有效绑定**，**596 个唯一 invocation**。两个额外发送来自 Research 和管理验证的网络重试。没有无效绑定、未登记入口或未匹配的 pytest 节点。
- 分母限定为 `evidence.json.capture_nodes` 列出的插桩节点的全部 provider seam 调用；在审计前递增，失败审计也写入原始记录。不把其他未插桩的旧回归发送算进来。记录器故意无绑定的自测使用独立临时日志，断言失败记录保留，不属于受控生产调用样本。
- 所有记录均校验独立连接可见的已提交 attempt/invocation/snapshot、最终逻辑输入、owner/profile/digest；未创建 Harness 的合法旧 Worker execution digest 保持 null，不伪造身份。
- 来源按唯一 invocation 统计：**complete=0，partial=596**。Research 标识实际摘录 ID/digest；Learning 标识可用 evidence 引用、提取原文、Judge rubric/盲评对。没有声称完整网页、全部历史逐片段或 dropped 清单已覆盖。
- `build_snapshot_m2_report.py` 同时验证 pytest 收集、JUnit 实际通过、入口分支发送证据、发送节点与 JUnit 交集、重复 send ID；任一缺失即非零退出。原始命令/时间/模式/退出码见 `logs/m2-runs.ndjson` 及各组 log/XML，关键文件 SHA256 见 evidence.json。

### 门禁收口与边界

1. B00–B13/B06A 已按 `test-contracts.json` 逐项映射；M1 原闭环记录保留，本轮快照/Harness/隔离 PG/M5/offline harness 复验通过。
2. Research/专家异常、取消和恢复不串 owner；PG 验证专家根预算分离、Learning job/root/bundle、非默认 program owner 的 goal_operation 根预算。UNKNOWN 通过真实 Learning 发送后故障及 Evolution 状态恢复验证不重发。
3. Runtime HTTP 生命周期覆盖澄清、计划、非法 JSON 修复、决策。普通无源 goal 按现有逻辑不反思；测试附加真实持久化 source turn 后验证可信反思，未改业务行为。T14 采用授权批次 owner 传播和真实受控 adapter 发送的组合证据，不宣称一个非默认 owner 全链路 HTTP 测试。
4. T34 投影 claim 失败零发送、READY 保存后恢复不重发；T35 daily Worker/period service/调整 API 修复和异常后 reset；T36 三种 Judge 和额外 conversation Judge 分别有独立调用。
5. 无 store/无身份/悬空来源拒绝；离线选项仅服务端明确装配。API body/query/工具参数反向验证，生产网关构造点不读取离线用户参数。管理未激活版本验证仍使用指定 profile，不创建 stable。
6. 最终选用组覆盖本轮四个生产文件的受影响业务；代码只增加来源元数据，不改 prompts、学习算法或状态机。

### 失败历史与未执行范围

- `m2-r1-learning-observe`：235 passed / 1 failed。`test_budget_blocked_evaluation_resumes_through_api_without_repeating_case` 原依赖 shell 成本模式；显式 enforce 后 `m2-fixed` 整组 236 passed。失败 log/XML 保留，未隐去。
- 开发中新增夹具曾有修复文案误判、裸 RuntimeError 未结算 STARTED、PG 未注入 CostService、冻结 profile 误更新、FK 故障注入方式、API 幂等键及测试字段拼写错误；修正后以本节完整分组复跑为准，非生产修复豁免。更早失败轮保留于下方历史及旧日志。
- 未重新执行仓库全量 pytest；用户此前报告的 2106 passed / 7 skipped / 8 failed 不是本轮证据。历史 golden journey/SQLite immutability/Tavily 失败未在本轮另行修复；相关快照数据库约束另有专项及 PG 通过证据，不声称全仓零失败。
- 本结论是离线自动化验收，不是生产长期观测或真实模型效果验收。direct/routed 响应后撤销语义仍有既有差异；发送前撤销与实际 usage 保留已回归，不声称两者响应后行为统一。
- 完整来源、线上遥测、策略/prompt A/B 与 Runtime 执行循环统一属于后续 LLM + Harness 工作，不是本轮扩展项。

## 以下为检查点历史记录（不代表当前状态）

## 2026-10-09 检查点后的风险补验（最新状态）

- 检查点 `8430f3d` 已本地提交，57 个文件；未推送。以下为检查点后的增量，未修改生产逻辑。
- 新增专家 Worker 两分支测试：同库两 owner 交错执行，一专家失败，成功专家各自触发安全 Judge，两种汇总看到各自实际产物；每场景 9 次发送独立绑定，跨 owner 快照读取拒绝。T09 pass；T10 仍缺 PG 预算隔离，partial。
- 新增 Research Worker 异常/取消后恢复，真实模型 plan 与 Judge 经过受控 gateway，旧快照保留、ambient context 恢复。T08 partial：不将 RUNNING invocation 重放覆盖冒充业务 UNKNOWN 契约。T36 三类 Judge 已有证据，但尚未逐项核实完整门禁，保留 partial。
- 新增请求 body/query 及工具 offline 参数反向测试；当前证明 Goal API 和 query_goals 工具不能开启旁路，T28 partial，不能外推所有 API。
- T32 新增只读 `scripts/audit_snapshot_callsites.py`，扫描 AST 候选并明确排除同名业务 complete、CLI 和共享内部 transport，对照注册清单、现有发送记录及 evidence 的实际 pytest 节点。发现并补登记演化 proposer、对话 Judge、Goal Program 初始/工具内编译；注册数从 18 到 21，最终生产分母仍未确认。失效 CLI 测试引用已修正。无孤立发送、无缺字段绑定、无未解析节点；仍有注册入口缺发送记录，脚本应退出 1。
- 专家夹具增加 expert 路由、stable 激活幂等键按 owner 区分，使同库多租户测试可复用；不改变生产路由。
- observe 回归：`test_snapshot_agent_entries.py test_snapshot_research_entries.py test_snapshot_production_boundary.py test_snapshot_goal_runtime_entries.py test_snapshot_gateway.py test_mainflow_experts.py test_research_service.py`，96 passed / 57.80s。首次同批 94 passed / 2 failed，原因是新增测试误把 Routed 的 RoutingError 当作 direct GatewayError，修正断言后通过。
- 最新对账 **16 pass / 21 partial / 1 pending（T23）**。M2 仍未完成；PG 预算隔离、UNKNOWN、全入口分母及各 partial 的余项不以本轮定向结果豁免。未重跑全量后端或隔离 PG。
- 最终 enforce 专项：专家、Research、production_boundary 三文件 **19 passed / 16.49s / exit 0**（包含新增工具参数拒绝与 RUNNING 重放用例）。当前记录覆盖 12/21 个已登记入口，另 9 个无该日志格式的发送证据；该比值不是生产覆盖率。

## 2026-10-09 最小补验更新（优先于下方历史状态）

- 按最小改动范围，仅在现有 `test_snapshot_goal_runtime_entries.py` 新增 3 个场景；未新建测试框架或拆分文件，未修改生产代码。T34 通过；T35 从 pending 更新为 partial；T36 根据此前 Runtime 用例更新为 partial。其余历史状态不自动升级。
- T34 验证真实对话 propose_execution → 投影：tenant-b 身份与 turn/bundle/root 绑定、claim 失败零发送、已保存 READY 草稿后中断保留快照、恢复和幂等重放不重复发送。
- T35 验证 daily Worker 失败后重试及 JSON 修复、period_summary 服务、调整 HTTP API 的 JSON 修复；发送前用独立连接读取已提交快照，失败/修复调用不共用快照，Worker 异常和成功后恢复原 ambient context。PG goal_operation 根预算和非默认 program owner 本轮未覆盖，不能记完整通过。
- Alembic guard 用例原先通过 `request.getfixturevalue` 读取 session fixture；前序 PG 用例已初始化时会复用缓存，环境变量修改不再触发拒绝检查。改为直接调用迁移 fixture 的原始函数验证拒绝发生在 subprocess 之前，不启动数据库、不修改迁移代码。未重跑原全量顺序，因此不声称重现了原失败的全部条件。
- 前轮修复已包含：无 source turn 的 Runtime 使用服务 owner；悬空 turn 不回退；专家/演化缺 owner 拒绝；无 store gateway 要求显式 transport 或 offline_unbound；Judge 保留调用方 purpose。下方旧描述中的无 turn 一律拒绝、仅 transport 可离线等内容已过时。
- 回归批次：目标 Runtime 专项、执行投影、每日复盘、周期复盘、Goal Program API、DB target guard，共 **83 passed**（默认成本环境，38.92s）。专项首次执行为 13 passed / 2 failed，均为新增测试装配错误（读取 tenant-b turn 未传 owner、sentinel 缺 role）；修正后 15 passed。
- 用户提供的全量结果为 2106 passed / 7 skipped / 8 failed，本轮未重新执行该全量集合；不得将本轮定向通过写成全量通过。
- 最终专项复验：`BETTER_AGENT_COST_MODE=enforce python -m pytest tests/test_snapshot_goal_runtime_entries.py tests/integration/test_db_target_guard.py -q --tb=short`，**39 passed / exit 0 / 10.49s**。Runtime 文件 `--collect-only -q` 收集 15 个节点，三个新增映射均真实存在。JSON 校验和 `git diff --check` 通过（后者仅换行提示）。
- B12/T32 仍未闭环：本轮只增加 CS-GR-03 / CS-GP-01 / CS-GP-02 的真实节点与发送证据，不外推全入口覆盖率。专家正向入口、生产旁路全仓审计及其他 partial/pending 暂不扩展；无需为建议文件名创建空文件。
- 当前对账为 **15 pass / 17 partial / 6 pending**，M2 仍未完成；代码仍在工作区，未提交。本节是增量更新，保留下方历史批次与失败记录。

- 日期：2026-10-08（Asia/Shanghai）
- 开发基线：`37a4e9af39faa781a92db1227e444173e13c2db0`，分支 `front0920`
- 当前代码状态：未提交；任务书文件仍由用户提供且未跟踪
- Python：3.13.0；仓库 `backend/agent.db` SQLite schema version 24；Alembic 声明 head `20260930_0025`
- M1：**通过本轮复验**。追加复验记录见 Phase 2A 报告末尾；Phase 2B 隔离 PostgreSQL 指定集合复跑 49 passed。历史报告与首次失败轮次均保留。
- M2：**未完成，不通过验收门禁**。本报告记录已实现的 fail-closed 与调用边界修复，以及有限的 SQLite 动态测试；不代表全入口覆盖。

## 已交付改动

- `ModelCallContext.owner_id` 不再默认成 `local-user`。Routed/direct 受控发送在打开 invocation 前拒绝缺失身份；profile 解析 helper 也不再造默认 owner。
- 无 control store 的 `ModelGateway` 在未显式注入 transport 时拒绝 HTTP 发送。注入 transport 的低层路径仅供测试/离线使用，仍不构成生产快照覆盖。
- Research Worker 源 turn 缺失时拒绝执行；Research Judge 不再回落 `local-user`。
- Agent Runtime 调用和 `judge_run_output` 从已授权源 turn/thread 取 owner；缺失时不发送 Judge 请求。
- `LiveBehaviorRunner` 显式接收授权 owner，baseline/candidate/Judge 分别派生逻辑调用身份。
- 对话辅助调用及主回答从 turn 父上下文派生新逻辑调用；Runtime JSON 修复派生新的 snapshot/span。
- ModelAdmin `_verify_live` 改走注入的 control store、服务 owner 与明确 purpose；startup 和 API fallback 装配受控 store。
- 更新共享入口清单，按稳定符号登记本期 18 个 callsite，并记录 M1 状态与未覆盖原因。

## 验收状态

| 范围 | 状态 | 证据 / 缺口 |
|---|---|---|
| B00 入口清单、基线与 M1 对账 | 部分完成 | M1 已按当前工作区复验通过；清单已追加 CS-RS 至 CS-MD 稳定 ID。完整生产装配追踪与 owner 构造点来源审计仍未完成。 |
| B01 动态验收夹具 | 部分完成 | `CommittedSnapshotTransport` 通过独立连接校验已提交 invocation/snapshot/attempt/request；Research/Learning/归档/引用捕获批次为 18 sends、17 invocations，18 次发送绑定有效。SQLite 参数化注入 snapshot/invocation/execution binding 三类写入失败均零发送；PG 管理端快照写失败也零发送。尚无全生产入口聚合总账。 |
| B02–B06 Research / 专家 / Learning / Evolution / 真实评估 | 部分完成 | Research 8 个方法分支及 JSON 修复/网络 retry 边界已动态验证；另有 LearningAgent、ConstraintExtractor、LearningJudge 用例。生产 Research/Learning Worker job 到 owner/root/bundle 的完整来源链、专家、Evolution/replay 流程仍未验收。 |
| B06A 目标 Runtime / 投影 / 复盘 | 部分完成 | Runtime owner 与 JSON repair 边界有改动；T33 仅验证 LiveRuntimeModel 的 JSON repair，没有 API Runtime、投影、Goal review Worker 完整测试。 |
| B07 摘要、引用、对话辅助入口 | 部分完成 | 新增真实 `ConversationArchiver` + `LiveEpisodeSummarizer` 和 memory reference resolver 的 SQLite 动态发送断言；归档恢复、引用解析超时/取消、对话辅助全部分支仍未覆盖。 |
| B08 管理端验证 | 完成 | T19–T22 均通过：指定未激活版本且不改 stable、跨 owner 零发送、缺凭据/缺 store/零预算/PG 快照写失败、失败 retry 绑定，以及 startup/API fallback 受控 store 装配。容量迁移的两个临时 service 仅读/写 profile version，不调用 verifier，已在入口清单说明排除依据。 |
| B09 生产旁路与身份 fail-closed | 部分完成 | Routed/direct 缺 owner 零发送及无 store 默认 HTTP 拒绝有测试；未完成全仓静态旁路扫描、离线 API 固化及全部调用构造点迁移。 |
| B10 统一身份、来源和失败回归 | 部分完成 | T03、T29/T30 等关键边界已有回归；两 owner 全入口交错、撤销和所有来源冲突场景仍需补齐。 |
| B11 PostgreSQL 集成与兼容 | 完成 | 文档规定的隔离 PG 集合 49 passed，包含 Research/ModelAdmin 新入口、管理跨 owner 与失败零发送、旧快照兼容、不可篡改、Harness 隔离和 attempt 并发结算。 |
| B12 全入口覆盖率对账 | 未完成 | 18 个生产 callsite 中 1 个完整覆盖、7 个部分、10 个待验；transport 计数仍只有专项捕获批次，不是全入口分母。 |
| B13 M2 验收与交付 | 未完成 | M1 已复验；M2 的 T01–T38 全量、全入口覆盖和独立提交尚未完成。阶段性报告/证据/日志已更新。 |

## 已执行的测试

T01–T38 已逐项记入 [t01-t38-accounting.md](t01-t38-accounting.md)：14 pass、15 partial、9 pending。partial 不计通过，因此 M2 门禁仍未满足。

- `tests/test_snapshot_flow.py` + `tests/test_snapshot_gateway.py` + 两个定向回归节点：52 passed。
- 对话、Worker、LiveModel、ModelAdmin API、startup contract：133 passed。
- Research/Agent Worker/Goal review/Goal adjustment/Memory archive 与 reference gateway 回归：159 passed。
- 成本、gateway、routed gateway、context wire gate 和 runtime policy 回归：80 passed。
- `tests/test_model_admin_api.py`：10 passed；含未激活版本、跨 owner 零发送、transport 失败重试绑定、缺凭据/缺 store/零预算失败以及 startup/API fallback 受控 store 装配。
- `tests/test_real_evaluation.py`：35 passed，执行配置 `BETTER_AGENT_COST_MODE=enforce`。
- 隔离 PostgreSQL：`test_model_input_snapshot_postgres.py`、`test_harness_context_postgres.py`、`test_root_task_budgets.py`、`test_attempt_settlement_postgres.py`：复跑 34 passed；`test_chat_goal_tools_postgres.py` 与 `test_goal_tool_recovery_postgres.py`：12 passed。首次 PG 轮次中 2 个 root budget 测试因调用点未显式传 owner 失败；补齐测试 owner 后同组复跑全绿。
- `tests/test_snapshot_flow.py::test_t38_conversation_auxiliary_calls_and_main_call_have_sibling_spans`：1 passed。
- `tests/test_snapshot_flow.py::test_t33_runtime_json_repair_gets_a_new_snapshot_and_child_span`：1 passed。
- 新增 Research/Learning/归档/引用 adapter 专项：15 passed；发送钩子捕获 17 次 SQLite 发送（含同一网络 retry 的第二次 attempt）并在独立连接验证绑定。来源均为 `partial`，Memory/Learning adapter 测试未构造 Harness。
- 新增 `tests/integration/test_snapshot_entrypoints_postgres.py`：3 passed；与文档要求的全部快照、Harness、chat goal tool、goal recovery、root budget、attempt settlement PG 组合同次运行，合计 49 passed。管理端快照插入触发器故障时，验证在发送前失败且事务内 invocation/attempt 为零。
- 文档规定的网关/上下文/对话/Goal 编译整组 SQLite 回归：修复 span 派生后 267 passed；首轮 265 passed、2 failed 的失败记录保留于日志。
- 文档规定的 Research/Agent/API 批次：134 passed；Runtime/投影/Goal review/adjustment 批次：73 passed；Learning/Evolution/真实评估/Memory 批次（`BETTER_AGENT_COST_MODE=enforce`）：235 passed。
- 捕获批次记录在 `logs/observed-bindings.ndjson`，脱敏关联示例整理在 `binding-examples.json`。本轮新增管理端节点见证据日志和 `evidence.json`。
- `tests/test_execution_context.py::test_u10_gateway_wrappers_keep_the_public_identity` 与 `tests/test_evolution.py::test_live_behavior_runner_pins_each_arm_to_its_bundle`：2 passed。
- `python -m compileall -q app`：exit 0；`git diff --check`：exit 0（仅报告换行格式提示）。
- SQLite 结果和命令记录在 `logs/phase2b-local-runs.log`。
- 历史宽回归失败记录保留于日志；其中真实评估成本断言在指定 enforce 配置及模块内显式配置后复验通过，不再作为未解释失败。

## 尚未满足的门禁与限制

1. M1 已通过本轮复验；详见 Phase 2A 报告追加段落。Phase 2B 自身仍不满足全入口门禁。
2. 生产入口到 pytest node 的全量双向映射未完成；登记的 18 个 callsite 中 1 个（CS-MD-01）满足完整验收契约，17 个仍未闭环。Research/Learning/Memory 有局部 adapter 发送证据。
3. Research/Learning/归档/引用专项捕获批次为 18 observed sends、17 个独立 invocation、18 个发送前有效提交绑定；其中一次 retry 按设计复用原 invocation/snapshot。这只是部分入口样本，不能作为全量 transport denominator。
4. 该批次 0 complete / 18 partial（因适配器用例未提供来源元数据）；没有全入口来源汇总、三类失败持久化零发送覆盖、所有 owner 并发隔离覆盖。
5. 管理端验证的安全失败路径、learning/evaluation 的资产撤销、attempt 结算保护、PG 事务和历史兼容未验收。
6. 未运行真实付费模型；本阶段无需真实模型冒烟。PG fixture 使用单独 compose project 与临时测试容器，没有设置 `TEST_DATABASE_URL`，容器运行后由 fixture 清理。

结论：本工作区包含针对身份缺失、默认 owner 回落、JSON 修复和对话调用 span 的具体修正，但**不能宣布 Phase 2B 完成**。需继续完成 B00–B13、T01–T38 和隔离 PostgreSQL 验收后再更新为最终报告。
