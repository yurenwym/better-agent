# R1 · M1 验收记录（M1-01 / M1-02 / M1-03）

日期：2026-09-19
范围：R1 的 A 级证据与统一预算函数、A 级完整请求计数。
不含：M2（缩减与携带）、M3（回归与灰度）、R2、R3、R4。

---

## M1-01 现状审计与 A 级证据

交付物：`docs/context-budget/r1-baseline.md`

已确认的事实（决定了 M1-02/M1-03 的形态）：

1. **最终校验已经覆盖全部入口**，R1 修的是**口径**，不是"有没有校验"。
   `assert_request_fits` 是唯一出口，被 `model_gateway.py:200` 与
   `model_control.py:441/454` 调用，13 个 LLM 调用入口全部经过它。
2. 只有 `live_model.py` 的 3 处做优先级丢弃；`_context_*` 提示只存在于打包路径。
3. `A / C / O` 三个容量数字**在生产里都没有证据**：`context_window` 与
   `max_output_tokens` 都只有仓库默认值，且 `O` 存在 8192 / 4096 两个默认值。
4. 三个预算互不联动：归档 12,000 / 热窗口 12,000 / 请求 23,348（字节）。
5. 计数器是 **UTF-8 字节上界**，不是 token；中文场景约 3 倍高估。

---

## M1-02 Profile 与统一预算函数

### 交付

| 文件 | 改动 |
| --- | --- |
| `app/token_budget.py` | 新增 `EffectiveInputBudget`、`effective_input_budget()`（唯一预算入口）、`ProtocolBudget`、`RequestCount`、`count_request_units()`、`strip_packing_hints()`；`request_budget()` 退化为兼容包装 |
| `app/model_gateway.py` | `ModelProfile` 增加 9 个预算契约字段 |
| `app/config.py` | `_budget_contract_from_env()`，三个 loader 全部接线 |
| `app/db.py` | 迁移 36：新增 9 列并**重建** `model_profile_versions_frozen` 触发器；`POSTGRES_SCHEMA_HEAD` → `20260918_0014` |
| `alembic/versions/20260918_0014_context_budget_contract.py` | Postgres 侧同构迁移 |
| `app/model_control.py` | 契约写入 + 回读（24 列 INSERT） |
| `app/model_admin.py` | 契约校验 + 回读 + 参与 config digest |

### 契约

```
S = min(soft_context_limit, admitted_context_limit=A, context_window=C 仅当 verified)
H = S - reserved_output - safety_margin
发送条件：U ≤ H
```

- **A 对 tier A 不可省**。没有 `min(soft, C)` 的静默降级：缺 A 直接 `ContextOverflow`。
- **未验证的 C 不参与取 min**。仓库默认值不是容量证据。
- **tier A 的 `safety_margin` 恒为 0**，包装开销只通过 `protocol_bound` 收一次，
  不再叠加固定护栏。
- **tier B 直接拒绝**，不用 `alpha=1, beta=0` 假装实现。
- **legacy（无 `validation_tier`）逐字节保持历史行为**：`C - O - min(2048, (C-O)//20)`。
  32768 / 8192 → margin 1228 → `input_limit` 23348，与 R1 前完全一致。

### 验收

`tests/test_context_budget_contract.py`（20 项全通过）：

- legacy 预算与 `request_budget` 包装逐值不变（23348 / 1228）。
- tier A 缺 A、缺 `counter_evidence_version`、未知 tier、tier B 全部拒绝。
- `min(soft, A, C_verified)` 三种胜出分支 + 未验证 C 不封顶 + 预算归零拒绝。
- `ProtocolBudget` 系数非负整数校验（bool 不算整数）、`declared` 依赖非空 evidence。
- 迁移 36：列存在、触发器覆盖新列（改 `counter_evidence_version` /
  `admitted_context_limit` / `validation_tier` / `protocol_budget_json` 均被 frozen 拒绝）。
- `ModelAdminService` 往返：缺 A / 缺证据 / 缺 protocol_budget / evidence 为空 /
  `soft < admitted` 全部报错；合法 tier A 版本可完整回读且 digest 变化。

真实 `Database` 上确认：`schema_migrations` 到 36，9 列齐备，`model_profile_versions_frozen` 已重建。

---

## M1-03 A 级完整请求计数

### 交付

`assert_request_fits` 改为 **tier 分派**，成为所有调用共用的最后一道校验：

- **tier A**：`U_A = byte_bound + protocol_bound`，其中
  `protocol_bound = b0 + bm·messages + bt·tool_definitions + bc·tool_calls + br·tool_results`，
  与 `H` 比较。
- **legacy**：保持原来的纯字节上界。
- 两条分支都先 `strip_packing_hints`，计数器只看真正发给 provider 的消息。
- **fail-closed**：tier A 没有"已声明"的 `protocol_budget` 时直接拒绝，
  不再用 4096 之类的臆造系数兜底。
- 报错信息带完整分解（byte_bound / protocol_bound / 各类计数 / counter / evidence），
  便于线上定位。

### 验收

同一测试文件内 3 项：

- 仅字节能过、加上声明的包装开销后必须被拒（`protocol_bound=8192` 出现在错误信息里）；
  同一请求在 legacy profile 上仍按字节通过。
- 带 `_context_*` 提示与不带提示计数完全相同。
- `U_A` 的分解值、计数、`evidence_version` 正确。

---

## 回归结论

在**隔离环境**（当前代码 + 还原到 `c52ae07f` 的 `costs.py`）中运行
`tests/test_cost_control.py` + `tests/test_model_control.py`：**25 passed**。
→ **M1 的改动零回归**。

真实仓库全量单测 1043 passed / 22 failed，22 个失败与本任务无关：

| 失败来源 | 数量 | 说明 |
| --- | --- | --- |
| 未提交的 `costs.py` WIP（root task budget / 货币限额，190 行在建） | 19 | 在 `c52ae07f` 基线上全部通过；隔离环境还原该文件后全部通过 |
| Windows 符号链接权限（`test_plan_security` ×3） | 3 | 基线上同样失败，环境问题 |

> 注：这 19 个失败会掩盖后续 R2 的回归信号。R2 验收一律用隔离环境复跑，
> 或在报告里显式扣除这 19 项。

---

## 尚未处理（按用户指令留到 R2 之后）

- M2-01 / M2-02 / M2-03：阈值触发、摘要携带、`min_turns`、Episode 优先级反转。
- R2-01 … R2-04。
- R3 / R4。
