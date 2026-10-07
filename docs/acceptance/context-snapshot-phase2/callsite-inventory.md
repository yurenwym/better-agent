# ContextSnapshot 第二阶段 · 真实模型调用入口清单（P00）

- 日期：2026-09-30（§7.1 于 2026-10-06 / 2026-10-07 追加）
- 代码基线：`dcfc54e358bdc8ae92e2ea1a41bf56694ea080f7`（2026-10-07 起，Phase 2A 主体已提交为
  `9992533fac42f19b9e1dbe321612d94912e6e8b3`；2026-10-07 的 F04 修复仍在工作区）
- 核对方式：只读检索 `startup.py` 的网关装配、全部 `.complete(` 调用点、全部 `ModelGateway(` 构造点、
  `ModelControlStore.begin_invocation` 调用点，以及 HTTP 路由到服务的可达性。**没有只搜一个网关类就下结论。**
- 本清单是 Phase 2A（M1）与 Phase 2B（M2）的共享入口台账。

## 0. 网关装配（谁拿到的哪个网关）

`app/startup.py`：

```python
control_store = ModelControlStore(db, events=events, costs=costs)     # :206
gateway = RoutedModelGateway(db, control_store) if registered_profile else None   # :207
model = LiveRuntimeModel(gateway, tools.describe())                   # :209
conversation_model = LiveConversationModel(gateway, settings)         # :211
...
control_store.learning_assets = runtime.learning.assets               # :261
```

结论：**生产装配只有一个网关实例**，`RoutedModelGateway`。所有从 runtime 取 `agent_runtime.gateway`
的服务都自动经过 `ModelControlStore.begin_invocation`。没有第二条生产网关。

## 1. 生产可达入口（经 RoutedModelGateway）

| # | 位置 | 调用方 / 用途 | 生产可达 | owner 来源 | 阶段 | 门禁 | 现有测试 |
|---|---|---|---|---|---|---|---|
| 1 | `live_model.py:415` | 对话 `route_and_respond`（含工具循环续接） | ✅ 对话主链路 | `ModelCallContext.from_harness(turn harness)` | **2A** | M1 / I01–I03、G01–G09 | `test_conversation*`、`test_routed_model_gateway.py` |
| 2 | `live_model.py:436` | 对话 JSON 修复（`repair_structured_output`） | ✅ | 同上（同一 turn harness） | **2A** | I04 | `test_conversation.py` |
| 3 | `live_model.py:636` | `classify_existing_plan_save` | ✅ | 同上 | 2A（同网关，自动覆盖） | I03 | `test_conversation.py` |
| 4 | `live_model.py:687` | `classify_plan_document_request` | ✅ | 同上 | 2A | I03 | `test_conversation.py` |
| 5 | `live_model.py:705/719/722` | `classify_research_request` / `classify_memory_request` + 修复 | ✅ | 同上 | 2A | I04 | `test_conversation.py` |
| 6 | `live_model.py:739` | `classify_context_dependency` | ✅ | 同上 | 2A | I03 | `test_conversation.py` |
| 7 | `live_model.py:751` | `answer_without_history` | ✅ | 同上 | 2A | I05 | `test_conversation_worker.py` |
| 8 | `live_model.py:1083` | `force_plan_document` | ✅ | 同上 | 2A | I03 | `test_conversation.py` |
| 9 | `live_model.py:1170` | `generate_clarification` | ✅ | 同上 | 2A | I04 | `test_conversation.py` |
| 10 | `goal_program_compiler.py:174`（经 `:108/122/139/153`） | 工具内部计划编译 `compile_goal_program` / `adjust_goal_program` / `daily_review` / `period_review` | ✅ `activate_goal_plan` 工具链 | `from_harness(tool child span)` | **2A** | M1 / I06、A01–A04 | `test_goal_programs.py`、`test_goal_program_compiler.py`、`test_harness_context_flow.py::test_i03` |
| 11 | `memory_archive.py:955`（`:499`） | 会话归档摘要 `summarize_episode` | ✅ 归档 worker | `ModelCallContext(role="reflector", owner_id=claim.owner_id, thread_id=...)` — **owner 来自 claim 行，不是 harness** | 2B（P12） | M2 / C03 | `test_memory_archive*.py` |
| 12 | `memory_reference.py:127` | 记忆引用解析 `resolve_memory_reference` | ✅ | 显式 owner 参数 | 2B（P12） | M2 / C03 | `test_memory_reference*.py` |
| 13 | `agents.py:804/829/847`（`:622/662/747`） | 专家/协调者调用 `expert_*`、`synthesize_experts`、`judge_expert_output` | ✅ 专家链路 | `ModelCallContext(owner_id=run["owner_id"], thread_id=run["thread_id"])` | 2B（P11） | M2 / C02 | `test_agents*.py` |
| 14 | `research/live.py:95/173/194` | 研究 `research_structured_step` / `write_research_section` / `repair_research_report` | ✅ 研究链路 | 显式 owner/thread | 2B（P11） | M2 / C02 | `test_research*.py` |
| 15 | `evolution.py:58/68/1711/1735` | 自进化 `evaluate_behavior_arm` / `judge_behavior_quality` / `propose_evolution_candidate` / `judge_canary_safety` | ✅ 开关 `BETTER_AGENT_LEARNING_V3` | 显式 owner + `runtime_bundle_id` | 2B（P12） | M2 / C03 | `test_learning_v3_*.py` |
| 16 | `learning_agent.py:427` | 学习生成 | ✅（V3） | 显式 owner | 2B（P12） | M2 / C03 | `test_learning_v3_*.py` |
| 17 | `learning_eval.py:188` | LLM Judge | ✅（V3） | 显式 owner | 2B（P12） | M2 / C03 | `test_learning_v3_*.py` |
| 18 | `learning_extraction.py:19` | `extract_learning_constraint` | ✅（V3） | 显式 owner | 2B（P12） | M2 / C03 | `test_learning_v3_*.py` |
| 19 | `learning_prompt.py:155` | 提示词候选生成 | ✅（V3） | 显式 owner | 2B（P12） | M2 / C03 | `test_learning_v3_*.py` |
| 20 | `learning_replay.py:92`（`:117/132`） | 回放 `learning_replay` / `learning_replay_select` | ⚠️ 脚本/服务入口（`BETTER_AGENT_LEARNING_V3` 回放） | 显式 owner + bundle | 2B（P12） | M2 / C03 | `test_learning_v3_*.py` |
| 21 | `real_evaluation.py:1247/1277` | 真实评估 `evaluation_*` / `paired_evaluation_judgment` | ✅ API `/evaluations*`（`:1780/1827`） | 显式 owner | 2B（P12） | M2 / C03 | `test_real_evaluation.py`、`test_evaluation_api.py` |

## 2. 直接构造网关的入口（不经 RoutedModelGateway）

| # | 位置 | 用途 | 有 control_store | 生产可达 | 阶段 | 门禁 |
|---|---|---|---|---|---|---|
| 22 | `model_control.py:887` `_execute_http_attempt` | **routed 的内部 attempt 执行**（`ModelGateway(profile)._attempt(...)`） | 是（外层已建 invocation） | ✅ | **2A**：必须**不**创建第二个 invocation/snapshot（P06） | G06 |
| 23 | `model_admin.py:355` `_verify_live` | 管理端模型版本验证 | ❌ **无 control_store** | ✅ API `POST /model-profiles/versions/{id}/verify` | **2B（P13）** | M2 / C01、C04 |
| 24 | `real_evaluation.py:1238` | 评估用网关构造 | ✅ 传入 `self.control_store` | ✅ | 2A 自动覆盖（走 #21） | G06 |
| 25 | `model_gateway.py:726` `provider_payload()` | 纯函数：算 body，不发请求 | — | ✅（被评估/管理端调用） | 2A：只读冻结输入派生（P06/G10） | G10、G11 |
| 26 | `eval.py:75` | 离线评估脚本入口 | ❌ | ❌ 离线 | 2B（P13 收口时明确标记） | C05 |
| 27 | `evals.py:73–199` | **测试桩**（`MockModelGateway`） | ❌ | ❌ | 不计入覆盖 | — |
| 28 | `scripts/followup_intent_v3.py:234`、`scripts/m5_controlled_acceptance.py:165`、`scripts/m5_live_acceptance.py:160`、`scripts/memory_reference_eval.py:26`、`scripts/r1_live_acceptance.py:208` | 离线/受控验收脚本 | 部分 | ❌ 离线 | 2B（P13 明确标记为离线） | C05 |

## 3. 与任务快照的关系（不得混用）

| 对象 | 位置 | 职责 | 本阶段动作 |
|---|---|---|---|
| `agent_context_snapshots` | `db.py`、`agents.py` | **任务级**上下文快照 | 保持原职责，只能作为**来源引用**，不得当作模型输入复用 |
| `learning_snapshots` | `learning_assets.py:76` | 每次 invocation 的**资产**引用（bundle/revision/skill） | 复用为 provenance 来源，不另造撤销状态 |
| `ContextSnapshot`（组装阶段） | `app/context.py:32` | `ContextAssembler` 的 blocks/text/hash | 保留组装能力；最终发送快照在网关边界另行冻结 |
| `ModelInputSnapshot`（本阶段新增） | 待建 `app/model_input_snapshot.py` | 一次逻辑 LLM 调用的**不可变逻辑输入** | 2A 新建 |

## 4. 覆盖归属小结

- **Phase 2A（M1）**：#1–#10（对话 + 工具内部编译）、#22/#24/#25（网关内部与协议派生）。
  因为冻结点落在 `ModelControlStore.begin_invocation` / `RoutedModelGateway.complete`，
  **其余经同一网关的入口（#11–#21）会同时获得快照绑定**，但 2A **不验收**它们的 provenance 质量，
  也**不宣称**它们已覆盖。
- **Phase 2B（M2）**：#11–#21 的逐入口动态验证与 provenance 补全，#23 的受控 direct 通道，
  #26–#28 的离线/旁路收口（C05），以及 C01/C02–C06/R02。
- **明确不属于任何阶段**：#27 测试桩。

## 5. 第一阶段专项测试基线（R01 对照）

依据 `docs/acceptance/harness-context-phase1/harness-context-phase1-repair-2026-09-30.md`：

| 范围 | 结果 |
|---|---|
| 14 个非 PostgreSQL 测试文件（Context / 对话 / 模型控制与路由 / 工具 / 审批恢复 / 计划编译） | **267 passed**，138.07s |
| 4 个 PostgreSQL 集成文件（Context 持久化与迁移 / Goal 工具 / 恢复 / 根预算） | **30 passed**，136.30s |
| Context 专项小计 | 45 非 PG + 13 PG = 58 |

R01 在 P10 用**当次实测**重跑，不照搬上表数量。

## 6. 未覆盖范围（本阶段明确不做）

- PolicyEngine / TaskRuntime / ToolRegistry 职责重构、检索算法改动、通用事件平台、Trace UI、
  跨 invocation 内容去重、JEV 预算调整、模型输出逐字复现。
- Research / Learning / Judge / 摘要 / 专家 / 管理端入口的**迁移**（2A 不做，2B 逐批做）。

## 7. Phase 2A 任务 → 测试 ID → pytest 节点映射

核对日期：2026-10-06 首次核对（HEAD `dcfc54e`）；2026-10-07 追加 §7.1 的 F04 行并复核。
最新一次机械对账：**§7 + §7.1 共 92 条引用，0 未解析，0 已实现但未登记**（6 个文件，127 个采集节点）。

| 任务 | 交付 | 测试 ID | pytest 节点 |
|---|---|---|---|
| P00 | 入口清单 + 第一阶段基线 | C01（2A 范围）、R01 | 本文件 §1–§5；R01 见 `phase2a/acceptance-report.md` §2.3 |
| P01 | `app/model_input_snapshot.py` | U01–U06、U08、U09 | `tests/test_model_input_snapshot.py::test_u01_key_order_does_not_change_the_digest_but_array_order_does`、`::test_u02_one_dimension_changes_its_digest_and_leaves_the_others_alone`、`::test_u03_mutating_the_original_request_cannot_reach_the_snapshot`、`::test_u04_each_to_request_copy_is_independent`、`::test_u05_*`（5 项）、`::test_u06_round_trip_covers_every_request_field_without_loss`、`::test_u08_only_the_memory_that_was_actually_sent_counts_as_included`、`::test_u09_a_complete_input_with_partial_provenance_still_freezes_and_sends` |
| P02 | 迁移 0025 / SQLite 47、约束 | P01、P02、P07 | `tests/integration/test_model_input_snapshot_postgres.py::test_p01_upgrade_from_the_previous_head_keeps_historical_calls_unbackfilled`、`::test_p02_empty_database_upgrade_matches_the_declared_head`、`::test_p02_snapshot_table_carries_its_constraints_and_binding_trigger`、`::test_p02_the_declared_head_is_reachable_without_rewriting_history`、`::test_p07_direct_sql_cannot_rewrite_or_unbind_a_snapshot` |
| P03 | `app/model_input_snapshot_store.py` | P03、P04、P06、P08、U07 | SQLite：`tests/test_model_input_snapshot_store.py::test_round_trip_returns_the_frozen_content_and_recomputes_the_digest`、`::test_p04_a_row_whose_digest_does_not_match_its_content_is_corrupt`、`::test_p04_a_row_whose_envelope_disagrees_with_its_owner_column_is_refused`、`::test_p04_a_self_consistent_envelope_cannot_impersonate_another_call`、`::test_p04_an_unknown_envelope_version_is_refused`、`::test_p06_a_binding_is_written_once_and_can_never_be_attached_later`、`::test_p06_reading_a_binding_for_an_unknown_call_or_snapshot_is_a_domain_error`、`::test_p08_identical_input_gets_independent_rows_and_cross_owner_reads_fail`、`::test_p08_two_calls_with_the_same_input_share_a_digest_but_not_a_row`、`::test_a_call_without_a_snapshot_is_legacy_rather_than_corrupt`、`::test_insert_accepts_a_caller_supplied_id_and_rejects_a_duplicate`、`::test_u07_a_snapshot_is_frozen_persisted_and_bound_in_one_transaction`、`::test_u07_derived_digests_come_from_the_frozen_content`、`::test_u07_a_pre_bound_snapshot_is_verified_and_never_trusted`、`::test_u07_a_derived_call_clears_the_inherited_binding_but_a_retry_keeps_it`、`::test_u07_idempotent_replay_neither_resends_nor_duplicates_the_snapshot`；PostgreSQL：`tests/integration/test_model_input_snapshot_postgres.py::test_p04_store_refuses_corrupt_and_impersonating_rows`、`::test_p08_identical_input_shares_a_digest_but_not_a_row` |
| P04 | `begin_invocation` 事务边界 + 幂等 | U07、P05、P06、I04 | `tests/test_model_input_snapshot_store.py::test_u07_*`（5 项，含 `::test_u07_idempotent_replay_neither_resends_nor_duplicates_the_snapshot`）；`tests/integration/test_model_input_snapshot_postgres.py::test_p05_a_failed_snapshot_write_rolls_back_the_invocation`、`::test_p05_a_failed_execution_context_write_rolls_back_snapshot_and_invocation`、`::test_p05_a_committed_call_can_be_read_back_through_the_snapshot`、`::test_p06_concurrent_calls_on_one_idempotency_key_have_exactly_one_winner`、`::test_p06_a_reused_key_with_different_input_or_identity_is_a_conflict`；`tests/test_snapshot_flow.py::test_i04_a_rewritten_input_opens_a_new_call_and_conflicts_on_a_reused_key` |
| P05 | `RoutedModelGateway.complete` | G01–G05、G07–G09 | `tests/test_snapshot_gateway.py::test_g01_the_snapshot_is_committed_before_the_request_is_sent`、`::test_g02_the_frozen_input_survives_mutation_of_the_original_request`、`::test_g03_a_retry_shares_the_snapshot_and_is_not_polluted`、`::test_g04_fallback_switches_profile_without_changing_the_input`、`::test_g05_a_fallback_that_cannot_hold_the_input_is_skipped_not_cropped`、`::test_g05_when_no_profile_can_hold_the_input_it_fails_closed`、`::test_g07_a_failed_snapshot_write_sends_nothing`、`::test_g07_a_binding_conflict_sends_nothing_and_does_not_fall_back`、`::test_g08_a_cancel_after_the_commit_sends_nothing_and_leaves_no_success`、`::test_g08_a_cancel_during_an_attempt_leaves_the_snapshot_unchanged`、`::test_g09_partial_output_blocks_unsafe_fallback_and_keeps_the_input` |
| P06 | direct 网关与协议派生 | G06、G10、G11 | `tests/test_snapshot_gateway.py::test_g06_a_direct_gateway_binds_exactly_one_invocation_and_snapshot`、`::test_g06_the_routed_internal_attempt_adds_no_second_invocation_or_snapshot`、`::test_g10_the_sent_body_is_the_adapter_output_of_the_frozen_input`（参数化 3：`[openai_compatible]`/`[anthropic]`/`[gemini]`）、`::test_g11_a_null_max_tokens_is_resolved_per_protocol_without_touching_the_snapshot`（参数化 3：同上） |
| P07 | 对话最终输入与来源 | I01–I05 | `tests/test_snapshot_flow.py::test_i01_the_first_conversation_call_freezes_what_it_sends`、`::test_i02_a_tool_result_produces_a_new_call_and_leaves_the_first_alone`、`::test_i03_the_same_purpose_twice_is_two_calls`、`::test_i04_a_rewritten_input_opens_a_new_call_and_conflicts_on_a_reused_key`、`::test_i05_a_memory_update_reaches_a_new_turn_and_leaves_the_old_snapshot_alone` |
| P08 | 工具内部编译与审批恢复 | I06、A01–A04 | `tests/test_snapshot_flow.py::test_i06_a_compile_inside_a_tool_gets_its_own_call_and_child_span`；`tests/test_snapshot_recovery.py::test_a01_a_resumed_approval_keeps_the_tool_identity_and_freezes_its_own_call`、`::test_a02_a_revoked_tool_resumes_into_no_call_and_no_new_snapshot`、`::test_a03_a_committed_call_is_readable_after_the_process_disappears`、`::test_a04_a_call_without_a_snapshot_stays_legacy_and_cannot_be_backfilled` |
| P09 | 学习资产冻结与撤销 | A05、A06、A08、I05 | `tests/test_snapshot_recovery.py::test_a05_an_asset_revoked_after_the_freeze_is_refused_before_the_send`（参数化 2：`[direct]`/`[routed]`）、`::test_a06_an_asset_revoked_between_attempts_stops_the_retry`（参数化 2：`[direct]`/`[routed]`）、`::test_a08_a_failing_asset_step_rolls_back_and_sends_nothing`；`tests/test_snapshot_flow.py::test_i05_a_memory_update_reaches_a_new_turn_and_leaves_the_old_snapshot_alone` |
| P10 | 读取能力与最小链路验收 | A07、M1 | `tests/test_snapshot_recovery.py::test_a07_snapshots_are_owner_scoped_and_leak_neither_text_nor_credentials`；M1 见 `phase2a/acceptance-report.md` §3 |

### 7.1 修复轮（2026-10-06）新增/加强的验收场景

对应 `ContextSnapshot-Phase2A修复任务与复验-2026-10-06.md` 第 2 节 R01–R06 与第 3 节 D/K/S/B。
本表给基名；`K02`/`K03`/`A05`/`A06` 为参数化用例，实际节点数按 `--collect-only` 计。

| 任务 | 缺陷 / 场景 | 测试 ID | pytest 节点 |
|---|---|---|---|
| R01、R02 | F01 发送前资产撤销（direct 与 routed 逐 attempt 检查） | D01–D07 | `tests/test_snapshot_gateway.py::test_d01_a_direct_gateway_refuses_the_first_send_after_a_revocation`、`::test_d02_a_direct_gateway_does_not_retry_past_a_revocation`、`::test_d03_a_direct_gateway_retries_normally_while_the_asset_stays_valid`、`::test_d04_a_call_without_the_optional_learning_service_still_sends`、`::test_d05_a_failing_required_check_leaves_no_open_attempt_or_reservation`、`::test_d06_a_callback_that_revokes_is_caught_before_the_wire`、`::test_d07_the_routed_gateway_keeps_its_per_attempt_check` |
| R03、R04 | F03 幂等调用身份比较（含 null、单边 harness、并发） | K01–K04 | `tests/test_model_input_snapshot_store.py::test_k01_an_identical_call_is_still_a_replay_and_writes_nothing`、`::test_k02_without_a_harness_every_public_identity_field_is_compared`（参数化 14）、`::test_k03_a_one_sided_harness_is_never_the_same_call`（参数化 2）、`::test_k04_the_same_columns_with_a_different_span_or_input_are_a_conflict` |
| R04 | F03 PostgreSQL 并发（同语义 replay / 异身份冲突） | K05、K06 | `tests/integration/test_model_input_snapshot_postgres.py::test_k05_concurrent_writers_with_the_same_identity_have_one_winner`、`::test_k06_concurrent_writers_with_different_execution_identities_conflict` |
| R05 | F02 编译器真实逻辑调用边界（编译 / 修复两个 span；retry 同调用） | S01–S05 | `tests/test_snapshot_flow.py::test_s01_a_real_compiler_repair_is_a_second_logical_call`、`::test_s02_a_network_retry_stays_inside_one_logical_call`、`::test_s03_a_repair_that_retries_keeps_its_own_identity`、`::test_s04_the_ambient_context_is_restored_after_a_compile`、`::test_s05_a_resumed_compile_keeps_its_tool_identity_and_pinned_bundle` |
| R06 | 真实来源与证据（Memory 版本、Skill 版本绑定与撤销、partial provenance） | B01–B03 | `tests/test_snapshot_flow.py::test_b01_a_memory_update_is_reselected_and_recorded_by_the_next_turn`、`::test_b02_a_skill_update_binds_a_new_version_and_a_revoked_one_stops_the_send`、`::test_b03_a_partial_input_with_a_known_reference_freezes_sends_and_stays_honest` |
| R09（2026-10-07） | F04 响应后撤销的 usage 保留与结算（含无/部分 usage、无重试、成功路径不变、单次结算） | D08–D12 | `tests/test_snapshot_gateway.py::test_d08_a_revocation_after_the_response_keeps_the_usage_and_settles_it`、`::test_d09_a_revocation_after_the_response_without_usage_does_not_fabricate_tokens`（参数化 2：`[usage-absent]`/`[usage-partial]`）、`::test_d10_a_revocation_after_the_response_does_not_retry_or_rewrite_the_snapshot`、`::test_d11_a_valid_response_after_the_check_still_succeeds_and_settles`、`::test_d12_a_settled_attempt_cannot_be_settled_a_second_time` |


补充说明：I02 与 I06 在网关层表达"首次 LLM → 工具 → 下一次 LLM"与"工具内部编译"，因为该层就是冻结点所在；
对话适配器本身的冻结由 I01 经真实 `LiveConversationModel` 覆盖，审批恢复的完整链路由 A01/A02 经真实
`ManagedTurnWorker` → `ChatToolRunner` → `GoalProgramService` 覆盖。

**测试 P03 的落点**：测试 ID `P03`（"正常事务创建快照 + invocation，随后更新 invocation 状态，绑定完整、外键有效、状态更新成功"）
没有独立命名的测试函数，其断言分散在：
`tests/test_model_input_snapshot_store.py::test_u07_a_snapshot_is_frozen_persisted_and_bound_in_one_transaction`（三者同事务写入、绑定完整）
与 `tests/integration/test_model_input_snapshot_postgres.py::test_p07_direct_sql_cannot_rewrite_or_unbind_a_snapshot` 的**步骤 3**
（`UPDATE ... SET status='SUCCEEDED'` / `root_budget_id=NULL` 仍成功，且绑定列保持不变）。
这是刻意的：把"状态机可更新"和"绑定列不可改写"放在同一条数据库级测试里断言，才能证明触发器的作用域没有误伤状态机。

**参数化节点**：`U02`、`U05` 的部分用例、`G10`、`G11` 为参数化节点，本表给出基名（不带 `[...]`），
pytest 允许用基名选择全部参数；实际节点数按 `pytest --collect-only` 计，与本表项数不是同一个量。
映射完整性由 `scripts/verify_acceptance_nodes.py` 机械校验（把本节的引用与 `--collect-only` 结果对账，
含"已实现但未登记"的反向检查）。
