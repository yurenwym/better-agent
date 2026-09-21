# 目标业务工具审查修复验收

日期：2026-09-20。范围：在现有八工具实现上修复审查问题，并明确计划交付与执行管理分离。

## 行为变更

1. 预览生成事件改为 `goal_tool.preview_generated`，不代表用户确认。Runtime 为激活审批绑定具体预览和摘要；审批卡片展示目标、日期、时区、时长和逐项安排。通用写入审批不能代替此激活审批。激活在业务事务内再次校验安排摘要。
2. 草稿创建通过 thread + operation key 派生稳定版本 ID，与文档和写入意图同事务保存。相同操作恢复原版本，内容变化被拒绝；PostgreSQL 并发创建通过事务 advisory lock 串行化。
3. 工具执行处于待核对状态时，仅允许从已提交版本或业务回执恢复成功结果。未找到已提交结果时保留待核对状态，不自动重复执行。激活先读取成功业务回执，避免自身版本递增破坏重放。
4. 项目内运行的列表、详情、行动、计划与写操作校验资源来源会话；目标列表在分页前过滤 project。
5. 仅指定排除日期时默认允许一周七天；查询分页拒绝负 offset。
6. 旅游攻略等默认只交付文档，保存结果明确 `delivery_ready=true`、`follow_up_enabled=false`。不创建执行目标、打卡行动或复盘。仅用户明确要求管理执行进度时，才进入预览及确认激活；主动提醒、定时监督和自动复盘不随激活开启。

## 已执行验证

后端：

```powershell
cd backend
python -m pytest tests/test_goal_tool_recovery.py tests/test_goal_tools.py tests/test_plan_document_service.py tests/test_goal_programs.py tests/test_tools.py tests/test_runtime.py tests/test_approval.py tests/test_tool_execution_claims.py tests/test_live_model.py tests/test_goal_program_api.py -q
```

结果：158 passed。包括审批门禁、业务服务与 API 回归、恢复及范围隔离测试。

独立 PostgreSQL：

```powershell
python -m pytest tests/integration/test_goal_tool_recovery_postgres.py -q
```

结果：7 passed。测试夹具创建独立临时 Docker pgvector 数据库，运行迁移并在测试后清理，不访问现有业务数据库。覆盖提交后工具回执落盘前故障、并发草稿创建、快照变化、通用审批不能激活及项目范围。

补充行动读取、反馈、延期和今日任务的项目隔离断言后，执行 `python -m pytest tests/test_goal_tool_recovery.py tests/integration/test_goal_tool_recovery_postgres.py -q`，最终结果 **16 passed**（9 个本地专项、7 个 PostgreSQL 专项）。

前端：

```powershell
cd frontend
npm run build
npm test -- --run src/__tests__/GoalActivationApproval.test.tsx
```

构建通过（存在 bundle 大小提示）；审批界面测试 1 passed，确认展示实际安排和仅交付文档的选项，拒绝不会触发批准。

## 验收边界

- 以上为确定性编译器与真实数据库测试，没有调用真实 DeepSeek；原开发文档的 18 个真实模型案例仍待执行。
- 本轮没有部署或重启应用，也没有修改真实用户目标。
- 工具接入现有 Agent Runtime；普通聊天是否能自动选择并进入该运行入口，仍需独立端到端验收。
- 文档当前仍采用每会话一份计划的 create-only 规则；本期未新增计划改写工具。
- 未提交、失败投影或无法证明成功的写入继续要求核对；本轮恢复保证覆盖“业务已提交，工具结果尚未保存”的窗口，不声称任何中断均可自动恢复。
- 提示词规定默认计划交付、可选跟进；最终用户意图识别质量仍需真实模型案例检验。执行激活有独立服务端审批约束。
- 首次交付记录中的“完整实现”和历史环境限制不应替代本报告。
