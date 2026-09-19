# R2 · R2-01 验收记录：大型工具结果投影

日期：2026-09-19
测试：`backend/tests/test_tool_projection.py`（19 项，全通过）
迁移：39（`turn_asks.owner_id` + 索引）、alembic `20260918_0017`

---

## 1. 审计：问题在哪里

| 存储 | 稳定标识 | 作用域 | 是否进入对话上下文 |
| --- | --- | --- | --- |
| `turn_asks`（ask_user 工具结果） | `call_id` UNIQUE ✅ | 原为**派生**（`turns` → `threads`），无直接列 ❌ | **是**（`transcript.render_history`） |
| `tool_calls.result_json` | `id` PK ✅ | `run_id` ✅ | 否 |
| `agent_artifacts` | `id` PK + `content_hash` ✅ | `task_id` → `agent_tasks` ✅ | 否 |

**结论：真正会反复占窗的是 `turn_asks.answer_json`。**

`app/ask.py::normalize_answers` 对答案做了结构校验，但 `free_text` **没有任何长度上限**（问题文本 400 字符、选项 ≤4 个都有界）。`free_text` 是唯一入口，写入 `answer_json` 后被原样序列化进 transcript，并在**此后每一轮**都进入上下文。

即：一条超长自由文本回答会长期占用热窗口，并且挤掉它周围真正有用的轮次。

---

## 2. 修复：投影在上下文，不改存储

### 硬性不变量

1. **原文先持久化，且永不改写。** `turn_asks.answer_json` 逐字节保留。
   `test_original_result_is_never_truncated_in_storage` 断言投影后存储内容与写入时**完全相等**。
2. **上下文只携带机械投影。** 由 `app/tool_projection.py` 生成，**不经过任何 LLM**。
   用摘要替换权威结果是明令禁止的：一个与真实结果相矛盾的摘要比没有结果更糟。

### 投影形状

超长字符串被替换为：

```json
{
  "_truncated": true,
  "_total_bytes": 15234,
  "_head": "……原样前缀……",
  "_tail": "……原样后缀……",
  "_reference": {"tool": "ask_user", "call_id": "call-9", "turn_id": "turn-1", "path": "$.answers[0].free_text"}
}
```

- **有界的权威字段原样通过**：`question_id`、`selected_options`、`header`、`options`、状态码、错误类型。`test_bounded_authoritative_fields_survive_projection` 逐项断言。
- `_head` / `_tail` 是**逐字节切片**，不是改写。`test_excerpt_is_verbatim_not_paraphrased` 断言 `original.startswith(_head)` 且 `original.endswith(_tail)`。
- 每个替换都记录**总字节数**与**可解析的引用**，所以模型知道被截断了，也拿得到回读凭据。
- 若投影后有界字段仍然累加超限，退化为信封并记录 `_dropped_top_level_keys`——**只记录不静默丢弃**。

### 阈值来自模型预算，不是固定常量

```
每结果上限 = max(H // 8, 256)
```

由 `hot_window().tool_result_bytes` 提供，随模型缩放。`test_result_budget_scales_with_the_model` 断言小窗口模型的每结果上限**小于**大窗口模型。

### 分页回读

`read_result_page(db, call_id, *, owner_id, offset, limit, profile=None)`：

- 从 `turn_asks.answer_json` 读**原始**内容，按字节分页，UTF-8 安全切片；
- 作用域校验用**持久化的 `owner_id`**，不信任调用方传入的 thread；
- 传入 `profile` 时对**该页**重新执行 `assert_request_fits`，所以读详情不会撑爆它本要保护的窗口。

`test_read_result_page_walks_the_whole_original` 逐页拼回后与原文**完全相等**。

---

## 3. 作用域补齐（迁移 39）

`turn_asks.owner_id` 直接持久化，从既有连接回填：

```sql
UPDATE turn_asks SET owner_id = (
  SELECT t.owner_id FROM turns tt JOIN threads t ON t.id = tt.thread_id WHERE tt.id = turn_asks.turn_id
) WHERE owner_id IS NULL;
CREATE INDEX idx_turn_asks_owner ON turn_asks(owner_id, call_id);
```

理由：分页读取不应该必须信任调用方给出的 thread 参数。有了直接列，作用域校验既便宜又不可绕过。

---

## 4. 接线位置

| 文件 | 改动 |
| --- | --- |
| `app/tool_projection.py` | 新增：`project_result_content()`、`read_result_page()`、`ResultScopeError`、`result_context_bytes()` |
| `app/transcript.py` | 新增 `project_context()`：产出**上下文视图**，canonical transcript 保持原文供归档器使用 |
| `app/conversation.py` | `_history()` 在 `pack_recent` **之前**投影，使一条大结果无法挤掉它周围的轮次 |
| `app/token_budget.py` | 新增 `tool_result_budget()`、`MIN_TOOL_RESULT_BYTES`；`HotWindow` 增加 `tool_result_bytes` |
| `app/db.py` | 迁移 39；`POSTGRES_SCHEMA_HEAD` → `20260918_0017` |

**为什么投影在 `render_history` 之前而不是 `build()` 里**：归档器读的是 canonical transcript，必须看到**原文**才能忠实摘要。只有模型读到的视图被投影。`test_context_view_is_projected_while_the_canonical_original_survives` 同时断言两侧。

---

## 5. 测试覆盖（19 项）

| 类别 | 覆盖 |
| --- | --- |
| 投影形状 | 小结果不动；有界字段保留；超长值记录大小/引用；摘录逐字；落在上限内；硬上限记录被丢弃键；拒绝非正上限 |
| 存储不变量 | 原文逐字节不变 |
| 分页回读 | 逐页拼回等于原文；跨作用域被拒；按页重新预算校验（超限抛 `ContextOverflow`，合规页正常返回）；未知 `call_id`；负 offset |
| 转录接线 | 上下文视图被投影而 canonical 原文不变；`call_id` 配对保持；小结果不动 |
| 降级 | 无原文可读时仍产出诚实标记的摘录；`answer_json` 为空时报告 0 字节而非伪造内容 |
| 策略接线 | `hot_window` 携带 `tool_result_bytes`；随模型缩放 |

---

## 6. 未做 / 不声称

1. **`agent_artifacts` 未做投影。** 审计确认它已有稳定标识与作用域，且**不在对话上下文路径**上，所以 R2-01 的投影不适用于它。若将来把 artifact 注入上下文，需要单独评估。
2. **`tool_calls.result_json` 未做投影**，同样因为不在上下文路径。
3. **`free_text` 仍未加写入上限。** 这是有意的：截断写入会破坏"原文先持久化"这一不变量。防护放在**读取/投影**侧，而不是写入侧。
4. 未测量投影对真实线上费用/延迟的影响（需要在线回放）。
