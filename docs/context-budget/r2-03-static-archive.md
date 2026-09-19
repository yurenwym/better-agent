# R2-03 静态提前归档

状态：**已实现，离线验收通过**。在线时延/队列指标未测（见 §7）。

## 1. 这一项解决什么问题

R1 的压缩是**前台阻塞**的：`_archive_history_before_generation` 只在
`measure(pending) > H` 时才动手，而且是在生成回答的路径上 `enqueue → claim → process`
循环等到 `pending <= compact_target`。用户为此付出等待。

R2-03 把这件事**提前到后台**：给出一条**静态触发线** `G`，未归档前缀一旦越过它，
后台 worker 就开始压缩；等前台真的需要压缩时，前缀通常已经落在目标 `T` 之下，
前台路径直接命中早返回，不阻塞。

同时它顺手堵掉一个真实的队列风暴：原来的后台触发是"每个终止 Turn 一个
`memory_archive_signals` 信号 → 每次 `enqueue` → 转写本变长 → 新的
`(start,end,source_hash)` → 新 Job → 新模型调用"。也就是说**归档调用次数随对话活跃度线性增长**。
加上静态线之后，一次 pass 只在"前缀真的越线"时发生，且一次 pass 尽量压到 `T`，
下一次离得很远。

## 2. 策略与数值

全部由**冻结的有效 Profile** 推导，`H = effective_input_budget(profile).input_limit`：

```
G = floor(0.30 * H)     触发线（低水位）
B = H - G               前台保留的余量
D = floor(0.20 * H)     预留，保证下一轮增长不会立刻再次触发
T = B - D               静态压缩目标
```

初始比例 `0.30 / 0.20` 是**回放候选**，不是对 p95 的预测。它们是版本化策略
（`ARCHIVE_POLICY_VERSION = "static-archive-v1"`），可通过 Profile 或环境变量
（`AGENT_MODEL_ARCHIVE_TRIGGER_RATIO` / `AGENT_MODEL_ARCHIVE_RESERVE_RATIO` /
`AGENT_MODEL_ARCHIVE_PREFIX_RESERVE`）覆盖，改一个比例会产生**新的不可变 Profile 版本**
（参与 `config_digest`）。

当前仓库 Profile（32768/8192 → `H = 23348`）的实际取值：

| 量 | 值 |
|---|---|
| `H` | 23348 |
| `G` | 7004 |
| `B` | 16344 |
| `D` | 4669 |
| `T` | 11675 |
| `R`（R2-02） | 5837 |
| `compact_target`（前台停止线） | 16344 |

**后台比前台压得更深**（T = 0.5H vs compact_target = 0.7H）是有意的：后台在关键路径之外，
压深一点可以让下一次 pass 离得更远，减少模型调用次数。前台的停止线保持不变，
以免静默改变 R1 已验收的行为。

## 3. 判定与选择

`static_trigger_state()` 同时给出两个判断：

- `reached = pending >= G` —— 只看触发线。
- `actionable = reached and pending > T` —— 还要求**这次 pass 真的能减少东西**。

`pending` 是**已提交**覆盖游标之后的未归档前缀（`uncovered_cost`）。在途的 Job 不会让
前缀看起来更小。

**（G, T] 区间是一段 no-op 带**：默认比例下 `G = 0.3H < T = 0.5H`，所以前缀落在
`[G, T]` 时触发线已越过、但按 `T` 压缩没有任何可压的。这是 `G` 与 `T` 两个比例之间
真实存在的张力，没有掩盖：`actionable` 就是为它设的闸门，避免每轮空转一次 enqueue。
默认比例下**实际生效的建 Job 线是 `T`**；只有当某个 Profile 声明 `G > T` 时 `G` 才成为
约束。两者都如实上报在 `StaticArchivePolicy.public_view()` 里。

选择本身走 `_select_archive_prefix(target=T, min_turns=N)`：用
`pack_recent(transcript, T, min_turns=N)` 求保留集，其余进归档批。它**不会**再读
`keep_tokens * 0.8` —— 那是与模型无关的固定字节常量。

**目标不是保证**：一次 pass 还受摘要器输入预算 `max_summary_tokens` 约束，可能停在 `T` 之上。
这不是失败：前缀变小了、线仍越着、下一次 pass 继续。已用测试钉住"受限 pass 仍然有进展"。

## 4. 幂等、预算版本与"不建新 manifest"

- **同快照幂等**：`memory_archive_jobs` 上已有
  `UNIQUE(owner_id,thread_id,start_message_seq,end_message_seq,source_hash,prompt_version)`，
  同一快照重复 `enqueue` 只会命中已有行。已加测试钉住。
- **预算版本**：Job 新增两列（迁移 41 / alembic `20260918_0019`）：
  `budget_policy_version`（静态策略版本）与 `budget_profile_version_id`（冻结 Profile 版本 id）。
  来源范围本来已由 start/end 序号 + `source_hash` 钉住；此前 Job **无法说明自己是哪个预算
  造出来的**，也就无法审计。前台硬上限路径产生的 Job 该列为 `NULL`，这是准确的：它们不是
  静态线产生的。
- **复用现有事务/覆盖游标**：没有新表、没有新 manifest。信号、Job、覆盖游标全部沿用
  `memory_archive_signals` / `memory_archive_jobs` / `conversation_archive_state`。

## 5. 后台上限与前台优先

`ManagedArchiveWorker` 三条限制：

1. **一次一个 Job**。`run_once` 只认领一个带栅栏的 Job，归档不会与自己并发，也不会
   成倍放大模型调用。
2. **速率上限** `max_jobs_per_minute`（环境变量 `AGENT_ARCHIVE_MAX_JOBS_PER_MINUTE`，不设表示不限）。
   **费用不在这里二次校验**：每次归档调用都已经过模型网关，网关在根任务预算耗尽时
   fail-closed，且被拒的调用会记在 Job 上。在这里再加一道预算检查是重复的执行路径。
3. **前台优先** `foreground_probe`。任何工作之前先问一句"有用户在等吗"
   （`foreground_turn_pending`：存在 `turn_jobs.status='QUEUED'`）。有意只问**在等**的，
   不问在跑的 —— 把"任何 turn 在飞"当成前台活动会让繁忙系统上的归档完全停摆。
   worker 不持有回答路径需要的任何锁，所以后台 pass 不可能阻塞生成；最坏情况是它在回答
   发出之后才完成。

**信号在越线与否都会被消费**。一个没越线的信号是"已花掉"而不是"待处理"：留着会让每次轮询
重新评估同一快照，而下一个完成的 Turn 会再发一个新信号。已加测试钉住"越线以下的信号不会
被无限重试"。

## 6. `P` 与 `R` 的第三项（诚实说明）

R2-02 的公式是 `R = min(ceiling, floor(ratio*H), max(0, T - P))`，`P` 是硬保护、必需摘要和
包装预算。R2-03 负责提供 `T`，并且已经接上：

```python
def recent_window_for(self, static_prefix: int = 0) -> int: ...
```

`_history` 传入 `P = envelope_for(message_count) + archive_prefix_reserve`：

- **包装预算部分是实测的**，精确，不是声明值。
- **必需摘要与硬保护部分目前是声明值，默认为 0**。原因：这些前导块在 `_history` **之后**
  才组装（`skill_context`、`memory_bundle`、`plan_context`、`goal_context` 都在
  `_history` 返回之后才 prepend），打包时无法知道它们的字节数。默认为 0 是**安全方向**：
  少留会把 `R` 放大（少截断），多留会提前截断热窗口 —— 后者正是 R2-02 明确禁止的失败。
- 补全它需要在打包前先组装前导块（`_process` 的重排序），本轮**没有做**。接口
  （`static_prefix`）已经就位并有测试。

**第三项在当前 Profile 下是不生效的**：`min(16384, 5837, 11675 - P)`，只有当 `P > 5838`
时静态项才开始收紧，而实测包装开销约 143。这一点已用测试钉住（
`test_the_third_term_is_inert_at_the_repository_profile`），以免它被悄悄改掉。所以接线
`T` 的意义是**把公式补完整**，让 ratio 项较大的 Profile 能受 `T` 约束，而不是改变今天的 `R`。

## 7. 验收

| 计划验收项 | 状态 |
|---|---|
| 后台摘要延迟 60 秒时可容纳请求照常发送 | **离线钉住**：选择器只读已提交覆盖游标；持租约的 RUNNING Job 既不改变渲染结果、也不改变 `pending` 与静态线（`test_a_running_background_job_changes_neither_the_render_nor_the_line`）。装得下时 `_archive_history_before_generation` 早返回。**未做真实 60 秒端到端计时。** |
| 无队列风暴 | 静态线 + `actionable` 闸门；越线以下的信号被消费而非重试；同快照幂等；速率上限。已测。 |
| 无跨会话预算串用 | 每个 Job 带 `owner_id`/`thread_id`/`root_budget_id`；策略按该 Turn 实际路由到的 Profile 解析。已测（scope 校验沿用既有测试）。 |
| 无覆盖缺口 | 覆盖游标只在 `_commit` 事务内前进，`pending` 从已提交游标量起。已测。 |

未做：在线 P50/P95 时延、排队等待率、归档调用次数/费用统计、灰度与回滚。这些需要真实
负载与在线观测，不是离线回放能替代的。

已知限制（沿用既有行为，本轮未改）：一个**永久失败**的信号（例如
`ArchiveBindingMissing`：源 Turn 缺失或越出 owner 作用域）会让 `enqueue` 一直抛错，
信号被保留、每次轮询重试一次。它**不产生模型调用**，因此不是费用风暴，但会持续打数据库
（默认 `poll_interval=0.25` → 每秒 4 次）。要修需要给信号加一个终态（dead-letter 或计数上限），
属于独立的可靠性改动。

## 8. 回归

- 新增 `backend/tests/test_static_archive_policy.py`：**46 passed**。
- 上下文/打包/近期窗口套件：`test_context.py`、`test_transcript.py`、
  `test_context_packing_limit.py`、`test_recent_window_policy.py` —— **55 passed**。
- 全量（`tests --ignore=tests/integration`，`-p no:randomly`）：**22 failed, 1181 passed**
  （10 分 04 秒）。失败集合与 R2-02 结束时**逐项一致**，全部是既有失败：

  | 文件 | 数量 | 归因 |
  |---|---|---|
  | `test_cost_control.py` | 10 | 用户未提交的 `costs.py` 价格/预算链路 |
  | `test_real_evaluation.py` | 2 | 同上 |
  | `test_m5_release_gates.py` | 2 | 同上 |
  | `test_evaluation_api.py` | 1 | 同上 |
  | `test_golden_journey.py` | 1 | 同上 |
  | `test_model_control.py` | 1 | 同上 |
  | `test_routed_model_gateway.py` | 1 | 同上 |
  | `test_startup_contract.py` | 1 | 同上（`MODEL_PRICE_MISSING` 未上报） |
  | `test_plan_security.py` | 3 | Windows 符号链接用例 |

  其中价格链路的失败与本次改动无关：`app/model_readiness.py` 只依赖
  `config.resolve_credential` 与 `costs.monetary_limits_enabled`，两者都不在本次改动内。
  原始输出留档在 `outputs/r2-03-regression.txt`。
- `tests/integration/*` 仍整体报错（Docker 不可用，`exit status 125`），环境问题。
