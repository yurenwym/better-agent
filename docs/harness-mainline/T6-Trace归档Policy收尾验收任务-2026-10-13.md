# T6：Trace、归档与 Policy 收尾验收任务

日期：2026-10-13<br>
目标：完成 T1–T5 的最后验证，确认 T6 是否可以关闭。<br>
范围：Trace 查询、归档租约恢复、Policy 主链路、SQLite/PostgreSQL 验收。

## 当前结论

两个独立审查结论一致：

- T1 Trace 删除隔离主体合理。
- T3 归档租约恢复主体合理。
- T4 归档 Worker 接入主体合理。
- T5 Trace 索引和分页主体合理。
- 模型发送、模型重试、工具执行已接入 Policy。
- 当前定向测试已通过 `64 passed`。

上述为最初审查状态。2026-10-14 已完成隔离 PostgreSQL 迁移、现有 integration 和非 integration 全量执行；新增 Trace 专项测试及最终验收记录见文末。

## 必须完成的任务

### T6.1：补充 Trace 边界测试

文件：

- `backend/tests/test_trace_query.py`
- 必要时新增 PostgreSQL 集成测试

#### T6.1.1：无来源回合的 run event

确认 `events` 中 `source_turn_id IS NULL` 的记录是否属于合法生产数据。

- 如果合法：修改 Trace 查询，使其可以通过 run 自身的 owner/thread 关系读取。
- 如果不合法：增加数据库约束或写入路径约束，并增加测试证明该数据不会产生。

#### T6.1.2：旧事件分页

覆盖：

- 旧 envelope 事件第一页可读。
- 第二页仍可读。
- 事件顺序稳定。
- 超过 fallback 上限时返回明确的有界结果，不得静默声称完整。

#### T6.1.3：任务隔离

覆盖：

- 同一线程的两个任务不能互相串事件。
- 任务事件位于大量无关事件之后时仍可查询。
- 删除线程后 `by_thread`、`by_trace`、`by_task` 均不可读。

### T6.2：PostgreSQL Schema 验证

在隔离 PostgreSQL 数据库执行：

```powershell
alembic upgrade head
```

确认：

- Schema head 为 `20261013_0029`。
- `events.trace_id` 存在。
- `thread_events.trace_id` 存在。
- 两个 Trace 索引存在。
- 迁移可以在干净数据库和已有数据库上完成。

### T6.3：PostgreSQL 行为验证

在 PostgreSQL 上验证：

1. 写入带 envelope 的事件。
2. 确认 `trace_id` 自动写入列。
3. 通过 `by_trace` 查询到该事件。
4. 验证 Trace 分页。
5. 验证删除线程隔离。
6. 验证同线程多任务隔离。
7. 验证归档租约过期、恢复和旧 epoch 拒绝提交。

### T6.4：全量回归

执行：

```powershell
python -m pytest backend/tests -q
python -m pytest backend/tests/integration -q
```

要求：

- 全部测试通过。
- 不删除测试。
- 不放宽断言。
- 不因环境问题跳过 PostgreSQL 验证；如果环境不可用，必须记录为未完成门槛。

## 现有实现验收点

### Policy

- 模型首次发送前执行 Policy。
- 每次模型重试前重新执行 Policy。
- 来源撤销在实际发送前重新检查。
- 工具执行使用统一 Policy 请求。

### 归档

- 过期租约进入 `RETRY_WAIT` 或 `DEAD_LETTER`。
- 旧租约 epoch 不能继续提交。
- DEAD_LETTER 写入 `finished_at`、错误码和错误详情。
- Worker 启动和轮询阶段都会调用恢复逻辑。

### Trace

- 删除线程不可通过 Trace 接口读取。
- `by_task` 只返回当前任务事件。
- 新事件使用索引列查询 Trace。
- 旧事件通过有界 fallback 兼容读取。
- 查询支持 `limit`、`cursor`、`next_cursor`。

## 可延期的性能任务

以下项目不阻塞当前功能验收，但需要登记为后续任务：

### P1：任务查询索引

当前 `by_task` 会先读取同线程事件，再按任务 ID 过滤。长线程下会增加内存和查询成本。

后续方案：

- 增加 `task_kind`、`task_id` 字段。
- 在事件写入时保存任务关联。
- 建立 `(task_kind, task_id, occurred_at, seq)` 索引。

### P2：Legacy Trace 全量迁移

当前旧事件 fallback 有上限。后续可以：

- 扫描旧 envelope。
- 回填 `trace_id` 列。
- 完成后减少或删除 legacy fallback。

### P3：Trace 复合索引

数据量增长后评估 PostgreSQL 索引：

```text
(trace_id, occurred_at, seq)
```

## 完成判定

只有同时满足以下条件，才可将 T6 标记为完成：

1. Trace 边界测试补齐并通过。
2. PostgreSQL 已升级到 `20261013_0029`。
3. PostgreSQL Trace、归档和 Policy 集成测试通过。
4. `backend/tests` 全量通过。
5. `backend/tests/integration` 全量通过。
6. 已记录 legacy fallback 和长线程查询的已知性能边界。

在 T6 完成前，不继续扩大 Policy、Runtime 或 Worker 的结构重构。

## 2026-10-14 收尾记录

### 验证范围

新增 `backend/tests/integration/test_trace_query_postgres.py`，使用项目隔离 PostgreSQL fixture 自动创建 pgvector 容器、执行 Alembic、清空测试数据并在结束后清理容器。

- 直接检查 schema head 为 `20261013_0029`，两张事件表具有 `trace_id` 列和索引。
- 从真实事件写入路径产生 envelope，再使用 `by_trace` 回读。
- 在无关旧事件之后放置目标旧 Trace，验证三页读取顺序及下一页游标。
- 同线程创建两个 expert run，验证 run 查询和 task 查询不会串入另一实例。
- 验证 task 查询返回 `partial`，外部 owner 和已删除线程不可读取。

此处迁移对象是隔离测试数据库；不代表开发或生产数据库已经升级。

### 已有执行结果

| 门槛 | 结果 | 说明 |
| --- | --- | --- |
| 非 integration 全量 | 2346 passed、3 skipped | 前轮执行，耗时 24 分 10 秒；本轮只新增测试和文档，未修改生产代码 |
| 原 integration 全量 | 188 passed、4 skipped | Docker 恢复后的前轮结果，耗时 2 分 41 秒 |
| 新增 PostgreSQL Trace 专项 | 3 passed | 本轮实际执行，包含迁移验证及以上边界 |
| 包含新增专项的 integration 全量 | 191 passed、4 skipped，0 failures、0 errors | 2026-10-14 完成；JUnit 共 195 项，耗时 147.703 秒 |
| SQLite Trace 定向复核 | 18 passed | 收尾复核，耗时 16.08 秒 |

最终 integration 结果已从 `outputs/t6-pg-20261014.xml` 核对。4 个跳过项分别为 expert HTTP live journey、Learning 真实模型验收、真实模型配对诊断、模拟 live journeys，均因未开启对应显式运行开关而跳过；不能计为通过。非 integration 的 3 个跳过项为 Windows 环境无法创建 symlink 的安全测试。

本次两个 Trace 问题的修复、隔离 PostgreSQL 迁移和自动化回归门槛已完成；不等同于全部 live 场景验收。`git diff --check` 通过。代码尚未提交或推送。

### 读取范围与已知边界

- expert 子任务查询只返回当前 Trace 事件流中明确携带该 `task_id` 或 `agent_task_id` 的事件。它没有聚合独立的 `agent_events` 表，因此统一标记为 `partial`，空结果也不能解释为任务未执行。
- expert run 查询筛选携带该 run 标识的事件；`exact` 表示当前可关联事件集合的筛选范围，不表示已收集所有后台业务日志。
- legacy 查询使用 envelope 文本匹配缩小 SQL 候选集，随后精确解析 trace id；不使用 Trace 索引加速旧行。每来源候选上限仍为 5000，达到扫描上限时返回 `legacy_scan_truncated`。这是候选扫描可能不完整的标记，不能用来证明全部历史事件已读完。
- `by_task` 为先筛选再分页而读取同线程事件，长线程查询成本仍高；任务字段与索引优化保留为后续性能任务。
- 本轮验收不包含付费真人对话、Learning live 或 expert live journey，相关跳过项须在最终结果中明确列出。
