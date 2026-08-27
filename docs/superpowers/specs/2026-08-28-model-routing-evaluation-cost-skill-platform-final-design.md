# Better Agent 多模型、真实评测、成本治理与 Skill 平台最终设计

> 状态：Final / Approved Architecture / Frozen Scope  
> 日期：2026-08-28  
> 适用仓库：`yurenwym/better-agent`  
> 实施基线：分支 `codex/personal-agent-v1`，提交 `00a9a04`  
> 规格属性：本阶段唯一正式产品与架构规格。实施时不得重新讨论已冻结范围；若实现与本文冲突，以本文为准。

## 1. 背景

Better Agent 已具备本地单用户个人 Agent 的主要闭环：目标对话、Ask 交互、计划文档、每日行动、复盘、长期记忆、深度研究、受控多 Agent、Checkpoint、审批、SSE 轨迹、Experience Observer、候选评测和 Canary。当前系统能够完成真实任务，但模型、成本和扩展能力仍停留在单配置或局部实现层面：

- `ModelGateway` 只直接处理 OpenAI-compatible 协议，重试被隐藏在一次返回值中；
- 模型角色、能力、价格和路由规则没有不可变版本与运行快照；
- `RealEvaluator` 仍以字符串相等为主要质量判断，不能支撑跨模型真实 A/B 结论；
- Token、TTFT、TPS 等统计来自事件投影，但没有统一的财务账本和预算预留；
- Skill 来自仓库目录，工具权限由 `_SKILL_TOOLS` 硬编码，无法安全安装、升级、授权和固定版本；
- 当前 Growth/Canary 已形成可信发布骨架，本阶段必须复用，不能再造第二套候选、审批或灰度系统。

本项目不是通用 Agent 云平台，而是一个可解释、可恢复、可审计的本地个人 Agent。本阶段的目标，是让它具备足以作为简历代表项目的模型控制平面、真实评测、成本治理与声明式扩展能力，同时保持单体架构和清晰安全边界。

## 2. 要解决的问题

### 2.1 模型调用不可证明

目前一次逻辑调用和一次真实网络请求没有被持久化区分。发生重试、结构修复或 fallback 后，无法准确回答：选中了哪个模型、为什么选中、实际请求了几次、每次消耗多少、在哪次开始输出，以及失败是否发生在副作用之前。

### 2.2 评测不足以支持进化结论

字符串相等只能验证少量确定性任务，无法评价计划质量、研究证据、Ask 交互、记忆使用和工具安全。若 Judge 与候选使用相同模型、可看到 A/B 身份或使用不同上下文，结论也不可信。

### 2.3 费用无法治理

缺少不可变价格快照、并发预算预留和 append-only 账本时，前端只能展示估算数字；重试、fallback、Judge 和 Usage 缺失都可能造成漏算或双算。

### 2.4 Skill 不是平台

目录扫描加硬编码工具集合无法支持安全安装、版本更新、权限 Diff、会话级选择、运行固定和卸载审计。若直接支持 Python、JavaScript、Shell 或安装脚本，又会把本地 Agent 变成任意代码执行器。

## 3. 产品目标

本阶段完成后，用户能够：

1. 在“模型”页面注册多个供应商模型，查看能力、可用状态和凭据配置状态；
2. 为对话、Ask、计划、执行、研究、专家、复盘和 Judge 配置确定性角色路由；
3. 在轨迹中看到每次 Invocation 的路由结果、真实 Attempt、重试/fallback 原因、Token、TTFT 和费用状态；
4. 设置单次、每日和每月预算，预算不足时在发起请求前失败关闭；
5. 用冻结黄金集执行真实的匿名配对跨模型评测，并将结果接入现有 Evolution/Canary；
6. 从 ZIP 安装声明式 Skill，先查看权限 Diff，再授权、启用、绑定到对话、更新或卸载；
7. 对 HTTP 工具保持 HTTPS、可信 Connector、DNS/IP 绑定、响应限额和逐次 WRITE 审批；
8. 在不泄露密钥的前提下导出脱敏的路由、评测、费用和 Skill 审计证据。

## 4. 非目标与禁止项

本规格明确不做：

- 微服务拆分、Redis、Celery、Kafka 或分布式调度；
- RAG、MCP、Shell 工具；
- 任意 Python/JavaScript/WASM 插件、安装脚本、生命周期 Hook 或依赖树；
- 工具并行；
- 让 LLM 自己选择供应商或在线修改路由权重；
- 跨供应商静默 fallback；
- 根据单一加权分数自动切换“最优模型”；
- 无人工审批的完全自主进化；
- 将 CI、Docker 和演示交付作为本阶段内容；
- 把 Mock、Echo、字符串相等、前端费用估算或只复制目录表述为真实验收完成。

## 5. 当前代码基线与复用边界

实施必须复用以下现有模块：

| 现有能力 | 文件 | 本阶段处理 |
|---|---|---|
| OpenAI-compatible 流式网关 | `backend/app/model_gateway.py` | 拆为统一 Gateway、Router 和单 Attempt Adapter |
| Runtime 模型适配 | `backend/app/live_model.py` | 所有角色调用改走统一 Invocation |
| Run/Thread 事件与 SSE | `backend/app/events.py`、`backend/app/api.py` | 复用；不新增 `model_events` 表 |
| StatsProjector | `backend/app/stats.py` | 改为从 Attempt 与成本账本投影模型指标 |
| 工具安全、审批和执行 Claim | `backend/app/tools.py`、`backend/app/domain.py` | 扩展授权绑定，不绕过现有机制 |
| 目录 Skill | `backend/app/skill_registry.py` | 删除 `_SKILL_TOOLS`，升级为版本化平台 |
| 真实评测最小实现 | `backend/app/real_evaluation.py` | 替换为配对执行、Judge 和统计报告 |
| Behavior Bundle | `backend/app/behavior.py` | 保存不可变路由策略与 Skill 快照引用 |
| Evolution/Canary | `backend/app/evolution.py` | 扩展 `policy` 候选，复用既有审批和灰度 |
| Growth UI | `frontend/src/pages/GrowthPage.tsx` | 增加真实评测、成本和 Canary 证据 |

数据库 migration `1..8` 已存在，其中 7 用于 Experience Observer，8 用于 Canary Exposure 修复。旧 migration 内容和 checksum 一律不得修改。

## 6. 冻结架构决策

### 6.1 保持单体

继续使用 FastAPI 单体、SQLite WAL、现有 Worker、lease、heartbeat、Checkpoint、append-only 事件与 SSE。模块通过同一数据库事务协作，不引入消息中间件。

### 6.2 三种协议、两种真实协议验收

代码必须完整实现并通过契约测试：

- OpenAI-compatible Chat Completions；
- Anthropic Messages；
- Gemini GenerateContent。

真实联网验收要求至少两种不同协议、三个模型配置成功；同一 OpenAI-compatible 协议下配置三个模型不等于两种协议。第三种协议缺少外部凭据时记录 `NOT_RUN_EXTERNAL_CREDENTIAL_MISSING`，不冒充成功，也不阻塞本地交付。

### 6.3 单一事实源

| 数据 | 唯一权威 |
|---|---|
| Invocation 身份、冻结路由、逻辑状态 | `model_invocations` |
| Attempt 状态、原始 Usage、TTFT、错误 | `model_attempts` |
| 预留、结算、释放和实际费用 | append-only `cost_ledger` |
| 用户轨迹、审计顺序与 SSE | 现有 `events` / `thread_events` |
| UI 聚合指标 | 可删除、可重建的 `StatsProjector` 投影 |

不创建 `model_events`、`model_routing_events` 或 `model_routing_channels`。事件只携带状态变化和权威记录 ID，不保存第二份可独立结算的 Token 或费用。

### 6.4 复用现有 Evolution/Canary

路由策略是 Runtime Bundle 的不可变引用，使用现有 `policy` Candidate：

```text
Experience → policy Candidate → 基线/候选真实评测
           → 人工审批 → 现有 Canary → 晋升或回滚 Bundle
```

不增加候选类型、不增加路由专用 Channel、不增加第二套 Canary。在途 Run 固定创建时 Bundle；只有新 Run 参与 Canary 分流。

### 6.5 声明式 Skill，不执行任意代码

Skill ZIP 只包含声明、提示文档、静态资产、评测样例，以及对内置工具或可信 HTTP Connector 的引用。安装 Skill 永远不会执行包内代码。

## 7. 总体架构

```text
Conversation / Research / Program / Multi-Agent / Evaluator
                         │ role + requirements + Runtime Bundle
                         ▼
                   ModelRouter
           capability filter → deterministic policy
                         │ immutable route snapshot
                         ▼
                   ModelGateway
      Invocation ──┬── Attempt 1 / ProviderAdapter
                   ├── Attempt 2 / retry
                   └── Attempt 3 / explicit fallback
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
   EventStore/SSE   CostLedger      StatsProjector

Skill ZIP → PackageValidator → SkillRegistry → Grant/Binding Snapshot
                                                  │
Global Tools ∩ Manifest ∩ User Grant ∩ Agent Role ∩ Phase
                                                  │
                                         ToolRegistry re-check
                                                  │
                            Built-in Tool / Trusted HTTPS Connector
```

## 8. 多模型注册

### 8.1 Model Profile

`model_profiles` 表示用户可识别的逻辑模型；`model_profile_versions` 是不可变执行配置。更新 endpoint、模型名、能力、上下文窗口或凭据引用时必须创建新版本。

版本字段至少包含：

- `provider_protocol`: `openai_compatible | anthropic | gemini`；
- `provider_name` 与用户显示名；
- `base_url`、`model_name`；
- `credential_env_ref`，只保存环境变量名；
- `capabilities_json`；
- `context_window`、`max_output_tokens`；
- timeout、同模型重试上限；
- `config_digest`、状态和创建时间。

能力键冻结为：

```json
{
  "text": true,
  "streaming": true,
  "tool_calling": true,
  "json_object": true,
  "json_schema": false,
  "vision": false,
  "cache_usage": false,
  "reasoning_usage": false
}
```

凭据值不得进入 SQLite、日志、事件、导出、异常消息或前端响应。健康检查只返回 `CONFIGURED / MISSING / VERIFIED / FAILED` 和脱敏错误类别。

### 8.2 ProviderAdapter 契约

每个 Adapter 的一次 `execute_attempt()` 只允许发起一次真实 HTTP 请求，不在内部重试。统一输入输出包含：

- 标准 message、system、tool schema、temperature、max tokens；
- `TEXT_DELTA`、`TOOL_CALL_DELTA`、`USAGE_SNAPSHOT`、`FINISH`；
- 标准 `UsageBuckets`；
- provider request id；
- 标准错误分类。

三种 Adapter 必须统一验证流式文本、工具调用参数拼接、结构化输出、Usage、取消、超时和错误分类。原始供应商响应只能进入受限调试日志，默认不持久化。

### 8.3 错误分类

冻结分类：

- 可进入显式 fallback：`timeout`、`rate_limit`、`server`、`provider_unavailable`；
- 只允许同模型受限修复：`structure`、`context_overflow`；
- 立即失败：`authentication`、`configuration`、`request`、`safety`、`cancelled`；
- 状态不明：`transport_unknown`，若调用可能包含 WRITE 工具则进入 reconciliation。

## 9. 能力路由

### 9.1 角色

路由角色固定为：

- `conversation`
- `ask`
- `planner`
- `executor`
- `reflector`
- `researcher`
- `expert`
- `coordinator`
- `judge_quality`
- `judge_safety`

### 9.2 决策过程

Router 不调用 LLM。它按以下顺序确定模型：

1. 读取 Run 固定的 Runtime Bundle；
2. 读取 Bundle 引用的不可变 Routing Policy；
3. 根据角色取得候选 Profile Version；
4. 过滤不满足 streaming、tools、JSON、vision、context 等硬能力的候选；
5. 检查健康状态、请求预算和用户禁用项；
6. 按策略中明确的整数优先级和稳定 ID 排序；
7. 保存完整 Route Snapshot 后创建 Invocation；
8. 执行主模型；只有策略显式允许且错误可 fallback 时，才尝试后续模型。

策略必须包含角色绑定、能力要求、主模型、可选 fallback 顺序、最大 Attempt、超时和 Digest。不得保存模糊自然语言权重。

### 9.3 fallback 安全边界

默认关闭跨模型 fallback。开启时必须在设置页清晰显示，并在轨迹中显示原模型、目标模型和原因。

一旦本 Invocation 发生以下任一情况，禁止跨模型 fallback：

- 已向用户发送任意文本；
- 已发出完整或部分工具调用；
- 已创建审批请求；
- 已进入任何可能产生外部副作用的 Commit Gate。

同模型重试和跨模型 fallback 都创建新的 Attempt，并分别计费。禁止根据最终 `response.attempts` 事后反推 Attempt。

## 10. Invocation 与 Attempt

### 10.1 Invocation

Invocation 是一次逻辑模型请求，字段至少包含：

```text
id, owner_id, run_id, thread_id, turn_id, agent_task_id
role, purpose, runtime_bundle_id, routing_policy_id, routing_policy_digest
route_snapshot_json, request_digest, tool_schema_digest, context_snapshot_digest
status, selected_attempt_id, idempotency_key, created_at, finished_at
```

状态机：

```text
CREATED → ROUTED → RUNNING → SUCCEEDED
                         ├→ FAILED
                         ├→ CANCELLED
                         └→ BUDGET_BLOCKED
```

### 10.2 Attempt

Attempt 是一次真实供应商 HTTP 请求，字段至少包含：

```text
id, invocation_id, ordinal, reason(primary|retry|fallback|repair)
profile_version_id, provider_protocol, provider_request_id
request_digest, status, error_kind, http_status
input/cache_read/cache_write/output/reasoning tokens
started_at, first_token_at, finished_at, usage_status, usage_digest
```

`UNIQUE(invocation_id, ordinal)`；每个 Attempt 只允许一个最终 Usage 快照。流式 Usage 是累计值，只保存最后一个有效快照，绝不累加快照。

Attempt 终态、最终 Usage、费用结算和对应事件追加必须在同一 SQLite 事务中完成。重放同一 idempotency key 必须返回原记录。

### 10.3 时间公式

- `TTFT = first_token_at - started_at`；没有文本 Token 时为 `null`；
- `decode_seconds = finished_at - first_token_at`；
- `TPS = output_tokens / decode_seconds`，任一值缺失或 `decode_seconds <= 0` 时为 `null`；
- `model_work_seconds = Σ Attempt duration`；
- `invocation_wall_seconds = Invocation finished_at - created_at`；
- 多 Agent 页面另展示 `critical_path_seconds`，不能把并发耗时相加冒充用户等待时间。

## 11. 价格、成本账本与预算

### 11.1 价格快照

`model_price_snapshots` 不可变，按 Profile Version 和生效时间保存各 Token 桶的 `microusd_per_million_tokens`。金额全部使用整数 `microusd`，禁止浮点金额。

本地估算公式：

```text
estimated_microusd = ceil(
  Σ(tokens_bucket × rate_microusd_per_million_tokens) / 1_000_000
)
```

若供应商返回可信账单金额，使用供应商金额；否则使用本地价格快照估算。两者只能二选一，不能相加。

### 11.2 费用状态

- `PROVIDER_REPORTED`
- `ESTIMATED_COMPLETE`
- `ESTIMATED_PARTIAL`
- `UNAVAILABLE`

Token 或价格缺失时绝不能显示为 0。请求已发送但 Usage 丢失时，按该 Attempt 的保守预留额结算，并标记 `ESTIMATED_PARTIAL`。

### 11.3 append-only 账本

`cost_ledger` 条目类型固定为 `RESERVE | CHARGE | RELEASE | ADJUSTMENT`。账本只追加，不更新、不删除；每条包含 owner、预算周期、Invocation、Attempt、价格快照、金额、状态、原因、幂等键和时间。

约束：

- `UNIQUE(attempt_id, entry_type, price_snapshot_id)` 防止重复结算；
- Attempt 开始前在事务中预留下一个 Attempt 的最坏费用；
- Attempt 结束后写 CHARGE 并 RELEASE 差额；
- 下一次 retry/fallback 在开始前重新预留；
- Judge、结构修复、失败调用和取消前已经产生的消耗均计费；
- `cost_budgets` 只保存 CAS 汇总和版本，可由账本审计重建；
- Stats 只汇总账本，不重新套价格公式。

预算层级固定为：Invocation 上限、每日上限、月度上限。任何一层无法完成预留时都不发起网络请求，并返回可解释的 `BUDGET_BLOCKED`。

## 12. 跨模型真实评测

### 12.1 黄金集

冻结 60 个唯一 Case，分为五个领域，每个领域 12 个：

| 领域 | DEV | HOLDOUT | SAFETY | 合计 |
|---|---:|---:|---:|---:|
| 对话与 Ask | 4 | 6 | 2 | 12 |
| 计划生成、执行与调整 | 4 | 6 | 2 | 12 |
| 深度研究与引用 | 4 | 6 | 2 | 12 |
| 工具、审批与恢复 | 4 | 6 | 2 | 12 |
| 记忆与个性化 | 4 | 6 | 2 | 12 |
| 合计 | 20 | 30 | 10 | 60 |

`DISCOVERY` 数据不属于冻结 60 Case，只用于 Experience 分析和候选生成。候选生成只能读取 DISCOVERY/DEV；HOLDOUT 和 SAFETY 的答案、Rubric 与历史结果对候选不可见。

### 12.2 公平配对

每个 Case 的 A/B 必须使用相同：

- 用户输入与冻结上下文快照；
- system/Skill/Memory Bundle；
- 工具 Schema 与确定性 Fixture；
- max token、时间和费用预算；
- 温度与采样配置；
- 评测代码和 evaluator digest。

A/B 执行顺序通过 `hash(eval_run_id + case_id)` 平衡，避免顺序偏差。真实评测调用真实模型 API，但所有 WRITE 工具使用无副作用 Fixture，禁止评测污染用户数据或外部系统。

### 12.3 评判顺序

1. 确定性检查：Schema、引用可解析、工具审批、不重复副作用、敏感信息和任务硬约束；
2. Quality Judge：只看用户可见输出，A/B 匿名并随机交换位置；
3. Safety Judge：独立模型和独立 Prompt，不得是 A/B 任一模型；
4. 统计汇总：质量胜/平/负、配对成功率差、成本、TTFT、P95 和 Bootstrap 95% CI。

Judge Invocation 同样进入统一调用账本和成本预算。Judge 配置、Prompt、Rubric、顺序随机种子和结果均保存 Digest。

### 12.4 发布门禁

Candidate 在创建评测前声明唯一主要目标：质量、延迟或成本。禁止事后挑选最好看的指标。

进入人工审批必须同时满足：

- 所有确定性硬检查通过；
- 10 个 SAFETY Case 全部通过且相对基线无安全回归；
- 30 个 HOLDOUT 的主要质量指标不劣于基线；
- 至少 20 个非平局质量 Judgment，否则标记“证据不足”；
- 预先声明的主要目标达到阈值，且配对 Bootstrap 95% CI 不跨越该阈值；
- 评测、模型、工具、上下文、价格和报告 Digest 完整；
- Judge 缺失、证据不完整或 Usage/费用异常时失败关闭。

领域切片只作描述，样本不足时必须写“结果不确定”。报告只能声明“在该冻结评测集上改善”，不得外推成供应商总体排名。

## 13. Evolution 与 Canary 集成

### 13.1 Bundle 结构

Runtime Bundle Manifest 增加：

```json
{
  "model_routing": {
    "policy_id": "routing_policy_xxx",
    "digest": "sha256..."
  },
  "skills": [
    {"skill_version_id": "skill_version_xxx", "package_digest": "sha256..."}
  ]
}
```

### 13.2 policy Candidate

现有 `policy` Candidate 可修改 `model_routing` 和 `model_role_bindings`，不得借路由候选扩大工具、网络、Skill 或 WRITE 权限。当前 `evolution.py` 只允许 prompt 进入部分在线流程，实施时必须增加受约束的 policy Runtime Contract 测试。

### 13.3 Canary

复用 `runtime_channels`、`canary_deployments` 和 `canary_exposures`。Exposure 固定完整 Bundle，因此自然固定路由和 Skill 版本。扩展 Exposure 完成指标：

- quality outcome；
- safety outcome；
- TTFT/P95；
- Invocation/Attempt 数；
- `cost_microusd`；
- routing policy、profile 和 Skill Digest。

任何 challenger 安全失败必须在同一事务中停止分流、回滚稳定 Bundle 并追加 `evolution.canary.auto_rolled_back`。晋升仍需用户明确确认。

## 14. Skill 包格式

### 14.1 ZIP 结构

```text
skill.zip
├─ skill.json
├─ SKILL.md
├─ assets/          # 可选静态文件
└─ evals/           # 可选声明式样例
```

`skill.json` 必填字段：

```json
{
  "schema_version": 1,
  "name": "travel-planner",
  "version": "1.2.0",
  "title": "旅行计划",
  "description": "生成可执行的旅行计划",
  "requested_tools": ["local_time", "calculator"],
  "connectors": [],
  "phases": ["conversation", "planner"],
  "entry_document": "SKILL.md"
}
```

不支持依赖、安装命令、可执行入口和 Hook。

### 14.2 包安全

安装前必须流式校验：

- ZIP 压缩包不超过 10 MB；
- 解压后不超过 25 MB；
- 文件数不超过 200；
- 单文件不超过 2 MB；
- `SKILL.md` 不超过 256 KB；
- 拒绝绝对路径、`..`、盘符、符号链接、硬链接和重复规范化路径；
- 拒绝 ZIP Bomb、加密 ZIP、非法 UTF-8 Manifest 和未知 schema version；
- 计算 `manifest_digest` 与 `package_digest`。

包保存到 `data/skills/{name}/{version}/{package_digest}/`。相同 name/version 不同 Digest 视为篡改并拒绝；相同 Digest 重装必须幂等。

### 14.3 生命周期

状态：`INSTALLED → ENABLED ↔ DISABLED → UNINSTALLED`。Skill Version 永远不可变；已被历史 Run 引用的版本只做 tombstone，不物理删除。更新先安装新版本、展示权限 Diff，经用户授权后才可设为默认；在途 Run 保持旧版本。

## 15. 五层权限与运行固定

有效工具集合是以下五层交集：

```text
Global Tool Registry
∩ Skill Manifest requested_tools
∩ User Grant
∩ Agent Role Allowlist
∩ Current Phase Allowlist
```

任何一层缺失、格式错误或无法解析都返回空集，绝不失败开放。模型只收到有效集合对应的 Tool Schema；执行前 `ToolRegistry.authorize()` 必须基于同一冻结快照再次检查，不能相信模型输出。

每次 Run/Turn 固定：

- `skill_version_id`；
- `package_digest`；
- `grant_snapshot_json` 与 `grant_snapshot_digest`；
- `routing_policy_id` 与 Digest；
- 有效 Tool Schema Digest。

删除 `backend/app/skill_registry.py` 中 `_SKILL_TOOLS`。内置 Skill 也必须通过同一 Manifest、版本和 Grant 流程，不留特权旁路。

WRITE 审批仍逐次发生，不提供“永久允许”。授权必须精确绑定：

```text
owner_id + run_id + tool_call_id + tool_name + normalized_params_hash
+ skill_version_id + package_digest + grant_snapshot_digest + routing_policy_digest
```

参数、Skill、策略或目标对象任一变化都必须重新审批。Execution Claim、Checkpoint 和幂等回执继续防止并发、恢复或重放导致重复副作用。

## 16. 声明式 HTTP 工具安全

### 16.1 Trusted Connector

Skill 不直接声明任意 URL，只引用项目维护的 Trusted Connector。Connector 保存：

- 固定 `https` base URL；
- 精确域名，不允许通配符；
- 只允许标准 443 端口；
- 方法、路径模板和 JSON Schema；
- credential 环境变量引用；
- timeout、请求/响应大小限制；
- risk 等级和版本 Digest。

Manifest 声明和宿主 Connector 注册必须同时允许本次请求。

### 16.2 SSRF 与传输约束

每次请求必须：

1. 拒绝 userinfo、IP 字面量、localhost、非 HTTPS、非标准端口和 URL 覆盖 Host；
2. 禁用所有重定向，`trust_env=False`；
3. 解析全部 A/AAAA，任一地址为 loopback、私网、链路本地、保留、多播或 IPv4-mapped IPv6 私网时整次拒绝；
4. 固定本次校验通过的公网 IP，TCP 实际连接该 IP，TLS SNI、Host 和证书校验仍使用原域名；
5. 连接后校验 Peer IP 属于已验证集合；
6. 每次 retry 重新解析、重新校验和重新绑定；
7. 限制 DNS 结果数、连接/读取超时和响应字节数。

如果绑定 Transport 和 Peer IP 校验未完成，HTTP Connector 必须保持 feature-disabled，不能以弱 DNS 预检形式交付。

HTTP `POST/PUT/PATCH/DELETE` 强制标为 WRITE。连接中断导致 WRITE 结果不明时进入 `RECONCILIATION_REQUIRED`，不得自动重试。工具响应统一标记为“不可信外部数据”，不能覆盖 system 指令、权限或审批状态。

## 17. 数据库 Migration

### 17.1 Migration 9：模型与路由

新增：

- `model_profiles`
- `model_profile_versions`
- `model_routing_policies`
- `model_invocations`
- `model_attempts`

所有 Version、Policy、Invocation 路由快照和 Attempt 请求绑定均不可变；只允许更新状态、终态时间和最终 Usage 等明确可变列，并用 trigger 保护冻结列。

### 17.2 Migration 10：价格、成本与真实评测

新增：

- `model_price_snapshots`
- `cost_budgets`
- `cost_ledger`
- `evaluation_runs`
- `evaluation_case_pairs`
- `evaluation_arm_results`
- `evaluation_judgments`

为 `model_attempts` 增加 `price_snapshot_id` 和结算状态；为 `canary_exposures` 增加质量、延迟、费用和 Digest 字段。`evolution_evaluations` 继续保存不可变最终摘要和报告 Digest，不复制每个 Case 详情。

### 17.3 Migration 11：Skill 与 Connector

新增：

- `skills`
- `skill_versions`
- `skill_grants`
- `skill_bindings`
- `trusted_connectors`
- `trusted_connector_versions`
- `skill_events`

`skill_events` 只记录安装、授权、启停、绑定、更新和卸载审计，不复制工具执行结果。工具调用继续使用现有 `tool_calls`、`tool_execution_claims` 和 Run/Thread EventStore。

旧 migration `1..8` 不修改、不重排、不复用编号。全新库和升级库必须得到相同 schema。

## 18. 事件契约

运行事件复用现有 seq 递增 EventStore：

- `model.invocation.created`
- `model.route.selected`
- `model.attempt.started`
- `model.output.started`
- `model.attempt.retry_scheduled`
- `model.fallback.selected`
- `model.attempt.finished`
- `model.invocation.finished`
- `cost.budget_reserved`
- `cost.settled`
- `cost.budget_blocked`
- `evaluation.run.started`
- `evaluation.case.finished`
- `evaluation.run.finished`
- `skill.snapshot_applied`
- `tool.authorization.denied`
- `tool.reconciliation_required`

每个事件至少包含 `schema_version`、`event_id`、stream seq、owner、Run/Thread、Invocation/Attempt 或 Skill Version ID、actor、occurred_at、correlation 和最小 data。事件 seq 由数据库事务分配；重试、进程恢复和 SSE 重连不得产生重复 seq 或重复逻辑事件。

事件中只显示模型名、角色、错误类别、Token/费用状态和引用 ID；不得包含 API Key、Authorization、完整请求 Prompt、未脱敏工具结果或 Memory 私密正文。

## 19. HTTP API

### 19.1 模型与路由

- `GET/POST /api/model-profiles`
- `GET /api/model-profiles/{id}`
- `POST /api/model-profiles/{id}/versions`
- `POST /api/model-profile-versions/{id}/verify`
- `POST /api/model-profile-versions/{id}/disable`
- `GET/POST /api/model-routing-policies`
- `GET /api/model-routing-policies/{id}`
- `POST /api/model-routing-policies/{id}/candidate`

### 19.2 成本

- `GET/PUT /api/cost/budgets`
- `GET /api/cost/summary?from=&to=&role=&model=`
- `GET /api/model-invocations/{id}`
- `GET /api/model-invocations/{id}/attempts`
- `GET /api/cost/export`（脱敏）

### 19.3 评测

- `GET /api/evaluation-suites`
- `POST /api/evaluation-runs`
- `GET /api/evaluation-runs/{id}`
- `POST /api/evaluation-runs/{id}/cancel`
- `GET /api/evaluation-runs/{id}/report`
- `GET /api/evaluation-runs/{id}/events/stream`

### 19.4 Skill 与 Connector

- `GET /api/skills`
- `POST /api/skills/install`（multipart ZIP，先校验后返回权限预览）
- `POST /api/skills/{id}/confirm-install`
- `GET /api/skills/{id}/versions`
- `PUT /api/skill-versions/{id}/grant`
- `POST /api/skill-versions/{id}/enable`
- `POST /api/skill-versions/{id}/disable`
- `DELETE /api/skills/{id}`
- `PUT /api/threads/{thread_id}/skills`
- `GET/POST /api/trusted-connectors`
- `POST /api/trusted-connector-versions/{id}/verify`

所有 mutation 复用 CSRF、owner scope、expected version 和 Idempotency-Key。冲突返回 409，权限拒绝返回 403，预算不足返回 402，外部凭据缺失返回可机器识别的 422，不返回密钥值。

## 20. 前端信息架构

### 20.1 模型页 `/models`

- 模型列表、协议、能力徽标、凭据状态和验证结果；
- 角色路由矩阵：角色 → 主模型 → 显式 fallback；
- 保存路由时展示能力冲突与不可变版本；
- 不在浏览器保存 API Key，界面只指导配置环境变量；
- 轨迹入口可查看某 Invocation 的 Attempts。

### 20.2 用量页 `/usage`

- 今日/月度已结算、已预留、未知费用；
- 按角色、模型、供应商分组；
- TTFT、TPS、P95、成功率、fallback 率；
- 预算设置与阻断记录；
- `UNAVAILABLE` 和 `ESTIMATED_PARTIAL` 必须显式显示，不能渲染为 ¥0。

### 20.3 评测与 Growth

Growth 页沿用“经验 → 候选 → 评测 → 审批 → Canary → 晋升”主线。评测详情用独立 `/evaluations/{id}` 页面展示：

- 冻结 Suite 与 Digest；
- A/B 模型仅在评测结束后揭盲；
- 硬检查、质量 Judge、安全 Judge；
- HOLDOUT 胜/平/负、CI、TTFT、P95 和成本；
- 失败门禁和“证据不足”原因；
- 与 Candidate、Approval、Canary 的可点击关联。

### 20.4 Skill 页 `/skills`

- 已安装 Skill 从上到下列出名称、版本、状态和来源 Digest；
- 点击后查看渲染后的 SKILL.md、请求工具、Connector、阶段和权限；
- 安装/更新先展示权限 Diff，再确认；
- 启用、禁用、更新和卸载均使用自定义对话框与居中偏上 Toast，不使用原生弹窗；
- 对话输入区现有 Skill 选择器读取已启用且已授权版本；
- 正在运行的会话显示固定版本，不能被更新悄悄替换。

四个模块必须有真实 UI；只有后端接口或占位卡片不算完成。

## 21. TDD 实施顺序

### Slice 0：特征测试与边界锁定

先补当前 Gateway、Stats、Evolution、Tool Approval、Checkpoint 和 Skill 目录行为的特征测试。保存 migration 1..8 checksum。此 Slice 不改变产品行为。

### Slice 1：Migration 9 与统一调用账本

先写失败测试：Invocation/Attempt 状态转换、幂等、取消、Usage 最终快照、事件 seq、恢复后不重复 Attempt。实现三种 Adapter 契约和 Model Profile/Policy Repository，再将对话最小闭环切到统一 Gateway。

验收：三种协议 MockTransport 契约全通过；一次流式对话可在 UI 看到 Invocation 和 Attempt。

### Slice 2：所有模型角色接入路由

依次接入 Ask、计划、执行、复盘、研究、专家、协调器和 Judge。删除 Runtime 事后根据 `response.attempts` 反推 Attempt 的逻辑。

先写失败测试：能力不匹配、确定性选择、显式 fallback、文本/工具输出后禁止 fallback、同模型 retry 分 Attempt、在途 Bundle 固定。

### Slice 3：Migration 10 与成本治理

先写失败测试：价格版本固定、整数公式、并发预算 CAS、预留/结算/释放、Usage 丢失保守结算、retry/fallback/Judge 分别计费、重放不双算、未知费用不为 0、脱敏导出。

验收：所有真实模型 Attempt 都有费用状态；账本可重放校验；预算阻断发生在网络请求前。

### Slice 4：真实配对评测

先实现冻结 Case Schema、分区访问边界和确定性 Fixture；再实现 A/B 执行、匿名 Judge、Safety Judge、统计报告和 SSE。

先写失败测试：候选读取 HOLDOUT 被拒绝、A/B 输入不一致被拒绝、Judge 与 A/B 模型相同被拒绝、身份泄露、少于证据门槛、安全失败、Digest 不完整、取消和预算耗尽恢复。

### Slice 5：Evolution/Canary 接入

扩展现有 `policy` Candidate 的 Runtime Contract 和 Canary 指标，不创建新闭环。

先写失败测试：权限扩大被拒绝、未通过真实评测不能审批、在途 Run 不换 Bundle、Exposure 幂等、安全失败原子回滚、晋升后 stable Channel 更新。

### Slice 6：Migration 11 与 Prompt-only Skill

先写恶意 ZIP、Digest 篡改、同版本不同内容、权限交集失败关闭、版本固定和卸载 tombstone 测试。实现安装、权限 Diff、Grant、Binding 和内置 Skill 迁移，删除 `_SKILL_TOOLS`。

验收：用户可安装一个不含工具的 Skill，绑定到新对话，运行轨迹显示固定版本，更新不影响在途对话。

### Slice 7：内置工具和 HTTPS Connector

先接入内置 PURE/READ 工具，再实现绑定 DNS 的 HTTPS Transport。未完成 Peer IP 校验前保持 HTTP feature flag 关闭。

先写失败测试：路径穿越、ZIP Bomb、私网/IPv6/重绑定、重定向、代理继承、超时、超大响应、Schema 越权、WRITE 审批绑定变化、状态不明 WRITE reconciliation 和 Checkpoint 防重复副作用。

### Slice 8：四个真实前端模块

实现 `/models`、`/usage`、评测详情和 `/skills`，并扩展轨迹/Growth。每个危险动作使用应用内 ConfirmDialog 和 Toast，完成键盘操作、焦点、ARIA、空状态、加载、失败和响应式布局测试。

### Slice 9：真实验收与证据归档

使用环境变量加载测试凭据，不读取或提交密钥文件。完成：

- 三协议契约测试；
- 至少两协议、三个模型配置真实烟测；
- 冻结 60 Case 的完整配对运行；
- 一个 Prompt-only Skill 和一个可信 HTTP Skill 的安装到卸载全流程；
- 预算耗尽、取消恢复、fallback、WRITE 审批、SSRF 和 Canary 回滚故障注入；
- 后端全量测试、前端全量测试、TypeScript 检查和 Vite 生产构建。

真实评测结果、数据库、`data/`、Memory 和密钥均不提交；仓库只提交脱敏报告模板、固定 Fixture 和可复现命令。

## 22. 验收矩阵

| 能力 | 必须证据 |
|---|---|
| 三协议 Adapter | 契约测试覆盖 stream/tools/usage/error/cancel/schema |
| 真实模型 | 两种协议、三个模型配置的脱敏 Invocation/Attempt 记录 |
| 路由 | 角色能力过滤、确定性结果、路由快照与显式 fallback 测试 |
| 调用账本 | 100% 模型调用关联 role/policy/profile/Invocation/Attempt |
| 成本 | 100% Attempt 有费用状态，账本幂等和预算并发测试 |
| 评测 | 60 Case Digest、匿名 A/B、独立 Judge、Safety 硬门禁与 CI |
| Evolution | policy Candidate 复用现有审批/Canary/回滚的 E2E |
| Skill 安装 | ZIP 安全、Digest、权限 Diff、版本固定、卸载 tombstone |
| HTTP 工具 | DNS/IP 绑定、Peer IP、SSRF、WRITE reconciliation 测试 |
| UI | 模型、用量、评测、Skill 四个页面组件与浏览器行为测试 |
| 脱敏 | API Key、Authorization、私密 Memory、工具结果导出测试 |

## 23. 禁止冒充完成

出现以下任一情况，本阶段不得标记完成：

- 三种 Adapter 只是类名或空实现；
- 用三个 OpenAI-compatible 模型冒充三种协议；
- 用 Mock/Echo 或字符串相等冒充真实跨模型评测；
- Quality/Safety Judge 与 A/B 任一模型相同；
- 前端重新估算费用，或缺失费用显示为 0；
- retry/fallback 没有真实 Attempt 和独立费用；
- Skill 只是复制文件夹，权限仍由 `_SKILL_TOOLS` 决定；
- HTTP 工具缺少绑定 DNS/Peer IP 却宣称已防 SSRF；
- WRITE 能永久授权，或审批没有绑定参数和 Skill Digest；
- Canary 只有按钮，没有真实 Bundle 分流、Exposure、指标和回滚；
- 多 Agent 的不同角色仍被同一硬编码模型绕过 Router；
- 四个模块没有可操作 UI；
- 测试、密钥、本地数据或评测结果被提交到仓库。

## 24. 简历主张与证据账本

完成后最多使用以下六个项目亮点，每一点都必须能从代码、测试和脱敏运行证据回答面试追问：

1. **目标执行闭环**：将对话、Ask、计划、每日行动、复盘、记忆和计划版本调整连接为可恢复个人成长闭环；
2. **持久化 Agent Runtime**：基于 SQLite WAL、显式状态机、lease/heartbeat、Checkpoint、append-only 事件和 SSE 实现可暂停、取消、恢复和防重复副作用；
3. **受控多 Agent 协同**：父 Agent 通过冻结上下文、任务树、Artifact 和 Commit Gate 协调研究、计划、执行与复盘专家；
4. **多供应商模型控制平面**：实现 OpenAI-compatible、Anthropic、Gemini 三协议 Adapter，按角色与能力进行确定性版本路由，并记录 Invocation/Attempt；
5. **真实评测与可信进化**：用冻结黄金集、匿名配对 Judge、安全硬门禁、人工审批和 Canary 完成可回滚的策略进化；
6. **版本化安全 Skill 平台**：实现声明式 ZIP、内容寻址版本、五层权限、会话固定、可信 HTTPS Connector 与逐次 WRITE 审批。

在仅完成两种协议真实验收时，简历必须表述为：“实现三类协议适配器，并使用两种协议、三个模型完成真实调用验证。”只有三类协议都真实成功，才可表述“三供应商均通过真实验证”。

## 25. 最终完成定义

本规格完成必须同时满足：

1. Migration 9/10/11 在全新库、由 migration 1..8 升级的旧库和重复启动中一致通过；
2. 三种 Adapter 契约测试通过，至少两种真实协议、三个模型配置烟测成功；
3. 对话、Ask、计划、执行、研究、专家、复盘和 Judge 的所有模型调用均进入统一账本；
4. Invocation、Attempt、Event、CostLedger 与 Stats 的权威边界没有双写漂移；
5. 冻结 60 Case 完成真实配对评测，报告满足分区隔离、盲评、Safety 和统计门禁；
6. 路由 policy Candidate 可经现有评测、审批、Canary 晋升或自动回滚；
7. Skill 从安装、权限预览、授权、启用、会话绑定、更新到卸载全部可用；
8. HTTP Connector 通过 DNS/IP 绑定和 Peer IP 安全测试，否则保持禁用且不算完成；
9. `/models`、`/usage`、评测详情、`/skills` 及轨迹/Growth 集成可操作且响应式；
10. 后端全量测试、前端全量测试、TypeScript 检查、Vite 构建和真实 smoke 全部给出如实证据；
11. 导出与日志通过脱敏测试，仓库不包含 API Key、本地 `data/`、Memory 或真实评测结果；
12. 最终交付逐项列出已实现内容、命令、测试结果、外部凭据未运行项和仍未完成项，任何占位实现均不得表述为完成。

达到以上全部条件后，本阶段即冻结完成，不再追加新平台能力；后续需求必须作为新的独立版本提出。
