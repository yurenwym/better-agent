# Trace、Policy 与归档恢复优化任务

日期：2026-10-13<br>
范围：主链路 Policy、Trace 查询、归档租约恢复<br>
原则：只修复已确认的问题，不扩大重构范围；每个任务完成后先测试，再进入下一任务。

## 当前状态

- 模型发送、模型重试、工具执行已经接入统一 Policy。
- 发送前的来源撤销检查已经位于实际发送路径之前。
- 定向测试当前为 `97 passed`。
- Trace 查询仍存在删除线程泄露、任务范围过宽和全量扫描问题。
- 归档过期恢复接口已存在，但尚未形成完整的状态更新和生产调用闭环。

## 任务总览

| 编号 | 任务 | 优先级 | 目标 |
| --- | --- | --- | --- |
| T1 | Trace 删除线程隔离 | P1 | 删除后的线程、Trace、任务均不可读取 |
| T2 | Trace 任务范围精确过滤 | P2 | 任务 Trace 不混入同线程其他任务 |
| T3 | 归档过期恢复状态完整化 | P2 | 恢复、失败、重试状态保持一致 |
| T4 | 接入归档恢复 Worker | P2 | 过期租约能被实际 Worker 周期回收 |
| T5 | Trace 查询性能优化 | P2 | 避免每次扫描 owner 全部历史 |
| T6 | 回归测试与验收 | P1 | 固化边界，作为提交前门槛 |

---

## T1：Trace 删除线程隔离

### T1.1 修复统一扫描条件

文件：`backend/app/trace_query.py`

修改 `_scan()` 的两条查询：

- `thread_events` 查询增加 `th.deleted_at IS NULL`。
- `events` 查询增加 `th.deleted_at IS NULL`。
- 保留 owner 条件，避免跨用户读取。

### T1.2 修复 expert 任务授权

修改 `_task_thread()` 的 expert 查询：

- `agent_runs` 查询关联 `threads`。
- `agent_tasks` 查询链路同样关联 `threads`。
- 同时校验 `owner_id` 和 `deleted_at IS NULL`。

### T1.3 验收

建立线程并产生事件后，将线程标记删除：

- `by_thread` 返回 `TraceNotFound`。
- `by_trace` 返回 `TraceNotFound`。
- `by_task` 返回 `TraceNotFound`。
- 其他 owner 仍然无法读取。

---

## T2：Trace 任务范围精确过滤

### T2.1 明确任务与事件关联

为每种任务确定可用的关联键：

- research：`research_job_id` 或任务对应的 trace id。
- expert：`agent_run_id`。
- 旧事件：无法关联时标记为不完整，而不是归入当前任务。

### T2.2 拆分任务查询路径

不要继续直接复用“线程全部事件”作为任务结果：

- `_task_thread()` 只负责授权和解析线程。
- 新增任务级过滤函数，根据任务类型筛选事件。
- 返回结果中增加 `task_scope`，取值为 `exact` 或 `partial`。

### T2.3 验收

同一线程创建两个任务，分别写入事件：

- 查询任务 A 只返回任务 A 的事件。
- 查询任务 B 只返回任务 B 的事件。
- 无法精确关联的旧事件不会伪装成任务事件。

---

## T3：归档过期恢复状态完整化

文件：`backend/app/memory_archive.py`

### T3.1 完善 `recover_expired()`

过期任务恢复时：

- `RETRY_WAIT`：清理租约，保留可重试状态。
- `DEAD_LETTER`：写入 `finished_at`。
- 写入 `last_error_code='lease_expired'`。
- 写入结构化错误信息。
- 递增租约 epoch，阻止旧 Worker 继续提交。

### T3.2 同步归档状态

根据恢复结果更新 `conversation_archive_state`：

- 可重试：保持处理中或等待状态。
- DEAD_LETTER：置为 `FAILED`，记录原因。
- 不得错误推进 `archived_through_seq`。

### T3.3 验收

- 过期 RUNNING 任务能进入 `RETRY_WAIT`。
- 达到最大尝试次数后进入 `DEAD_LETTER`。
- 旧 claim 的 `require_lease()` 和 `heartbeat()` 均失败。
- 状态接口能返回错误码、结束时间和当前状态。

---

## T4：接入归档恢复 Worker

### T4.1 找到实际 Worker 生命周期

确认归档 Worker 的启动、轮询和退出位置，避免只增加孤立 API。

### T4.2 接入恢复周期

在 Worker 轮询中调用 `recover_expired()`：

- 启动时先恢复一次。
- 每轮 claim 前或固定间隔再次恢复。
- 恢复失败不能终止 Worker 主循环。

### T4.3 验收

启动两个 Worker：

1. Worker A 获取任务后停止。
2. 租约过期。
3. Worker B 自动恢复并重新 claim。
4. Worker A 使用旧 epoch 提交必须失败。

---

## T5：Trace 查询性能优化

### T5.1 先增加保护上限

在索引改造前增加：

- `limit` 参数。
- 最大返回条数保护。
- 超限时返回明确错误或分页游标。

### T5.2 减少全量扫描

优先级从低成本到高成本：

1. 先按线程和时间范围缩小查询。
2. 将 `trace_id` 作为可查询字段保存。
3. 为 `owner_id、thread_id、trace_id、occurred_at` 建立索引。
4. 仅在需要兼容旧事件时执行全量回退扫描。

### T5.3 验收

- 10 万条历史事件下，单 Trace 查询不会加载全部事件到内存。
- 分页结果顺序稳定。
- 旧事件仍能显示兼容标记。

---

## T6：回归测试与验收门槛

### T6.1 必测用例

- 删除线程后 `by_thread`、`by_trace`、`by_task` 均不可读。
- expert 任务所属线程删除后不可读。
- 同线程双任务查询不串数据。
- 归档租约过期后可恢复。
- 达到最大尝试次数后进入 DEAD_LETTER。
- 旧 Worker 无法使用旧租约提交。
- 模型来源撤销后首次发送和每次重试均不发送。
- 工具撤销后不执行。

### T6.2 测试命令

```powershell
python -m pytest `
  backend/tests/test_policy_engine.py `
  backend/tests/test_policy_adapters.py `
  backend/tests/test_send_policy.py `
  backend/tests/test_trace_query.py `
  backend/tests/test_lease_interface.py `
  backend/tests/test_event_envelope.py -q
```

完成标准：所有定向测试通过；新增边界测试全部通过；不得以删除测试或放宽断言解决失败。

## 实施顺序

严格按以下顺序执行：

1. T1 Trace 删除线程隔离
2. T2 Trace 任务范围过滤
3. T3 归档恢复状态
4. T4 Worker 接入
5. T5 查询性能
6. T6 全量回归与提交前门槛

每个任务只修改必要文件，完成后记录：变更文件、测试命令、测试结果、遗留风险。
