# Better Agent V1.2：可信成长与可验证交付实施计划

## 1. 背景

Better Agent 已经具备对话、Ask、深度研究、计划文档、Goal Program、Today、每日复盘、计划调整、分层记忆、多 Agent Expert Run、Runtime Bundle、Evolution Candidate、Canary、Checkpoint、SSE 和追加式轨迹事件。当前主要问题不是缺少模块，而是：用户需要自己理解多个页面才能推动目标；真实任务不会自动形成可信的 Evolution Experience；候选评测缺少基线/候选行为对照；多 Agent 只能手动开启；公开仓库缺少 CI、Docker、浏览器 E2E 和量化报告。

## 2. 开发目标

按固定顺序完成八个阶段，将现有模块连接成一个可演示、可复现、可量化的本地 Personal Agent Runtime：

1. CI、Docker、黄金 E2E；
2. 统一目标工作区；
3. 用户成长档案；
4. Experience Observer；
5. 基线/候选真实评测；
6. 动态专家协同；
7. Canary 与 Growth UI 完善；
8. Demo、架构图、性能与故障报告。

## 3. 固定边界

- 继续使用 FastAPI、React + TypeScript、SQLite WAL 和进程内 Managed Worker。
- 不新增 Redis、Celery、Kafka、微服务、RAG、向量数据库、MCP、Shell、Skill 市场或多租户云平台。
- 不允许子 Agent 直接执行 WRITE，不允许并行副作用。
- V1.2 自动进化只支持 Prompt Candidate，不自动修改 Memory、Skill、Policy、Code、权限或安全门槛。
- Promote 必须人工确认；安全失败可自动 Rollback。
- 不持久化隐藏思维链，只保存状态、事件、结构化 Artifact 和用户可见结果。
- 不提交密钥、`data/`、memory、研究结果、评测运行结果或本地数据库。

## 4. 实施原则

- 测试驱动：状态迁移、幂等、Owner 隔离、数据分区、候选门槛、Canary 回滚先写失败测试。
- 纵向切片：每阶段包含数据库/API/前端/测试，不留下只可调用但不可用的占位接口。
- 复用现有服务：Research、PlanDocument、GoalProgram、MemoryStore、AgentTaskService、EvolutionService、StatsProjector。
- 每阶段通过定向测试；最终通过后端全量、前端全量、生产构建、黄金 E2E、敏感扫描与 `git diff --check`。

## 5. 阶段一：CI、Docker、黄金 E2E

### 5.1 交付物

- `.github/workflows/ci.yml`：Python 测试、确定性评测、前端测试、生产构建、Playwright E2E、敏感信息扫描。
- `Dockerfile`、`.dockerignore`、`compose.yaml`：单容器同源托管，数据挂载到 `/app/data`，只暴露 8000。
- Playwright 配置和黄金路径测试；使用确定性 E2E 模型/种子数据，不依赖真实 API Key。
- README 更新：真实能力、边界、Docker/本地启动、测试命令和数据安全。
- `LICENSE`。

### 5.2 黄金路径

`新建会话 → 生成并保存计划 → 激活 → Today 完成行动 → 复盘 → 接受调整 → 完成目标 → 查看成长记录`。

研究质量依赖网络，不纳入每次 CI 的确定性黄金路径；另提供带本地固定检索器的研究 E2E。

### 5.3 验收

- CI 中所有检查通过；Mock E2E 连续运行 20 次无失败。
- Docker 健康检查通过，空白数据卷可启动。
- 重复提交不产生重复计划、Action、Review 或 Memory Episode。

## 6. 阶段二：统一目标工作区

### 6.1 数据与服务

- 不新建第二套 Goal 表；新增 `GoalWorkspaceProjector`，从 Thread、Research Job、PlanDocument、GoalProgram、Review、Memory Episode 投影只读工作区。
- 工作区状态：`DISCOVERING / PLANNING / READY_TO_START / EXECUTING / REVIEWING / ADJUSTING / COMPLETED / PAUSED / BLOCKED`。
- 确定性计算唯一 `next_action`，包含 `kind/label/href/resource_id/reason`。

### 6.2 API 与前端

- `GET /api/workspaces/{thread_id}` 返回当前阶段、来源链、最近产物、Today 摘要和下一步。
- 新增目标工作区页面/侧栏入口；主要按钮只执行已有 API 或跳转已有页面。
- Research → Plan、Plan → Start、Today → Review、Review → Adjustment、Completed → Growth 均可追溯。

### 6.3 验收

- 任意非终态工作区恰有一个主要下一步。
- 工作区不缓存可从事实表推导的状态；刷新和重启投影一致。
- Research 到 Plan 一次操作，Plan 到 Today 不超过两次确认。

## 7. 阶段三：用户成长档案

### 7.1 数据与服务

- 新增 `growth_profiles`/`growth_snapshots`，或在可复用前提下以 Program Completion Episode + 投影实现。
- 从 Action、Feedback、Daily Review、Adjustment、Program 汇总：完成率、预计/实际用时、难度趋势、跳过/延期、调整次数和周期总结。
- 稳定经验只生成 Memory Proposal，必须由用户确认后进入长期记忆。

### 7.2 API 与前端

- `GET /api/growth/profile`、`GET /api/growth/programs/{id}`。
- Growth 页面分为“我的成长”和“Agent 改进”；默认展示用户成长。
- 支持查看目标历程、调整前后变化、来源证据；沿用 Memory 页编辑/停用/删除能力。

### 7.3 验收

- Program 完成后立即可见成长总结。
- 敏感反馈不出现在普通摘要或长期记忆 Proposal。
- 单次行为不被确定为长期偏好。

## 8. 阶段四：Experience Observer

### 8.1 数据模型

- `evolution_observer_offsets(stream_kind, owner_id, last_row_id, updated_at)`。
- 扩展 Experience：`source_kind/source_id/source_event_id/signal_type/severity/evidence_json/observed_at`。
- 唯一约束：`owner_id + source_kind + source_event_id + signal_type`。
- `lineage_group_hash` 绑定根 Turn、Run、Research Job 或 Goal Program；同一 lineage 最多计为一条独立证据。

### 8.2 Observer

- 消费 Thread、Run、Research、Goal Program、Agent Event 的终态和反馈事件。
- 支持信号：失败、取消、重试、用户纠正、工具失败、预算耗尽、研究重做、高难度、超时、Adjustment 决策、Program 完成、Expert Partial/Failed。
- 规则产生结构化 Experience；LLM 不决定分区、去重、独立性或安全等级。

### 8.3 验收

- 重启、重放、并发观察不重复写入。
- 同一任务多个失败事件不满足三证据门槛。
- 敏感文本只保留类别、计数、摘要 Hash 和安全的结构化字段。

## 9. 阶段五：基线/候选真实评测

### 9.1 Eval Case Registry

- 提交脱敏、固定的 `DISCOVERY/DEV/HOLDOUT/SAFETY` 场景定义；运行结果继续 Git 忽略。
- Candidate Generator 只能读取 DISCOVERY；DEV 可用于迭代；HOLDOUT/SAFETY 仅 Evaluator 可读取。
- 基线与候选使用相同输入、模型配置、工具边界和预算；评审隐藏 A/B 身份。

### 9.2 指标与门禁

- 完成率、协议合法率、Ask 合理性、用户纠正/重试率、安全违规、TTFT、Token、总延迟和盲评质量差。
- SAFETY 必须 100%；结构回归为零；质量非劣；成本/延迟不超过配置阈值。
- 报告绑定 Dataset、Evaluator、Candidate、Base/Target Bundle Digest。

### 9.3 验收

- 同一 Candidate 重跑得到相同确定性门禁结果。
- HOLDOUT 不泄露给 Candidate Generator。
- 无基线对照或报告 Digest 不匹配时不能批准。

## 10. 阶段六：动态专家协同

### 10.1 路由

- 对话控制协议加入 `start_expert`。
- `answer`：简单任务；`ask`：关键上下文缺失；`start_research`：需最新来源；`start_expert`：至少两个独立视角可提高质量；`propose_execution`：需要执行或副作用。
- 第一版自动路由只支持“复杂方案比较/决策”；保留用户手动“深入处理”覆盖。

### 10.2 编排

- 最多三个任务：领域分析、风险审查、综合协调；不创建通用 DAG 引擎。
- 父子使用不可变 Context Snapshot；子 Agent 只能提交 `expert_result.v1`。
- 单个失败生成 `PARTIAL` 综合结果；全部失败才失败；取消保留已提交 Artifact，拒绝 Lease 失效后的迟到结果。

### 10.3 验收

- 简单任务误触发率 <5%，复杂比较识别率 ≥90%。
- 路由后 500ms 内显示状态。
- 同一幂等键不重复创建 Run/Task/Message。
- 子 Agent 无 WRITE 能力，迟到 Artifact 接受率为 0。

## 11. 阶段七：Canary 与 Growth UI

### 11.1 Canary

- V1.2 只对 Prompt Candidate 开放 Runtime Adapter。
- Champion/Challenger 使用稳定哈希分流，记录真实 Bundle 暴露和任务结果。
- 最少 20 个 Champion + 20 个 Challenger 样本；安全失败立即 Rollback；质量恶化按冻结门槛 Rollback；Promote 必须人工确认。

### 11.2 Growth UI

- 明确区分“我的成长”和“Agent 改进”。
- Candidate 卡片展示：问题证据、独立 lineage 数、Prompt Diff、离线 A/B 指标、审批、实时 Canary 指标和回滚原因。
- 不用“已学会”描述仅创建但未验证的 Candidate。

### 11.3 验收

- UI 每个结论都可追溯到 Experience/Evaluation/Exposure。
- 样本不足不能 Promote；安全失败后稳定通道保持 Champion。
- 回滚后新任务不再分配到 Challenger。

## 12. 阶段八：Demo、架构图、性能与故障报告

### 12.1 交付物

- 3–5 分钟演示脚本与录制说明；不提交包含真实用户数据的视频源文件。
- 系统架构图、目标闭环图、多 Agent 时序图、自进化发布图。
- 性能脚本和脱敏基线报告：非模型 API P50/P95、SSE 重连、SQLite 并发、Observer 吞吐、Agent Worker 恢复时间。
- 故障注入报告：Worker 崩溃、Lease 过期、网络超时、模型限流、SQLite 冲突、投影恢复、迟到结果、重复幂等请求。

### 12.2 验收

- 100 个并发本地只读/幂等 API 请求无状态错乱；非模型 API 本机 P95 <300ms。
- Lease 过期任务可重新领取；晚到结果拒绝率 100%。
- Demo 不需手工修改数据库，README 与实际功能一致。

## 13. 最终测试矩阵

- 后端全量 `pytest -q`。
- 前端全量 `npm test -- --run`。
- `npm run build`。
- Playwright 黄金 E2E 与研究固定检索 E2E。
- 确定性 eval、Candidate A/B eval、Canary 状态机测试。
- 故障注入与并发幂等测试。
- `git diff --check`、敏感信息和禁止路径扫描。

## 14. 提交策略

每阶段至少一个独立提交，只有在该阶段验收通过后进入下一阶段。建议提交前缀：

- `build:` 阶段一；
- `feat(workspace):` 阶段二；
- `feat(growth):` 阶段三；
- `feat(evolution):` 阶段四、五、七；
- `feat(agents):` 阶段六；
- `docs:` 阶段八。

