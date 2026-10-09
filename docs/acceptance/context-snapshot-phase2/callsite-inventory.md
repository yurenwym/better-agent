# ContextSnapshot 第二阶段 · 真实模型调用入口清单（P00）

- 日期：2026-09-30（§7.1 于 2026-10-06 / 2026-10-07 追加；§8 于 2026-10-08 追加）
- 2A 历史核对基线：`dcfc54e358bdc8ae92e2ea1a41bf56694ea080f7`（2026-10-07 起，Phase 2A 主体已提交为
  `9992533fac42f19b9e1dbe321612d94912e6e8b3`；F04 随后在 `37a4e9af39faa781a92db1227e444173e13c2db0` 提交）
- 2B 开始基线：`37a4e9af39faa781a92db1227e444173e13c2db0`，分支 `front0920`。本次开发尚未形成提交。
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
| 23 | `model_admin.py::_verify_live` | 管理端指定版本验证 | ✅ 注入 `ModelAdminService.control_store` 与服务 owner | ✅ API `POST /api/model-profile-versions/{id}/verify` | **2B（P13）** | B08、T19–T22 已验收通过；整体 M2 未完成 |
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

## 8. Phase 2B 稳定入口登记（按符号，不依赖行号）

核对基线 `37a4e9a`。下列 ID 是本期稳定 callsite ID；动态测试映射在 2B 验收文件落地后补全。标为“待测”的入口不能计入 M2 覆盖率。

| Callsite ID | 生产入口 / 逻辑调用 | Owner 来源 | 接入与目标验收 |
|---|---|---|---|
| CS-RS-01 | `LiveResearchModel.plan/distill/reflect/curate/audit/summarize` 结构化请求 | `ManagedResearchWorker.run_once` 的源 turn/thread owner | Routed；T04 有 8 个方法分支的独立连接正向证据；T05 JSON 修复新 invocation/snapshot/span、T06 网络 retry 复用 invocation/snapshot 已动态通过。Worker job 到 owner/root/bundle 的来源链仍待测 |
| CS-RS-02 | `LiveResearchModel.write`、报告修复、连续章节 | 源 turn/thread owner；固定 runtime bundle | Routed；T07 部分（write 单次适配器发送），连续章节、报告修复、Worker 恢复待测 |
| CS-RS-03 | Research Worker → `LiveSafetyJudge.judge`（`judge_research_output`） | 持久化 research job 的源 turn/thread owner | ambient context；T36/T37，待测 |
| CS-AG-01 | `AgentTaskWorker` 专家执行与汇总（`execute_bundle`、`synthesize`、`synthesize_user_task`） | 持久化 run owner | Routed；T09/T10，待测 |
| CS-AG-02 | Agent Worker → `LiveSafetyJudge.judge`（`judge_expert_output`） | 持久化 run owner | ambient context；T36，待测 |
| CS-LN-01 | learning agent/extraction/prompt/eval 生成与 Judge | 已授权 learning job owner、root、bundle | Routed；T11 部分（LearningAgent、ConstraintExtractor、LearningJudge 各单入口），pipeline/job/root/bundle、prompt、来源冲突和撤销待测 |
| CS-EV-01 | `LiveBehaviorRunner` baseline、candidate、quality Judge | `EvolutionService.evaluate_builtin` 的已授权 owner | Routed；T13/T14，开发中 |
| CS-EV-02 | `learning_replay` selector/baseline/candidate/Judge | 已授权 replay job owner | Routed；T13/T14，待测 |
| CS-EV-03 | `RealEvaluationRunner` profile arms 与 paired Judge | evaluation run/config owner、root | Direct + control store；T15，待测 |
| CS-GR-01 | `AgentRuntime._model_call`：clarification/planning/react/reflection 与结构修复 | run source turn → thread owner；缺失则拒绝 | Routed；T33/T37，开发中 |
| CS-GR-02 | Agent Runtime → `LiveSafetyJudge.judge`（`judge_run_output`） | run source turn → thread owner；缺失则零发送 | ambient context；T36/T37，开发中 |
| CS-GR-03 | `ExecutionMaterializer.project_plan_for_execution` → `PlanExecutionCompiler` | 授权 turn/thread owner、pinned bundle/root | T34：`tests/test_snapshot_goal_runtime_entries.py::test_t34_projection_claim_failure_and_ready_retry_keep_the_snapshot`；tenant-b、失败零发送、READY 恢复不重发（SQLite） |
| CS-GP-01 | `ManagedGoalReviewWorker.daily_review`、`period_review` 非工具编译 | goal program 行 owner；`goal_operation` root | T35 partial：`tests/test_snapshot_goal_runtime_entries.py::test_t35_daily_and_period_review_restore_context_after_failure`；缺 PG root 与非默认 program owner 补验 |
| CS-GP-02 | API `adjust_goal_program` 非工具编译与修复 | goal program 行 owner；`goal_operation` root | T35 partial：`tests/test_snapshot_goal_runtime_entries.py::test_t35_adjustment_api_repair_has_independent_committed_snapshots`；缺 PG root 与非默认 program owner 补验 |
| CS-MA-01 | `MemoryArchiveWorker` → `summarize_episode` | 持久化 archive claim owner/thread | T16 部分：`test_snapshot_auxiliary_entries.py::test_archive_worker_summary_uses_claim_owner_and_committed_snapshot` 驱动真实 ConversationArchiver 与 LiveEpisodeSummarizer；恢复重试及 Harness 来源仍待测 |
| CS-MR-01 | `MemoryReferenceResolver.resolve_memory_reference` | 显式授权 owner + 最终候选列表 | T17 部分：`test_snapshot_auxiliary_entries.py::test_reference_resolution_freezes_the_final_candidate_list` 验证候选选择、owner 和实际发送冻结；超时/取消与 owner 交错仍待测 |
| CS-CA-01 | `LiveConversationModel` 分类、澄清、无历史回答及修复 | 已授权 turn/thread owner | T38：两种分类辅助调用和主回答各自绑定唯一 snapshot/span；T18 的澄清、无历史回答及修复分支仍待验 |
| CS-MD-01 | `ModelAdminService._verify_live` 指定 profile version | 服务端 `ModelAdminService.owner_id` | Direct + control store；B08/T19–T22 验收通过 |
| CS-EV-04 | `LivePromptCandidateProposer._propose` | `EvolutionCandidateGenerator.generate` 写入 pattern 的授权 owner | 2026-10-09 静态对账补登记；缺 owner 零发送已有回归，正向发送未闭环 |
| CS-CA-02 | `ManagedTurnWorker._finish_exposure` → `LiveSafetyJudge.judge` | 持久化 thread owner；purpose=judge_conversation_output | 2026-10-09 静态对账补登记；动态全契约待验 |
| CS-GP-03 | Goal Program preview / 工具内编译 → `GoalProgramCompiler._validated` | program owner 或授权工具 Harness | 2026-10-09 补登记；2A I06 有局部证据，不能遗漏于全入口分母 |

2026-10-09 对账增量：注册入口现为 21 个，**不是最终确认的生产分母**。`scripts/audit_snapshot_callsites.py` 只读扫描 AST，按稳定符号分类候选、排除业务 complete/CLI/内部 transport，并对照发送日志与 evidence 的 pytest 收集结果。动态分派及共享 adapter 的逐分支可达性仍需核实，T32 不因此自动通过。专家新增节点：`tests/test_snapshot_agent_entries.py::test_t09_t10_expert_workers_isolate_interleaved_owners_and_judges`（两种汇总分支）；Research 新增节点：`tests/test_snapshot_research_entries.py::test_t08_t36_research_worker_restores_context_and_judges_recovered_job`（异常/取消）。

### 8.1 Direct/transport 和排除项

| Symbol | 分类与依据 |
|---|---|
| `RoutedModelGateway._execute_attempt` → `ModelGateway._attempt` | routed 内部 transport。外层已有 invocation/snapshot，不得二次绑定，T27。 |
| `ModelGateway._attempt` | 低层 HTTP transport。生产只能由受控 direct/routed 路径调用；其余须通过明确 offline API，T25–T28。当前旁路待 B09 收口。 |
| `ModelGateway.provider_payload` | 纯协议转换，不发网络请求。 |
| `eval.py::_run_live_smoke` | CLI `--mode live` 当前构造无 store、无 transport 的 gateway；默认 fail-closed 并将 `GatewayError` 记为 smoke 失败，不会打开 HTTP。明确列为非 API 可达脚本入口，不计生产发送覆盖；若恢复真实 smoke 发送，必须注入 `ModelControlStore` 和可信 service owner。 |
| `evals.py` | 确定性 Runtime 使用 `MockModelGateway`；底层 retry smoke 明确注入 `httpx.MockTransport`，属于测试 transport，未发现生产装配路径。 |
| `model_capacity_migration.plan_model_capacity_migration` | 只读 profile/version 并生成计划，不调用 verifier；临时 `ModelAdminService` 只执行列表/读取。 |
| `model_capacity_migration.apply_model_capacity_migration` | 更新容量版本但不调用 verifier；临时 `ModelAdminService` 只执行读取/加版本。当前两处均不可能触发 `_verify_live`，因此不要求注入发送用 control store；若后续增加 live verification，必须注入受控 store。 |

### 8.2 Owner 构造审计及 M1 对账

- 2B 基线 HEAD：`37a4e9a`；工作区初始仅有 Phase 2B 任务书未跟踪。Python `3.13.0`；仓库 `backend/agent.db` 的 SQLite `schema_migrations` max version 为 `24`；Alembic 声明 head 为 `20260930_0025`。本轮未运行隔离 PostgreSQL，因此未复核 M1 的数据库证据。
- M1：已按 Phase 2A 报告追加记录完成本轮复验；快照/网关/恢复 SQLite 119 passed，M5 gates + 离线 PG harness 25 passed，真实评估 35 passed，对话 worker 41 passed，Phase 2B 指定 PG 集合 49 passed。历史失败记录仍保留；M1 结论基于这些复验，不依赖 HEAD 推断。
- 生产代码 `ModelCallContext(...)` 审计发现：`runtime.AgentRuntime._model_call`、`runtime.AgentRuntime._finish_exposure`、`research.worker` 两处存在缺 owner/默认回落；`evolution.LiveBehaviorRunner` 缺 owner 且 answer/Judge 共用 ambient identity；`RoutedModelGateway.complete` 无 context 时创建隐式身份；`resolved_profile` / `output_limit` 回落 `local-user`；`model_admin._verify_live` 是无 store direct 调用。这些分别登记到 B06A/B08/B09 与 T19–T22、T33–T37。
- `ModelCallContext.owner_id` 已改为默认 `None`；Routed 与受控 direct 发送路径在打开 invocation 前拒绝缺失 owner；无存储 `ModelGateway` 仅在显式注入 transport 的离线/测试构造下可运行。定向动态拒绝证据：`tests/test_snapshot_gateway.py::test_routed_gateway_refuses_missing_execution_identity_before_any_send` 与 `::test_direct_gateway_rejects_missing_identity_and_uncontrolled_http`。Phase 2A 全量回归仍未完成，不能据此宣称 M1 或 M2 通过。
- 对 `backend/app/**/*.py` 做 AST 枚举，当前没有未显式写 `owner_id` 的 `ModelCallContext(...)` 直接构造点；仍需人工追踪从服务配置、授权 run/turn/job 到这些构造点的可信性，AST 结果不代替生产可达性审计。
- 已采集到的部分 pytest node（这些只覆盖表中逻辑的一部分，不代表对应入口完整验收）：
  - T02：`tests/test_snapshot_gateway.py::test_t02_transport_recorder_reads_the_committed_binding_before_send`、`::test_t02_transport_recorder_fails_on_an_unbound_send`（复用 `tests/snapshot_entrypoint_helpers.py::CommittedSnapshotTransport`）。
  - T25/T37：`tests/test_snapshot_gateway.py::test_routed_gateway_refuses_missing_execution_identity_before_any_send`、`::test_direct_gateway_rejects_missing_identity_and_uncontrolled_http`。
  - T33：`tests/test_snapshot_flow.py::test_t33_runtime_json_repair_gets_a_new_snapshot_and_child_span`（LiveRuntimeModel adapter，不含 `/api/goals` 端到端 Runtime）。
  - T38：`tests/test_snapshot_flow.py::test_t38_conversation_auxiliary_calls_and_main_call_have_sibling_spans`。

#### Phase 2B adapter evidence update (2026-10-08)

- Research adapter partial dynamic nodes (8 collected): `tests/test_snapshot_research_entries.py::test_research_entrypoint_sends_only_after_committed_snapshot[plan|distill|reflect|curate|write|summarize|audit|repair]`. The transport hook checks committed binding through an independent connection. It does not drive `ManagedResearchWorker`, so it does not prove the job-to-owner source chain.
- Learning adapter partial dynamic nodes: `tests/test_snapshot_learning_entries.py::test_learning_agent_generation_freezes_authorized_job_input`, `::test_learning_constraint_extraction_freezes_source_excerpt`, `::test_learning_judge_uses_its_own_committed_call`. The transport hook checks binding but does not drive the Learning job pipeline.
- PostgreSQL entrypoint node: `tests/integration/test_snapshot_entrypoints_postgres.py::test_research_plan_is_committed_before_postgres_transport_send`. Together with the six listed Phase 2A/budget/settlement integration groups: 47 passed in the isolated PostgreSQL project.
- Research logical-boundary nodes: `tests/test_snapshot_research_entries.py::test_research_invalid_json_repair_is_a_new_logical_call` (T05) and `::test_research_network_retry_reuses_the_frozen_logical_call` (T06). Both send via `CommittedSnapshotTransport`; repair uses separate invocation/snapshot/span, while retry has two attempts under one invocation/snapshot.
