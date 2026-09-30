# HarnessExecutionContext 第一阶段修复与复测

日期：2026-09-30。基于 `733866b` 之后的当前工作区实现，修复尚未提交。

## 修复内容

| 问题 | 修复 | 回归证据 |
|---|---|---|
| 首次持久化先读后写，两个不同 Context 可能都成功并覆盖 | 三张表统一使用 JSON/digest 皆 NULL 的条件更新；竞争失败后重新读取，校验摘要、绑定及内容。同值幂等，异值拒绝 | `test_first_context_binding_race`：3 张表 × 同值/异值，共 6 项真实 PostgreSQL 并发测试；屏障确保双方均先读到空记录 |
| 工具内部计划编译再次解析学习策略，可能改变原 bundle | 有 harness 时沿用已固定版本，不再重新解析学习策略；无 harness 的独立入口保留原策略及撤销检查。Memory 约束检查保留 | I06 参数化覆盖学习服务存在/不存在，实际关联 thread，stable 切换后仍使用原版本，并断言不调用策略重新解析 |
| 双份 Context 字段校验跳过 None，且遗漏 trace/span/task 等字段 | 所有已声明的重复身份字段严格相等，包括 None；`rebind` 禁止改变 harness 身份或替换 harness，仅允许非身份元数据更新 | 14 组身份字段冲突测试，覆盖适配器构造及 rebind；固定/空 bundle 均不得被替换 |

严格校验同时暴露了入口兼容问题：未挂载 evolution 服务时，原实现依赖模型网关延迟填入 stable bundle。现改为 `accept_turn` 在可信入口读取并保存 stable，随后创建的 Context 和内部调用均继承该版本。没有 stable 配置的旧入口仍保留空值。

涉及生产代码：`app/execution_context.py`、`app/harness_context_store.py`、`app/goal_programs.py`、`app/conversation.py`。接入说明同步修正了 rebind 的语义。

## 最终测试结果

| 测试范围 | 结果 | 日志 |
|---|---|---|
| Context、对话、模型控制与路由、工具、审批恢复、计划编译等 14 个测试文件 | **267 passed**，138.07 秒 | [回归日志](repair-regression-2026-09-30.log) |
| Context 持久化与迁移、Goal 工具、恢复、根预算等 4 个 PostgreSQL 测试文件 | **30 passed**，136.30 秒 | [PostgreSQL 日志](repair-postgres-2026-09-30.log) |

合计 **297 项通过，无失败或跳过**。其中 Context 专项为 45 项非 PostgreSQL 用例及 13 项 PostgreSQL 用例。中途回归曾暴露入口延迟绑定、旧测试构造以及编译前后重复策略解析问题；修复后以上两组命令均完整重跑成功。

PostgreSQL 使用隔离数据库 `better_context_fix_e29dc064_test`，经 `db_target_guard` 校验后执行迁移及测试；未操作开发数据库。模型调用使用测试桩，不代表真实付费模型效果评估。

复现非 PostgreSQL 回归（在 `backend` 目录执行）：

```powershell
python -m pytest tests/test_execution_context.py tests/test_harness_context_flow.py tests/test_harness_context_approval.py tests/test_model_control.py tests/test_model_gateway.py tests/test_routed_model_gateway.py tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py tests/test_conversation.py tests/test_conversation_worker.py tests/test_conversation_protocol_v2.py tests/test_goal_programs.py tests/test_goal_program_compiler.py -q --tb=short
```

配置 `TEST_DATABASE_URL` 指向隔离 PostgreSQL 测试库后：

```powershell
python -m pytest tests/integration/test_harness_context_postgres.py tests/integration/test_chat_goal_tools_postgres.py tests/integration/test_goal_tool_recovery_postgres.py tests/integration/test_root_task_budgets.py -q --tb=short
```

此次仅验收第一阶段最小链路及相关回归，未重跑全仓库测试，未扩展 PolicyEngine、TaskRuntime 或 JEV 预算，也不以本记录替代历史全量测试结果。
