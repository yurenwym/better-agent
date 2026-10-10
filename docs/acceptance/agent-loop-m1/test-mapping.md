# AgentLoop 验收节点

命令均在 `backend` 目录运行；节点前缀为 `tests/`。

| 契约 | pytest 节点或测试文件 |
|---|---|
| AL-T01 | test_agent_loop.py::test_al_t01_direct_answer |
| AL-T02 | test_agent_loop.py::test_al_t02_order_and_new_invocations |
| AL-T03 | test_agent_loop.py::test_al_t03_pending_stops_remaining_calls |
| AL-T04 | test_agent_loop.py::test_al_t04_invalid_arguments_repeat_without_execution |
| AL-T05 | test_agent_loop.py::test_al_t05_guards；test_al_t05_wall_time_cancels_inflight_model |
| AL-T06 | test_agent_loop.py::test_al_t06_cancel_inflight |
| AL-T07 | test_agent_loop.py::test_al_t07_reset_and_binding_exception |
| AL-T08 | test_live_model.py；test_conversation_worker.py；test_chat_goal_tools.py；test_snapshot_production_boundary.py |
| AL-T09 | test_conversation_capabilities.py::test_al_t09_t14_research_handoff_idempotent |
| AL-T10 | test_conversation_capabilities.py::test_al_t10_incomplete_research_is_observation；test_al_t10_research_preconditions_fail_without_writes |
| AL-T11 | test_conversation_capabilities.py::test_al_t11_t14_expert_context_and_idempotence |
| AL-T12 | test_conversation_capabilities.py::test_al_t12_t13_expert_rejections |
| AL-T13 | test_conversation_capabilities.py::test_al_t13_identity_rejected_before_research；test_al_t12_t13_expert_rejections |
| AL-T14 | test_conversation_capabilities.py::test_al_t09_t14_research_handoff_idempotent；test_al_t11_t14_expert_context_and_idempotence |
| AL-T15 | test_conversation_capabilities.py::test_al_t15_remember_evidence_and_idempotence |
| AL-T16 | test_conversation_loop.py::test_al_t16_native_ask_then_answer |
| AL-T17 | test_conversation_loop.py::test_native_conversation_main_chain；test_al_t24_t26_plan_binding_boundaries |
| AL-T18 | test_conversation_protocol_v2.py；test_conversation_worker.py |
| AL-T19 | test_goal_loop.py::test_al_t19_goal_terminal_mapping |
| AL-T20 | test_goal_loop.py::test_al_t20_goal_approval_resume_executes_once；integration/test_agent_loop_goal_postgres.py::test_goal_native_gateway_snapshot_and_trace_survive_approval |
| AL-T21 | test_goal_loop.py::test_al_t21_goal_plain_text_exhausts |
| AL-T22 | test_agent_loop_routing_eval.py；test_routing_comparison.py |
| AL-T23 | test_conversation_loop.py::test_al_t23_native_plan_publish |
| AL-T24 | test_conversation_loop.py::test_al_t24_t26_plan_binding_boundaries[empty/mixed] |
| AL-T25 | test_conversation_loop.py::test_al_t24_t26_plan_binding_boundaries[history/missing_history] |
| AL-T26 | test_conversation_loop.py::test_al_t24_t26_plan_binding_boundaries[existing] |

额外边界：错误守卫状态落盘、模型错误码、来源身份缺失、专家幂等参数变化、工具待核对 checkpoint、同回合计划保存恢复、前端可消费的计划消息引用。
