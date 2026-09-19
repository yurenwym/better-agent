# M2 对话与上下文最终验收

日期：2026-09-09  
结论：`PASSED`  
范围：仅验收 M2；未执行 Canary、候选发布或 M3。

## 阶段出口

最终生产代码真实批次为 `M2-LIVE-20260909-008`。三轮结果为 `PASSED / PASSED / PASSED`，总计 20 次模型请求，分轮为 `6 / 7 / 7`，耗时 113.21 秒。三轮均验证：

- ask 持久化与回答续接；
- 续接继承父 Turn 的不可变 bundle 与根预算；
- stable 切换不改变在途续接，新 Turn 使用新 bundle 和独立根预算；
- 仅更新对话目标不会静默修改计划文档；
- 明确计划修订会生成新版本并保留 base version。

报告：`docs/acceptance/m2-live-results-2026-09-09-008.json`。报告 source manifest 中所有 `app/*.py` 和 Alembic 文件的摘要与当前生产文件一致。008 后的改动只涉及验收运行器的当前库清理，不改变生产应用逻辑。

## 改动与关键决定

- ask continuation 使用 `parent_turn_id` 和 `turn_asks.continuation_turn_id` 读取父 Turn 原始请求；只有真实 ask continuation 才继承计划文档交付意图。
- ask continuation 固定使用父 Turn 的 `runtime_bundle_id` 与 `root_budget_id`；普通新 Turn 使用提交时 stable bundle 和新根预算。
- 明确动作走确定路径，避免无必要的研究/计划意图分类调用。
- 已确认 ask 续接返回有效 artifact 时不再被回答文本二次分类降级。
- 已确认文档意图可规范化带明确计划类一级标题的 Markdown；泛化标题（如 `# Answer`）保持硬失败。
- 根任务预算覆盖辅助分类、主回答和修复调用；并发预留、失败回滚、重启复用和过期/越权根预算均有 PostgreSQL 回归。

## 冻结案例、正常与故障证据

- 最终 M2 后端集合：`345 passed, 3 skipped`。
- 定向回归：`137 passed`；清理运行器最终定向测试：`3 passed`，并通过 `py_compile`。
- 前端：`203 passed`；生产构建成功。
- 当前 PostgreSQL 回滚探针：`M2_CURRENT_POSTGRES_ROLLBACK_PROBE=PASS`。
- PostgreSQL migration head：`20260909_0003`。
- `git diff --check` 通过；仅存在工作树 LF/CRLF 转换警告。
- 离线故障覆盖包括必需上下文超限前置失败、认证失败不重试、429 有界重试、取消、结构错误、显式 fallback、并发根预算和 PostgreSQL 重启重放。

历史 001–007 报告和预算文档保留原状态，不冒充最终代码证据。007 虽三轮成功，但随后发现 `# Answer` 修复范围过宽，因此最终出口只采用 008。

## 预算

- 008 实际账本费用：`504,640 microusd`（US$0.504640）。
- 008 硬上限：30 次、20 分钟、`756,960 microusd`（US$0.756960）、无 fallback、首错停止。
- 008 实际调用：20 次；未触及调用、时长或费用硬上限。

## 当前数据库清理与回滚

验收严格使用唯一 PostgreSQL 数据库 `better_agent`。运行后的审计确认旧运行器所谓临时 schema 未实际承载数据，测试行写入了 `public`。已修正运行器，按精确 batch owner 与 bundle manifest 清理，并在事务中恢复测试前 stable bundle。

- 001–008 累计删除 4,752 行测试数据；
- 33 张含 `owner_id` 的表全部核验为无 `m2-live-*` 行；
- 对 `public` 所有 text/varchar/json/jsonb 字段扫描，`m2-live-` 命中为 0；
- M2 runtime bundle、invocation、attempt、ledger、budget、profile、policy 和根预算均为 0；
- 普通线程仍为 7 个；
- stable 已恢复为 `bundle_8040b4cc18e6bf2282d15992`；
- `thread_events` 与 `cost_ledger` 的 append-only 触发器均处于启用状态。

清理函数在单一事务内只临时禁用上述两张测试审计表的 append-only 删除触发器；任一删除或校验失败会整体回滚。它不删除普通 owner 数据。

## 未解决风险与后续边界

- 008 JSON 中保留旧的 `temporary_schema`/`cleanup` 字段，因此存储清理事实以本交付记录及清理后数据库核验为准；行为、调用与费用证据仍以 008 JSON 为准。
- 当前工作树包含此前 M1/M2 以及未提交的其他改动，本次未回退或提交它们。
- M2 满足进入下一阶段的技术条件，但本次任务要求成功后停止，因此没有进入 M3，也没有发布任何候选。
