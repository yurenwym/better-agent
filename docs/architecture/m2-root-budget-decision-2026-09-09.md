# M2 根任务共享预算：待确认架构决定

状态：仅完成现状检查和方案准备，未执行数据库迁移。按用户“运行，直到架构问题再停下”要求，在此暂停实施。

## 已确认的问题

可靠性方案Q9及M2第6项要求：同一根任务的时间、调用数和费用有共同硬上限，辅助判断、专家、修复、重试与备用模型均不能重置额度。

目前实现只有：

- `backend/app/db.py` 中cost_budgets的period_kind约束允许INVOCATION、DAILY、MONTHLY。
- `backend/app/costs.py` 的_configured_attempt_periods只选择上述三个层级；无预算配置时reserve_attempt直接返回。
- `backend/app/model_control.py` 的ModelCallContext包含run_id/turn_id/agent_task_id，但没有跨这些对象的统一预算归属。
- `backend/app/api.py` 的预算读写接口也只接受三个层级。
- 专家任务有budget_units和父子关系，执行器有自己的步数限制，验收脚本有内存总计数；它们不是跨流程的持久化费用/调用/截止时间预算。

例如，单次额度允许、每日额度也允许时，多个独立invocation不会检查“这个根任务是否已经耗尽额度”。这是缺少根任务归属与生命周期的契约，不能仅靠调低每日额度解决。

## 推荐方案

复用PostgreSQL、现有CostService与cost_ledger，不建立第二套费用账本。

1. 增加预算根记录，保存owner、根来源类型/ID、调用上限、已启动调用数、绝对截止时间及版本；金额仍记在cost_budgets/cost_ledger中，增加ROOT维度。
2. 首个用户任务创建预算根，后续询问续接、专家/研究/执行交接和关联重试继承同一个预算根。独立新任务另建根。thread本身不能充当根，因为同一会话可含多个独立任务。
3. 所有持久化任务对象与model_invocations保存预算根ID；ModelCallContext显式携带。owner或父子归属不一致、应有绑定却缺失时，网络前拒绝。
4. 每次实际HTTP attempt前，在同一PostgreSQL事务内按固定顺序锁定预算根及金额预算，校验截止时间、预占调用次数和各层费用；任一失败全部回滚。实际启动后的失败请求也占调用次数；价格/usage缺失继续使用保守预留，不计为零。
5. 重启不清零，重试与fallback不另建根。在途请求受根剩余时限约束。API/UI显示根任务已用额度、剩余额度及明确阻断原因，各账本维度是同一费用的限制视角，不能相加重复计费。

## 需要用户确认的产品语义

推荐采用**绝对截止时间，包含等待用户回答的时间**。这是最简单且可审计的硬时限；用户很晚才回复时，继续任务前需要显式延长额度/截止时间，不能自动重置。

另一种语义是等待用户时暂停计时，但这需要持久化暂停/恢复和并发活动时段的计算，不能用进程内计时器代替。原方案没有指定这两者，因此不自行决定。

同时建议：新独立用户请求建立新根；ask续接与显式retry继承旧根。自动判断“新的问题还是旧任务继续”不能静默改变预算归属；有明确工作流关联时以持久化关系为准。

## 迁移与验证

- 新增Alembic增量迁移，先扩展表和外键，保留旧记录，不篡改历史费用。
- 历史已完成任务不补造预算。历史在途任务必须在启用前明确绑定/初始化策略；推荐暂停这类任务，重新确认额度后恢复，不默认为无限额。
- 修改范围：数据库迁移、CostService、ModelCallContext、各任务创建/续接/worker入口、预算API和展示；不改变PostgreSQL唯一权威源，不增加分布式队列。
- 验收必须包含真实隔离PostgreSQL并发争抢最后一次额度、预留失败回滚、跨角色计费、失败/重试/fallback不重置、询问超时、重启恢复、跨owner拒绝和API/UI阻断原因一致。
- 先做无网络模拟调用验证；真实付费验收另外冻结批次。生产迁移前执行备份与恢复检查。

## 实施记录

已新增 Alembic revision `20260909_0002_root_task_budgets`，包括 `task_budget_roots`、ROOT费用层级及任务对象外键；`Database` 的 PostgreSQL schema head 已同步到该 revision。离线费用/网关回归 `42 passed`，`git diff --check` 通过。

后续已将 Agent、研究和评测任务保留 `root_budget_id` 关联入口，研究 worker 的模型上下文会携带该 ID，评测 enqueue 支持持久化绑定；现有无根预算调用保持兼容。

首次 PostgreSQL 对话现在自动创建 turn 根预算：默认30次模型调用、30分钟绝对时限（含等待用户）、1,000,000 microusd；可通过 `ROOT_TASK_MAX_ATTEMPTS` 和 `ROOT_TASK_MAX_COST_MICROUSD` 配置，创建后冻结。SQLite 保持旧测试语义，不创建根预算。

首次隔离 PostgreSQL 集成尝试发现测试容器仍停留在旧迁移 head，连接校验先于测试而拒绝；容器随后由 fixture 清理。尚未证明新 migration 在真实 PostgreSQL 上成功，下一步必须重启隔离数据库、执行 `alembic upgrade head` 并修复迁移兼容问题后才能继续。

## 确认范围

批准本方案意味着同意上述根任务归属、绝对时限（含用户等待）、历史在途任务暂停再授权的语义，以及所需增量schema/API改造；不批准任何真实付费批次或生产数据迁移立即执行。
