# Personal Agent V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在单进程、本地单用户边界内交付一个可运行的个人目标 Agent V1，覆盖“澄清—计划—批准—执行—观察—复盘—确认记忆”的闭环，并让状态、审批、副作用、事件、统计、SSE、导出和确定性评测都可恢复、可审计、可验证。

**Architecture:** 后端使用 `backend/app` 中的薄层模块：SQLite WAL 负责结构化状态和 append-only 事实事件，Runtime 是唯一状态/工具/记忆协调者，模型网关只做 OpenAI-compatible Chat Completions 流式适配，工具经过 Registry/SafetyGate/Approval，Markdown 只保存已确认长期记忆。FastAPI 提供同源 REST + SSE，React + TypeScript 提供四个核心页面；所有确定性行为由 MockModelGateway/FakeToolRegistry 驱动，避免把 LLM 结果当成安全边界。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic、httpx、SQLite（标准库 `sqlite3`，WAL）、pytest；React 18、TypeScript、Vite、原生 `fetch`/`EventSource`；不引入 Redis、Celery、MCP、Shell 工具、RAG、向量库或额外服务。

---

## 文件边界

后端文件按单一职责创建：

- `backend/app/config.py`：环境变量、模型 Profile、Runtime/安全配置；只返回脱敏配置视图。
- `backend/app/db.py`：SQLite 连接、WAL、schema、事务上下文和启动扫描。
- `backend/app/models.py`：Pydantic API 模型与领域枚举/值对象。
- `backend/app/events.py`：事件信封、每 Run 连续 seq、事务内写入和脱敏 JSONL 导出。
- `backend/app/stats.py`：从完整事件日志增量/重建 `run_stats`，严格实现 Usage 替换、TTFT/TPS/cache 公式。
- `backend/app/domain.py`：状态机、计划版本、步骤状态、审批和 Checkpoint 的纯规则。
- `backend/app/model_gateway.py`：统一请求/流式响应、重试分类、Usage/Timing 归一化、Mock 网关。
- `backend/app/context.py`：显式 Scope 记忆选择、上下文优先级、一次性裁剪和请求快照。
- `backend/app/tools.py`：ToolRegistry、JSON 参数校验、SafetyGate、工作区路径检查和四个内置工具。
- `backend/app/memory.py`：候选记忆、Markdown 原子写入/版本/hash、确认/拒绝/停用/回滚及应用事件。
- `backend/app/runtime.py`：Interaction、澄清/规划/批准/顺序 ReAct/等待/复盘/恢复协调；模型永远只提交结构化意图。
- `backend/app/api.py`：Host/Origin/CSRF 校验、规格中 REST 路由和 SSE Last-Event-ID 补发。
- `backend/app/main.py`：FastAPI app factory、静态前端托管和本地启动入口。
- `backend/app/eval.py`、`backend/app/evals.py`：Invariant、12 个确定性场景、轨迹重放、live 报告和 compare CLI。
- `backend/tests/`：单元、集成、API、统计/导出安全和 12 场景测试；每个新行为先写失败测试。
- `backend/pyproject.toml`、`backend/requirements.txt`：最小运行/测试依赖和 pytest 配置。

前端文件按页面和可复用 API 边界创建：

- `frontend/package.json`、`frontend/tsconfig.json`、`frontend/vite.config.ts`、`frontend/index.html`：构建配置。
- `frontend/src/api.ts`、`frontend/src/types.ts`：同源 REST/SSE 客户端和共享类型。
- `frontend/src/App.tsx`、`frontend/src/styles.css`：一级导航、当前 Run 状态和本地布局。
- `frontend/src/pages/ChatPage.tsx`、`PlanPage.tsx`、`TrajectoryPage.tsx`、`MemoryPage.tsx`：四个规格页面。
- `frontend/src/components/StatsBar.tsx`、`EventStream.tsx`、`ApprovalCard.tsx`：统计、实时事件和审批交互。
- `frontend/src/__tests__/`：页面渲染、关键提交和事件续传测试；构建作为前端阶段的完整类型验证。

其他文件：

- `skills/goal-planning/SKILL.md`、`skills/reflection/SKILL.md`：V1 两个内置 Skill 的输入、输出、工具交集和停止条件。
- `evals/cases/v1.json`、`evals/baselines/README.md`：固定场景和 baseline 说明；运行结果继续由 `.gitignore` 排除。
- `scripts/start.py`：构建前端并启动仅监听 `127.0.0.1` 的 FastAPI。
- `README.md`：安装、配置、启动、测试、评测和 V1 非目标。

## Task 1: 建立最小可运行骨架和测试基线

**Files:**
- Create: `backend/pyproject.toml`, `backend/requirements.txt`, `backend/app/__init__.py`, `backend/app/main.py`, `backend/app/config.py`, `backend/tests/conftest.py`, `backend/tests/test_health.py`
- Create: `frontend/package.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/index.html`, `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/styles.css`
- Modify: `.gitignore`, `README.md`

- [ ] **Step 1: 写最小失败测试**：`test_health_returns_local_service_metadata` 使用 FastAPI `TestClient` 请求 `/api/health`，断言 `status=ok`、服务没有返回环境变量值，并断言 app 默认只允许 loopback Host。
- [ ] **Step 2: 运行红测试**：`cd backend; pytest tests/test_health.py -q`；预期因 `backend.app.main`、`/api/health` 尚不存在而失败。
- [ ] **Step 3: 写最小实现**：创建 FastAPI app factory，`/api/health` 只返回固定状态和版本；配置只读取 `AGENT_MODEL_API_KEY` 的存在性，不读取到 API 返回；加入 Vite React 入口和最小四页占位导航，所有页面先显示真实页面标题而不是空白占位文案。
- [ ] **Step 4: 运行绿测试和构建**：`cd backend; pytest -q`；`cd frontend; npm install; npm run build`；两者退出码均为 0。
- [ ] **Step 5: 提交**：`git add backend frontend .gitignore README.md; git commit -m "chore: bootstrap personal agent app"`。

## Task 2: SQLite WAL、领域表和 append-only 事件

**Files:**
- Create: `backend/app/db.py`, `backend/app/models.py`, `backend/app/events.py`, `backend/app/stats.py`
- Create: `backend/tests/test_db.py`, `backend/tests/test_events.py`, `backend/tests/test_redaction.py`
- Modify: `backend/app/main.py`, `backend/tests/conftest.py`

- [ ] **Step 1: 写失败测试**：覆盖 schema 表存在、`PRAGMA journal_mode` 为 `wal`、同一 Run 的事件 seq 为 `1..n` 且不能更新；覆盖事件信封包含 schema/version/correlation；覆盖脱敏副本隐藏 `api_key`、Authorization、Cookie、密码、环境变量值和工作区外绝对路径，异常时不回退原文。
- [ ] **Step 2: 运行红测试**：`cd backend; pytest tests/test_db.py tests/test_events.py tests/test_redaction.py -q`；预期因数据库/事件/导出模块不存在而失败。
- [ ] **Step 3: 最小实现**：用标准库 sqlite3 创建 `goals`、`sessions`、`runs`、`interactions`、`plan_versions`、`plan_steps`、`checkpoints`、`messages`、`events`、`approvals`、`memory_candidates`、`memory_file_versions`、`run_stats`，启用 WAL/foreign keys；事件写入在事务中用 `MAX(seq)+1` 并以 `(run_id,seq)` 唯一约束；加入 redacted JSONL 导出器，默认只输出长度/hash/summary/结构。
- [ ] **Step 4: 运行绿测试**：`cd backend; pytest tests/test_db.py tests/test_events.py tests/test_redaction.py -q`；预期全部通过并无泄漏。
- [ ] **Step 5: 提交**：`git add backend/app backend/tests; git commit -m "feat: add sqlite event store and redacted export"`。

## Task 3: 显式状态机、计划版本、审批和 Checkpoint

**Files:**
- Create: `backend/app/domain.py`, `backend/tests/test_state_machine.py`, `backend/tests/test_plan_versions.py`, `backend/tests/test_checkpoint.py`, `backend/tests/test_approval.py`
- Modify: `backend/app/models.py`, `backend/app/db.py`, `backend/app/events.py`

- [ ] **Step 1: 写失败测试**：逐条锁定规格状态转换；验证非法转换、`BLOCKED` 只能恢复到保存的 `resume_state`、终态不可恢复；验证计划修改必生成不可变新版本、已完成步骤保留、运行中的原子步骤完成后停止；验证审批绑定 `run_id/tool_call_id/params_hash`，参数变化或过期 Run 必须拒绝；验证 Checkpoint 含当前状态/版本/步骤/预算/观察/待审批/已应用记忆/最后 seq，并且恢复会跳过已成功 `tool_call_id`。
- [ ] **Step 2: 运行红测试**：`cd backend; pytest tests/test_state_machine.py tests/test_plan_versions.py tests/test_approval.py tests/test_checkpoint.py -q`；预期失败在未定义规则或存储接口处。
- [ ] **Step 3: 最小实现**：实现 `AgentState`、`transition()`、`PlanVersionService`、`ApprovalService`、`CheckpointStore`；所有写状态/领域行/事件/StatsProjector 调用共享事务；只允许 Runtime 调用领域状态更新。
- [ ] **Step 4: 运行绿测试**：同一组 pytest 命令；再运行 `cd backend; pytest -q`，修复回归后才进入下一任务。
- [ ] **Step 5: 提交**：`git add backend/app backend/tests; git commit -m "feat: enforce runtime state plans approvals and checkpoints"`。

## Task 4: Model Gateway、Usage/Timing 和上下文快照

**Files:**
- Create: `backend/app/model_gateway.py`, `backend/app/context.py`, `backend/tests/test_model_gateway.py`, `backend/tests/test_context.py`
- Modify: `backend/app/config.py`, `backend/app/events.py`, `backend/app/stats.py`

- [ ] **Step 1: 写失败测试**：固定 OpenAI-compatible SSE fixture，验证普通 delta/tool call/finish/usage；验证 `input_tokens` 含 cache 时互斥转换、reasoning 不重复累计、缺失 cache/首 Token/闭合事件为不可用；验证 429/5xx/timeout 最多按网络重试且不超过 `max_attempts`，401/403/key 缺失不重试，结构错误仅修复重试一次，取消保存事实事件；验证上下文按 1—7 优先级选记忆，裁剪只执行一次，快照 hash 不含 Key/隐藏推理。
- [ ] **Step 2: 运行红测试**：`cd backend; pytest tests/test_model_gateway.py tests/test_context.py -q`；预期因网关、归一化、ContextAssembler 尚不存在而失败。
- [ ] **Step 3: 最小实现**：用 httpx AsyncClient 实现单一 `/chat/completions` 流式协议和可注入 sleep/random；用 `ModelProfile` 读取 env key；定义 `UsageBuckets`、`Timing`、`ModelResponse`，将每次真实 HTTP 请求建为 `ModelAttempt`；实现 MockModelGateway；ContextAssembler 只做显式 Scope 选择、固定一次裁剪和 snapshot hash。
- [ ] **Step 4: 运行绿测试**：`cd backend; pytest tests/test_model_gateway.py tests/test_context.py -q`；再运行 `pytest -q`。
- [ ] **Step 5: 提交**：`git add backend/app backend/tests; git commit -m "feat: add compatible model gateway and context snapshots"`。

## Task 5: Tool Registry、SafetyGate、内置工具和 Markdown 记忆

**Files:**
- Create: `backend/app/tools.py`, `backend/app/memory.py`, `backend/tests/test_tools.py`, `backend/tests/test_memory.py`
- Create: `skills/goal-planning/SKILL.md`, `skills/reflection/SKILL.md`
- Modify: `backend/app/db.py`, `backend/app/events.py`, `backend/app/context.py`

- [ ] **Step 1: 写失败测试**：验证未知工具、Schema 错误、Skill 越权、`..`、绝对路径、符号链接逃逸和工作区外路径均被拒；PURE/READ 自动执行，WRITE 无审批永不执行，批准参数 Hash 改变必须重批；验证本地时间、计算器、Markdown read/write；验证 WRITE 成功后重试/恢复不重复副作用。
- [ ] **Step 2: 运行红测试**：`cd backend; pytest tests/test_tools.py tests/test_memory.py -q`；预期失败在 Registry/SafetyGate/Markdown 存储不存在处。
- [ ] **Step 3: 最小实现**：精确名称注册 `ToolSpec`，用 Pydantic 校验参数；路径统一 `resolve(strict=False)` 并检查工作区边界/符号链接；`ToolResult` 固定结构；计算器只允许 AST 数字运算；Markdown 使用临时文件 + `os.replace`，旧文件归档到 `.versions` 并记录 hash/version；候选状态只允许 proposed→confirmed/rejected/superseded/disabled，确认后从下一原子步骤应用，每次应用发 `memory.applied`。
- [ ] **Step 4: 运行绿测试**：`cd backend; pytest tests/test_tools.py tests/test_memory.py -q`；确认 `rg -n "AGENT_MODEL_API_KEY|api_key|Authorization|Cookie" backend/app backend/tests` 不会把 secret 原文写入事件/日志测试 fixture。
- [ ] **Step 5: 提交**：`git add backend/app backend/tests skills; git commit -m "feat: add safe tools and versioned markdown memory"`。

## Task 6: Runtime 纵向闭环、顺序 ReAct、预算与恢复

**Files:**
- Create: `backend/app/runtime.py`, `backend/tests/test_runtime.py`, `backend/tests/test_react_budget.py`, `backend/tests/test_restart_recovery.py`
- Modify: `backend/app/domain.py`, `backend/app/model_gateway.py`, `backend/app/tools.py`, `backend/app/memory.py`, `backend/app/context.py`, `backend/app/events.py`, `backend/app/stats.py`

- [ ] **Step 1: 写失败测试**：使用 MockModelGateway/FakeToolRegistry 写最小纵向闭环：创建 Goal→Interaction→Planning→PlanVersion 1→AwaitingApproval→批准→逐步 Executing→Reflecting→Completed；覆盖信息不足进入 Clarifying、计划修改创建版本、WRITE approval/rejection、`max_react_iterations_per_step=5` 耗尽后 Checkpoint + BLOCKED + 预算增加恢复、工具连续错误/重复动作/墙钟预算、AwaitingOutcome continue/finish、取消当前步骤；覆盖重启扫描非终态 Run 和副作用去重；覆盖候选确认后下一步骤的 Context 与 `memory.applied`。
- [ ] **Step 2: 运行红测试**：`cd backend; pytest tests/test_runtime.py tests/test_react_budget.py tests/test_restart_recovery.py -q`；预期因 Runtime 未实现而失败。
- [ ] **Step 3: 最小实现**：Runtime 以单个 Run 锁/执行协程为边界；每次原子步骤按观察→结构化决策→校验→单工具→观察；模型决策只能返回 `complete_step`、`continue`、`await_outcome`、`tool_call`、`blocked` 意图；每轮写 started/finished 事件；预算、审批和 Checkpoint 在副作用前后持久化；ReflectionWorker 只产生候选，不直接把推断写入长期记忆。
- [ ] **Step 4: 运行绿测试**：同一组 pytest 命令和 `pytest -q`；测试产出的临时 SQLite/Markdown 均在 tmp_path，不写入仓库 data/evals/results。
- [ ] **Step 5: 提交**：`git add backend/app backend/tests; git commit -m "feat: implement resumable sequential react runtime"`。

## Task 7: FastAPI REST、SSE、CSRF/Host 边界

**Files:**
- Create: `backend/app/api.py`, `backend/tests/test_api.py`, `backend/tests/test_sse.py`, `backend/tests/test_security.py`
- Modify: `backend/app/main.py`, `backend/app/runtime.py`, `backend/app/config.py`, `.gitignore`

- [ ] **Step 1: 写失败测试**：覆盖规格全部 REST 路由的核心成功/错误响应，计划/预算/记忆乐观版本冲突返回 409；修改接口缺少 JSON Content-Type 或 CSRF 返回 400/403；非 loopback Host/Origin 拒绝；`/events/stream` 只发布已提交事件，支持 `Last-Event-ID` 补发且 SSE id 等于事件 seq；API 响应和日志 fixture 不含 Key 原文。
- [ ] **Step 2: 运行红测试**：`cd backend; pytest tests/test_api.py tests/test_sse.py tests/test_security.py -q`；预期因路由和中间件不存在而失败。
- [ ] **Step 3: 最小实现**：用 app state 保存启动时 CSRF token（仅通过同源前端 meta/health 获取 token，不进日志）；实现 `/api/goals`、Runs、plans、approvals、events、stats、export、memories；SSE 使用 async generator 读取事件表，首次连接按 Last-Event-ID 补发，之后只读已提交事件；前端静态 dist 存在时挂载，否则保留 API/开发说明。
- [ ] **Step 4: 运行绿测试**：`cd backend; pytest tests/test_api.py tests/test_sse.py tests/test_security.py -q`；再运行 `pytest -q`。
- [ ] **Step 5: 提交**：`git add backend/app backend/tests .gitignore; git commit -m "feat: expose local rest and sse api"`。

## Task 8: React + TypeScript 四页 UI 和实时轨迹

**Files:**
- Create: `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/components/StatsBar.tsx`, `frontend/src/components/EventStream.tsx`, `frontend/src/components/ApprovalCard.tsx`, `frontend/src/pages/ChatPage.tsx`, `frontend/src/pages/PlanPage.tsx`, `frontend/src/pages/TrajectoryPage.tsx`, `frontend/src/pages/MemoryPage.tsx`, `frontend/src/__tests__/App.test.tsx`, `frontend/src/__tests__/api.test.ts`
- Modify: `frontend/src/App.tsx`, `frontend/src/styles.css`, `frontend/package.json`, `backend/app/main.py`

- [ ] **Step 1: 写失败测试**：用 Vitest/Testing Library 验证导航渲染“对话/计划/轨迹/记忆”、Chat 可创建目标/发送消息/显示状态和审批、Plan 显示当前及历史版本并提交批准/修订、Trajectory 显示 StatsBar/泳道/筛选并用 Last-Event-ID 订阅、Memory 显示候选确认/拒绝/编辑/停用/回滚。
- [ ] **Step 2: 运行红测试**：`cd frontend; npm test -- --run`；预期因页面/组件/客户端接口不存在而失败。
- [ ] **Step 3: 最小实现**：使用原生 `fetch`、`EventSource` 和 URL query，不引入状态管理库或 UI 框架；共享 API 类型与后端 JSON 字段一致；事件流按 `event.lastEventId` 去重；所有修改按钮带 CSRF/JSON；样式提供可读的四页桌面/窄屏布局和状态/风险色。
- [ ] **Step 4: 运行绿测试和构建**：`cd frontend; npm test -- --run; npm run build`；再从根目录运行后端测试。
- [ ] **Step 5: 提交**：`git add frontend backend/app/main.py; git commit -m "feat: add chat plan trajectory and memory ui"`。

## Task 9: 评测模型、12 个确定性场景、StatsProjector 重建

**Files:**
- Create: `backend/app/evals.py`, `backend/app/eval.py`, `backend/tests/test_invariants.py`, `backend/tests/test_eval_scenarios.py`, `backend/tests/test_stats.py`, `evals/cases/v1.json`, `evals/baselines/README.md`
- Modify: `backend/app/stats.py`, `backend/app/runtime.py`, `backend/app/events.py`, `README.md`

- [ ] **Step 1: 写失败测试**：实现并先运行 Invariant 测试，锁定状态转换、未批准计划/WRITE、未确认记忆、版本变化、预算、步骤终态、event.seq、ToolResult 关联和 API Key 脱敏；Stats 测试锁定 interaction/plan/attempt/tool/LLM 时间/TTFT/TPS/cache/token 的公式、替换 Usage、缺失数据不可用、未闭合 span 不计时。
- [ ] **Step 2: 运行红测试**：`cd backend; pytest tests/test_invariants.py tests/test_stats.py tests/test_eval_scenarios.py -q`；预期因评测注册和投影未完成而失败。
- [ ] **Step 3: 最小实现**：从事件重建 `run_stats`，提供 projection version/rebuild；定义 12 个固定场景（正常闭环、澄清、计划修改、步骤取消、WRITE 拒绝、未知工具、429 重试、鉴权 BLOCKED、预算追加、工具失败、偏好确认应用、重启恢复）；轨迹重放只读事件，场景重跑使用固定 mock；CLI 支持 `run --suite v1 --mode deterministic|live` 与 `compare`，live 无 evaluator 时只写人工 Markdown 报告。
- [ ] **Step 4: 运行绿测试**：`cd backend; pytest tests/test_invariants.py tests/test_stats.py tests/test_eval_scenarios.py -q`；`python -m app.eval run --suite v1 --mode deterministic`（从 `backend` 目录运行，结果写入被忽略的 `evals/results/`）；确认 12/12 通过且结果未进入 `git status`。
- [ ] **Step 5: 提交**：`git add backend/app backend/tests evals/cases evals/baselines README.md; git commit -m "feat: add deterministic v1 evaluation suite"`。

## Task 10: 本地一条命令、文档、安全审计和最终验证

**Files:**
- Create: `scripts/start.py`, `backend/tests/test_startup_contract.py`
- Modify: `backend/app/main.py`, `README.md`, `.gitignore`

- [ ] **Step 1: 写失败测试**：验证启动配置默认 host=`127.0.0.1`、前端 dist 存在时由 FastAPI 同源托管、`data/agent.db`/`data/memory`/`data/artifacts`/`evals/results` 被忽略；验证 `git diff --check` 和 secret scan 规则不会发现提交中的密钥/本地数据。
- [ ] **Step 2: 运行红测试**：`cd backend; pytest tests/test_startup_contract.py -q`；预期因启动脚本/静态挂载契约未完成而失败。
- [ ] **Step 3: 最小实现**：`scripts/start.py` 先执行前端 build，再以 `uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000` 启动；README 给出 Windows PowerShell 和跨平台命令、Profile/YAML/环境变量配置、恢复/导出/评测命令与明确 V1 非目标；只添加 `.env.example`，不添加真实 key/data。
- [ ] **Step 4: 最终验证**：运行 `cd backend; pytest -q`、`cd frontend; npm test -- --run; npm run build`、`git diff --check`、`git status --short --ignored`、`python -m app.eval run --suite v1 --mode deterministic`；核对 12/12、构建退出码 0、测试 0 failures、没有本地数据/密钥被跟踪。
- [ ] **Step 5: 提交**：`git add scripts backend frontend evals/cases evals/baselines README.md .gitignore; git commit -m "feat: complete local personal agent v1"`；提交后再次运行完整验证并记录分支、commit hash、启动方法和规格仍未实现项。

## 规格覆盖自审

- 模型网关/重试/非静默 fallback：Task 4、Task 9。
- 显式状态机/计划版本/ReAct/预算/Checkpoint/恢复：Task 3、Task 6。
- ToolRegistry/SafetyGate/WRITE 审批/路径安全/内置 Skill：Task 5、Task 7。
- SQLite WAL/事件 seq/StatsProjector/TTFT/TPS/cache/token/JSONL 脱敏：Task 2、Task 9。
- Markdown 长期记忆、候选状态、MemoryApplied、版本和回滚：Task 5、Task 6、Task 8。
- FastAPI REST/SSE/Last-Event-ID/CSRF/Host：Task 7。
- React + TypeScript 对话/计划/轨迹/记忆：Task 8。
- 12 个确定性场景和 CLI：Task 9。
- 本地单命令启动、127.0.0.1、忽略密钥/data/eval 结果：Task 1、Task 10。

已扫描本计划中的占位词、未定义接口和跨任务命名；实现顺序固定为基础设施→安全边界→Runtime→API/UI→评测→发布契约，任何阶段失败都先回到该阶段的红测试定位根因。
