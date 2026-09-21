# 普通聊天接入目标业务工具（D2）验收

日期：2026-09-20。范围：按开发文档 D2，把普通聊天接入现有八个目标业务工具，复用可信身份、Tool Registry、审批、预算与幂等；不改 Runtime 架构，不新增 Agent 框架。

结论：**链路已打通并通过确定性验收**；旅游攻略、保存文档、执行管理预览/激活、今日任务与反馈均走真实业务服务与审批。真实模型端到端与页面确认卡片仍待执行（见第 5 节）。

## 1. 行为变更

1. **聊天工具循环**：`LiveConversationModel.route_and_respond` 在普通聊天请求中追加八个目标工具 schema；READ 调用在同轮内联执行，WRITE 调用暂停并等待用户决定。循环上限 8 次模型调用，超限按协议错误失败。
2. **可信身份**：每次工具调用由 `ChatToolRunner` 从 turn/thread 持久化绑定构建 `ToolExecutionContext`（owner/project/thread/root_budget/runtime_bundle），模型传入 `owner_id`、`run_id`、`idempotency_key` 等字段仍被 Registry 拒绝。
3. **审批复用**：WRITE 调用写入既有 `approvals` 表并绑定参数与 binding；`activate_goal_plan` 的 activate 必须携带编译快照 `goal_activation`，普通写入审批不能代替激活确认。决定与 continuation 创建在同一事务内提交，决定幂等键唯一。
4. **幂等与恢复**：新增 `turn_tool_calls` 只保存聊天侧调用身份与结果；执行认领与结果复用仍由 `tool_execution_claims`/`tool_calls` 提供。业务已提交、工具结果未落盘时，重放会复用已提交结果，不重复写入。
5. **上下文重放**：`CanonicalTurnTranscriptBuilder` 把聊天工具调用与结果渲染为 provider 的 tool 消息，供后续轮次与归档使用；被拒绝的调用回放为拒绝 observation，模型不得据此声称已完成。
6. **提示词**：新增规则——需要查询或改变系统状态时必须调用工具；先读真实 ID 与版本；未收到成功回执不得声称已保存、已开启、已激活、已取消、已延期或已提醒；保存成功后即可结束；未实际配置调度不得声称已开启每日复盘。
7. **迁移**：SQLite migration 42、Alembic `20260920_0020_chat_tool_calls`、`postgres_schema.sql` 同步新增 `turn_tool_calls`；`POSTGRES_SCHEMA_HEAD` 更新为 `20260920_0020`。
8. **API**：`GET /api/turns/{turn_id}/tool-call` 返回待决定调用；`POST /api/turns/{turn_id}/tool-call/decision`（approve/reject + expected_version + idempotency_key）创建 continuation。取消 turn 会拒绝待处理审批并把调用标记为 CANCELLED。

## 2. 验收切片

| 切片 | 覆盖测试 | 结果 |
| --- | --- | --- |
| 生成攻略只交付内容，不创建执行目标 | `test_read_tool_executes_inline_without_execution_rows`；既有 `test_worker_streams_markdown_and_finishes_answer_without_agent_rows` | 通过：goals/runs/approvals 均为 0 |
| 保存攻略实际写入并返回文档引用；拒绝不写入 | `test_write_tool_pauses_until_approved_then_commits_document`、`test_rejected_write_never_writes_and_reports_rejection` | 通过：approve 后 1 个 committed 版本且 `activated=false`；reject 后 0 文档 |
| 管理执行进度：预览 → 确认具体版本 → 激活 | `test_execution_management_previews_then_activates_after_confirmation` | 通过：3 次审批（草稿/预览/激活），激活审批携带 `goal_activation.snapshot_hash`，最终目标 ACTIVE 且有行动 |
| 查询今日任务与记录反馈使用真实 ID/版本 | `test_today_tasks_and_feedback_use_real_ids_and_versions` | 通过：反馈写入 `goal_action_feedback`，kind=partial、actual_minutes=30，行动未被标记完成 |
| 未调用成功的工具不得声称成功 | 提示词规则 + 拒绝回放 observation（`chat_tool.resumed` 携带 `USER_REJECTED`） | 规则与拒绝路径已就位；真实模型行为审阅留待第 5 节 |

补充：`test_tool_decision_replay_never_double_writes` 验证同一决定幂等键只产生一个 continuation 和一个版本；`test_cancelled_turn_rejects_pending_approval` 验证取消不产生写入；`test_crash_after_commit_before_result_persistence_recovers_same_version` 验证“业务已提交、结果未落盘”窗口重放不重复写入；`test_later_turn_replays_the_tool_exchange_to_the_model` 验证后续轮次能看到工具调用与结果。

## 3. 已执行验证

```powershell
cd D:\RAG\better\backend
python -m pytest tests/test_chat_goal_tools.py -q
# 9 passed

python -m pytest tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py `
  tests/test_plan_document_service.py tests/test_plan_documents.py tests/test_plan_document_api.py `
  tests/test_save_message_plan.py tests/test_messages.py tests/test_conversation_api.py `
  tests/test_v2_acceptance.py tests/test_approval.py tests/test_tool_execution_claims.py `
  tests/test_tools.py tests/test_runtime.py tests/test_materializer.py tests/test_live_model.py `
  tests/test_conversation_worker.py tests/test_conversation_protocol_v2.py -q
# 270 passed

python -m pytest tests/test_goal_programs.py tests/test_goal_integrity.py tests/test_goal_program_api.py `
  tests/test_goal_adjustments.py tests/test_goal_execution_v1.py tests/test_checkpoint.py `
  tests/test_restart_recovery.py tests/test_skill_platform.py tests/test_skills.py tests/test_goal_reviews.py -q
# 91 passed

python -m pytest tests/integration/test_chat_goal_tools_postgres.py -q
# 2 passed（独立 Docker pgvector，alembic upgrade head，含新表）

python -m pytest tests/integration/test_postgres_contract.py -q
# 4 passed
```

`tests/test_static_archive_policy.py::test_the_declared_line_survives_a_round_trip_through_the_profile_version` 仍失败（`model_admin.version()` 未返回 `archive_trigger_ratio`），属于既有容量改造遗留，与本期改动无关。

## 4. 已知边界

- **页面行为留给 D3**：当前通过 API 决定审批；聊天页尚未渲染待决定卡片，`AWAITING_TOOL_APPROVAL` 期间再次发送会被后端以 409 拒绝。
- **保存文档存在两条路径**：既有 `v=2 plan_document` artifact 与新增 `create_plan_draft` 工具并存，提示词与工具描述的取舍在 D3 统一。
- **待核对写入没有聊天侧恢复界面**：`TOOL_RECONCILIATION_REQUIRED` 时保留 APPROVED 状态、不重复执行，但需要后续界面/重试入口。
- **Skill 交集**：有绑定 Skill 时按 executor 阶段授权取交集；绑定缺失或工具未授权时 fail closed（不暴露工具）。
- 普通聊天不再只暴露 `ask_user`/`review_check_in`，模型请求体积增加；预算预检已把新工具 schema 计入。

## 5. 未执行 / 待验收

- **真实模型端到端（D2 明确要求）未执行**：未配置可用密钥，未发起付费调用。现有 `backend/scripts/followup_intent_eval.py` 覆盖的是意图探针与聊天适配器回放，不能代替本链路验收；需新增真实模型案例：旅游攻略交付、明确保存、拒绝保存、执行预览与激活、今日任务、反馈、每日复盘不得谎称已开启。
- **数据库前后对比证据**：本报告给出的是自动化断言（版本数、反馈行、目标状态）；真实模型运行下的数据库前后快照与成本记录待补。
- **D1 失败项**中“执行管理被降为文字建议”与“无工具承诺副作用”在本期后端链路上已具备修复条件，但最终以真实模型行为审阅为准。
