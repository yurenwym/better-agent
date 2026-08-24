# Better Agent 多 Agent 协同与受控自进化实施计划

> 日期：2026-08-24
>
> 依据：`docs/superpowers/specs/2026-08-24-multi-agent-collaboration-and-controlled-evolution-design.md`
>
> 原则：测试驱动、纵向切片、SQLite 权威、中心化提交、先只读并行、候选永不直接生效。

## 1. 交付目标

1. 建立可追溯的最小 Runtime Bundle，每次专家运行固定 code/model/Skill/Prompt/Policy/工具/Context 配置 Hash。
2. 建立持久 SQLite Task Kernel：父子任务树、lease epoch fencing、heartbeat、attempt、Checkpoint、Artifact、join、取消、预算预留和 append-only 事件。
3. 建立按需专家模式：Coordinator 将复杂目标拆给 Researcher、Planner、Critic；子 Agent 只读上下文并返回类型化 Artifact；Coordinator 合并为用户可见结果或现有领域 Proposal。
4. 建立工具执行 claim，避免多个执行者竞争同一逻辑副作用；未知外部结果进入 reconciliation，禁止盲重放。
5. 建立受控自进化：Experience、Candidate、Evaluation、Approval、Runtime Channel、Canary Exposure、Promotion 和 Rollback。
6. 建立用户界面：深入处理入口、专家进度、取消、可读轨迹、统一决策和成长页；不展示原始思维链。

## 2. Slice 0：失败测试与最小 Runtime Bundle

### 后端测试先行

- 新库和旧库重复 migration。
- Bundle 内容 Hash 稳定；Skill/Prompt/工具 Schema 变化后 Hash 变化。
- Run/AgentRun 创建后固定 bundle，active channel 切换不影响在途任务。
- Checkpoint/Context Snapshot 可以追溯 bundle。

### 实现

- migration 5：`runtime_bundles`、`runtime_bundle_items`、`runtime_channels`。
- `BehaviorBundleService` 生成、读取、激活不可变 Bundle。
- 启动时从 Git/文件 Hash、模型配置、Skill 文件、Prompt/Policy 协议和工具描述生成 stable Bundle；不存 API Key。

### 验证

- `pytest tests/test_behavior_bundles.py tests/test_db.py`

## 3. Slice 1：Task Kernel 安全内核

### 后端测试先行

- 两个 Worker 同时 claim 只有一个成功。
- lease 过期接管后旧 epoch 的 heartbeat、Checkpoint、Artifact 和完成提交均失败。
- 确定性 `child_key` 防止重复 fan-out；深度和子任务数超限失败。
- ALL_SUCCESS / ALL_DONE 在任意完成顺序下确定性收敛。
- 父任务取消使后代 lease 失效；迟到结果只记录拒绝事件。
- Artifact Schema 无效、Hash 冲突和重复提交失败。
- 并发预算预留不超支。
- 事件 seq 连续、唯一、append-only。

### 实现

- migration 6：`agent_runs`、`agent_tasks`、`agent_task_attempts`、`agent_context_snapshots`、`agent_task_checkpoints`、`agent_artifacts`、`agent_events`、`agent_budget_ledger`。
- `AgentTaskService`：create root、fan-out、claim、heartbeat、checkpoint、complete/fail、join、cancel、recover。
- 所有提交使用 `task_id + lease_owner + lease_epoch + lease_until + expected_version` fencing。
- 合法 Artifact、task pointer、终态和事件同事务提交。
- `ManagedAgentWorker` 单进程有限并发；SQLite 为唯一事实源。

### 验证

- `pytest tests/test_agent_tasks.py tests/test_agent_worker.py`

## 4. Slice 2：工具 claim 与审批绑定

### 后端测试先行

- 两个执行者竞争同一逻辑动作最多一个 claim 成功。
- tool 参数、target、task、agent、Skill digest 或 Policy version 变化后审批失效。
- 外部成功但内部未确认时进入 `RECONCILIATION_REQUIRED`，恢复不重放。
- COMPLETED claim 重试返回原结果。

### 实现

- migration 7：`tool_execution_claims`，扩展 approvals 的不可变绑定字段。
- 将现有 ToolRegistry 执行路径改为 prepare/claim/execute/finalize。
- 单 Agent 旧路径继续兼容；多 Agent 首版禁止 WRITE leaf。

### 验证

- `pytest tests/test_tool_execution_claims.py tests/test_tools.py tests/test_approval.py tests/test_checkpoint.py`

## 5. Slice 3：专家纵向闭环

### 后端测试先行

- Coordinator 只为明确 expert request 或通过复杂度门槛的请求创建专家任务。
- Researcher/Planner/Critic 得到同一固定快照，但只能读取自己的必要切片。
- 子 Agent 不能 WRITE、不能接受 Proposal、不能改变正式计划或记忆。
- 合并顺序稳定；Critic 只读 Artifact 和 rubric。
- Worker 重启从 Checkpoint/已提交 Artifact 继续。
- 取消后在一个 heartbeat 周期内停止。

### 实现

- `agents/coordinator.py`：有限角色、任务树、输出 Schema、merge。
- 复用 ModelGateway、Research Retriever、Plan/Memory Context Pin 和 Skill allowlist。
- `ExpertModel` 结构化输出；无模型时使用确定性测试 handler。
- Conversation 新增显式“深入处理”入口；专家结果写 assistant message。
- 用户可见任务事件投影到 Thread Event。

### API

- 创建/查看/取消 Expert Run。
- 任务、Artifact、事件列表与 SSE stream。
- Thread SSE 在 Expert Run 活动期间保持连接。

### 验证

- `pytest tests/test_agent_coordinator.py tests/test_agent_api.py tests/test_agent_sse.py`

## 6. Slice 4：Experience 与受控自进化

### 后端测试先行

- 单条经验不能生成全局生效候选。
- 同 lineage 不能跨 DISCOVERY/DEV/HOLDOUT/SAFETY。
- 当前周期 Eval/Shadow/Canary 数据不能修改同一候选。
- Candidate 从 READY_FOR_EVAL 起冻结，修改创建新版本。
- 确定性安全失败拥有否决权；LLM Judge 不能覆盖。
- 审批绑定 candidate/evaluation/permission diff/target bundle digest 和 expiry。
- Canary 分配稳定，只统计实际 exposure；样本不足不能晋升。
- Promotion/rollback 原子切换 channel，新任务使用新 bundle，在途任务不漂移。
- 安全指标失败自动回滚；外部副作用只进入 reconciliation。

### 实现

- migration 8：Experience、Candidate、Evaluation、Decision、GatePolicy、Canary Deployment/Exposure、Evolution Event。
- Observer 从终态轨迹生成脱敏 Experience。
- Candidate Service 支持 Memory/Skill/Policy/Prompt/Code 五层权限矩阵。
- Evaluation Service 运行静态检查、确定性 suite、replay/holdout 指标；保存完整 attestation。
- Approval/Promotion Service、stable/canary channel、自动 rollback。
- 首版仅人工创建或从三条独立同类经验建议候选；不自动修改文件。

### 验证

- `pytest tests/test_evolution.py tests/test_evolution_api.py tests/test_eval_scenarios.py tests/test_redaction.py`

## 7. Slice 5：前端

### 测试先行

- 用户可选择深入处理或普通继续。
- 专家进度显示任务、状态、证据数、结论和不确定项，不显示原始思维。
- 可取消专家运行，保留已完成成果。
- SSE 缺 seq 时停止投影并补拉。
- 成长页展示候选 diff、证据、评测、权限变化、审批、Canary 和回滚。
- 所有决策卡展示动作、目标、影响、风险和撤销方式。
- 320px 不产生页面级横向滚动；键盘可操作。

### 实现

- `ExpertRunCard`、`ExpertTaskCard`、`EvolutionCandidateCard`。
- Chat 发送区增加“深入处理”；普通模式保持默认。
- 轨迹页面增加专家里程碑分组和技术详情二级入口。
- GrowthPage：待确认、能力、历史；复用 MemoryPage 的记忆入口。
- WorkspaceSidebar 增加“成长”。

### 验证

- `npm test -- --run`
- `npm run build`

## 8. Slice 6：端到端与故障注入

- 长期骑行计划：Researcher + Planner + Critic -> Plan 候选 -> 用户确认。
- 七天内蒙古研究：ALL_DONE 部分失败仍交付并说明缺口。
- Ask Policy 候选：三条经验 -> Candidate -> Eval -> Approval -> Canary -> Promote/Rollback。
- 在 claim、checkpoint、artifact commit、last child join、promotion pointer 切换处注入崩溃并重启。
- 验证取消、SSE 重连、预算耗尽、失租、迟到结果、脱敏导出。

## 9. 最终门槛

- 后端全量 pytest 通过。
- 前端全量 Vitest、TypeScript、Vite build 通过。
- `git diff --check` 通过。
- 无密钥、`data/`、memory、eval results、构建缓存或本地运行产物进入提交。
- 形成按安全内核、专家闭环、自进化、前端分开的清晰提交。
- 最终报告如实区分自动化已完成项和仍需真实流量/样本才能启用的 Canary 晋升能力。
