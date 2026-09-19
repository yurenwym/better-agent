# R1 · M2 验收记录（M2-01 / M2-02 / M2-03）

日期：2026-09-19
范围：R1 的「先构建完整请求再判断缩减」、「归档预算解耦与 Episode 优先级修复」、「所有调用的最后一道校验」。
不含：M3-01（质量回放与灰度）、R2、R3、R4。

目标形态（用户口径）：**压缩后返回给 LLM 的是「摘要 + 最近几轮对话」**，一轮 = 用户一条 + LLM 一条。

---

## M2-01 先构建完整请求再判断缩减 + `min_turns`

### 交付

| 文件 | 改动 |
| --- | --- |
| `app/token_budget.py` | 新增 `history_min_turns()`、`compact_ratio()`、`compact_target()`、`HotWindow`、`hot_window()`；`DEFAULT_COMPACT_RATIO=0.7`、`DEFAULT_HISTORY_MIN_TURNS=5` |
| `app/model_gateway.py` | `ModelProfile` 增加 `history_min_turns`、`compact_ratio`，并进入 `public_view()` |
| `app/transcript.py` | 新增 `measure()`；`pack_recent()` 增加 `min_turns` 下限与 `on_degraded` 回调 |
| `app/conversation.py` | 新增 `_hot_window()` / `_window_for_turn()`；**删除两处硬编码 `12_000`**；归档停止条件改为 `compact_target` |

### 关键数值（`docs/context-budget/r1-m2-acceptance.md` 实测）

| 场景 | `input_limit` (H) | `min_turns` | `compact_target` |
| --- | --- | --- | --- |
| legacy Profile（32768/8192） | 23348 | 5 | 16344 |
| tier A（admitted 32768, verified 32768, O 8192） | 24576 | 5 | 17204 |
| Profile 策略 `min_turns=8, compact_ratio=0.5` | 23348 | 8 | 11674 |

### 解决的两个问题

1. **归档与热窗口不再共用同一个魔数。** 原先 `conversation.py` 里两处 `min(12_000, archiver.keep_tokens)` 与 `pack_recent(token_budget=12_000)` 各写各的，模型窗口换了它们不动。现在 `H`、`min_turns`、`compact_ratio` 全部来自同一个已版本化的 Profile。
2. **贴近 `H` 时不再每轮重复归档。** 原停止条件只看「装不下」，导致停在阈值附近时每轮都要重新归档一次。现在停止条件是 `compact_target = ceil(compact_ratio × H)`，留出确定性余量。

### `min_turns` 语义（不可静默降级）

最新的 N 个**完整 Turn** 无条件保留。只有当**单个 Turn 自身**就超过整个预算时才降级，且必须记录：

```
context.window_degraded  →  "a single complete turn exceeds budget=...; kept K of N complete turns (min_turns=M)"
```

降级是显式事件，不是静默裁剪。

---

## M2-02 归档预算解耦与 Episode 优先级修复

### 交付

| 文件 | 改动 |
| --- | --- |
| `app/conversation.py` | memory-context 优先级 **20 → 60** |
| `app/memory_archive.py` | `MAX_SUMMARY_OUTPUT_TOKENS=1600`、`DECISION_OUTCOME_RESERVE_RATIO=0.3`；重写 `_merge_chunk_summaries()` |

### 修复的 P0：摘要在最需要它的时候被优先丢弃

原优先级阶梯里 memory-context 是 `_context_priority=20`，而**裸历史默认 50**。`live_model.py` 按优先级升序丢弃，于是**摘要在它所替代的原文之前被丢掉**——压缩的意义在最需要时正好归零。

修复后阶梯（升序丢弃，数值越大越先保留）：

| 优先级 | 内容 |
| --- | --- |
| 70 | skill |
| **60** | **memory-context（归档 Episode 摘要）** |
| 55 | goal（required） |
| 50 | plan / 裸历史 |

### 修复：`_merge_chunk_summaries` 的字段饥饿

原实现按字段顺序串行抽取（`synopsis → decisions → outcomes → open_loops → topics`）。一个长 `synopsis` 会把预算吃光，`decisions` / `outcomes` 全部为 0。

新实现：`synopsis` 先抽但**封顶**在 `1600 × (1 − 0.3) = 1120`；随后 **decisions / outcomes 交错抽取**（按 chunk 与字段双维度交错），任何一个长 chunk 或长 decisions 列表都无法饿死 outcomes。

对抗性验证（`synopsis` 200 字符 × 10、`decisions`/`outcomes` 各 200 字符）：

| 实现 | synopsis | decisions | outcomes | 大小 |
| --- | --- | --- | --- | --- |
| 旧 | 6 | **0** | **0** | — |
| 新 | 4 | 1 | 1 | 1564 ≤ 1600 |

### 有意偏离方案的地方（记录在案）

方案 v2 的 M2-02 第三条要求同时改 `keep_tokens` 与 `proactive=True` 的 `keep_tokens × 0.8`。**未改**，原因：`ConversationArchiver` 是跨会话共享对象，且 `_select_archive_prefix` 同时被后台 `proactive` Worker 使用，不在对话热路径范围内。改动会影响非本任务路径，收益不明确。

---

## M2-03 所有调用的最后一道校验

方案要求的 6 项逐条核对：

| # | 要求 | 结论 |
| --- | --- | --- |
| 1 | 各入口都检查**实际 Payload** | 已实现（见下「发现的缺口 A」） |
| 2 | 摘要调用只用自身预算，不递归触发会话归档 | ✅ 已核实 |
| 3 | 计数后若增加消息/工具/输出上限必须重算 | ✅ 已核实无此路径 |
| 4 | fallback 按实际 Profile 重算；不能则**明确跳过** | 已实现（见「缺口 C」） |
| 5 | 不重放已执行工具，不把内部打包提示发给供应商 | 已实现（见「缺口 A」） |
| 6 | 容量拒绝最多一次有实质缩减的重试；无变化不重发 | 已实现（见「缺口 B」） |

### 交付

| 文件 | 改动 |
| --- | --- |
| `app/token_budget.py` | 新增 `assert_provider_payload_fits()`：测量**序列化后的真实 payload** |
| `app/model_gateway.py` | 新增 `_provider_messages()`；三个适配器（OpenAI / Anthropic / Gemini）全部改为发送剥离后的消息，并在发出前调用 payload 门禁；容量拒绝不再盲重试 |
| `app/model_control.py` | 预检 fallback 跳过而非中止整个调用；路由层容量拒绝同样记录证据失效；`begin_invocation` 把解析后的 `invocation_id` 钉进 context |
| `app/db.py` | 迁移 37：`model_invocation_events` 表 + 索引；`POSTGRES_SCHEMA_HEAD` → `20260918_0015` |
| `alembic/versions/20260918_0015_model_invocation_events.py` | Postgres 侧同构迁移 |

### 审计发现的三个真实缺口（均已修复）

**缺口 A — 内部打包提示会发给供应商。**

`strip_packing_hints()` 只用于**计数**（局部变量），而 `payload["messages"] = request.messages` 发的是原对象。实测确认 `_context_required` / `_context_priority` / `_context_group` 会被原样序列化进 provider body。修复：三个适配器统一走 `_provider_messages()`，计数与发送看到同一份消息。

**缺口 B — 容量拒绝被原样重发。**

两个网关都把一个 `context_overflow` 当作可重试错误，**重发完全相同的 body**。而这两层都没有缩减能力（压缩归调用方），所以重发只消耗重试预算并掩盖真实信号。修复：记录 `model.context.capacity_evidence_invalidated` 后失败退出。

**缺口 C — 一个装不下的 fallback 会拖死整个调用。**

原实现里任一 fallback 预检失败即 `finish_invocation("failed")` 并抛出。修复：非末尾 profile 预检失败时记录 `model.context.fallback_skipped` 并 `continue`，让后面能装下的 profile 有机会服务；只有最后一个 profile 才终止调用。

**缺口 D（顺带发现）— 事件会被静默丢弃。**

`record_event` 只在存在 `run_id + goal_id` 或 `thread_id + turn_id` 时落库。上面这些新事件在无 thread/goal 的调用里**什么都不会留下**。修复：迁移 37 新增 `model_invocation_events`，并在 `begin_invocation` 把解析后的 `invocation_id` 钉进 context，使每次调用的事件都能落到自己的调用上。

### 一个仍然存在的口径差异（明确记录，不掩盖）

`assert_request_fits` 估计的是规范化 `{"messages","tools"}` 形状；三个适配器各自**重新序列化**：Anthropic 把 system 提升为顶层 `system` 字符串并把工具改写为 `{"name","description","input_schema"}`，Gemini 构造 `contents` + `functionDeclarations`。因此**估计值不等于线上 body**。

`assert_provider_payload_fits()` 消除了这个差异：它测量**已完成 payload 的真实字节**，且严格比估计更紧（真实字节 vs 字节 + 声明的 wrapper 开销），所以只可能拒绝估计误放行的请求，不可能放行估计已拒绝的请求。

---

## 测试与回归

### 新增测试

| 文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `tests/test_context_hot_window.py` | 11 | M2-01：`hot_window` 数值、Profile 策略、`min_turns` 下限、降级仅在单 Turn 溢出时发生、摘要优先于裸历史 |
| `tests/test_context_wire_gate.py` | 9 | M2-03：payload 门禁、三适配器不泄漏打包提示、容量拒绝不重发、证据失效落库、装不下的 fallback 被跳过 |
| `tests/test_context_budget_contract.py` | 22 | M1-02 / M1-03（沿用） |

### 回归结果

| 组 | 结果 |
| --- | --- |
| `test_context_budget_contract` + `test_context_hot_window` + `test_memory_archive` + `test_memory_archive_api` + `test_transcript` + `test_conversation_worker` | **110 passed** |
| `test_model_control` + `test_model_gateway` + `test_routed_model_gateway` + `test_live_model` + M1/M2 两组 | **144 passed, 2 failed** |
| `test_context_wire_gate` | **9 passed** |

### 2 个失败的归属（已证实非本次改动引入）

`test_evaluation_attempt_pins_price_snapshot_while_regular_call_uses_current_price` 与
`test_routed_budget_block_is_explainable_and_happens_before_transport`
在**把本次改动的 4 个应用文件回退到 HEAD 后仍以同样方式失败**（`'NoneType' object has no attribute '__dict__'`，`_execute_attempt` 桩返回 `None`）。属于既有失败，不计入本次回归。

---

## 未完成项

1. **M3-01 质量回放尚未执行。** 机制指标（保留量 / 归档次数 / 延迟 / 费用）改善**不能**证明「比现在好」。按方案要求必须比较「未压缩参考 / R1 前后 / 不同 `min_turns`」的约束遵守、最新纠正值、已确认参数、工具结果、失败尝试与未解问题；机制改善而质量下降必须判为不通过。这是 R1 的硬性验收，目前**未做**。
2. `keep_tokens` 与 `proactive` 的 `0.8` 系数按上述理由保留原值。
3. 真实 Profile 启用 tier A 仍需 A 级证据（`admitted_context_limit` + `counter_evidence_version` + 已声明 `protocol_budget`），仓库默认值不构成证据。
