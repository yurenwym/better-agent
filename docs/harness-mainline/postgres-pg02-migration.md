# PostgreSQL test migration (PG02)

Status: **PG02 batches 1–2 complete** — notification, trace, archive, shared
lease and TaskRuntime run on isolated PostgreSQL. Learning/Embedding remains.

## 1. Scope

Migrated in this batch (all original nodeids retained):

- `backend/tests/test_notifications.py` — 26 test functions (45 runtime cases
  including `test_channel_success_fields` parametrization).
- `backend/tests/test_trace_query.py` — 29 test functions (32 runtime cases
  including the invalid-page parametrization).

Later PG02 modules identified from the plan and the PG01 inventory (not migrated
here): `test_archive_events.py`, `test_memory_archive.py`,
`test_lease_interface.py`, `test_task_runtime.py`,
`test_learning_promotion.py`, `test_learning_v3_fixes.py`, plus directly related
DB-backed Learning/Embedding coverage to assess in that batch
(`test_learning_v3_wiring.py`, `test_learning_targets.py`,
`test_learning_decision.py`, `test_learning_pipeline_v3.py`,
`test_learning_agent.py`, `test_embedding.py`, and the characterization/contract
learning modules). Existing `tests/integration/` PG tests are untouched.

## 2. Fixture contract

Both modules use the accepted PG01 fixtures:

```python
@pytest.fixture()
def database(migrated_postgres_url, isolated_postgres_test, tmp_path, monkeypatch):
    monkeypatch.delenv("BETTER_AGENT_TEST_ALLOW_SQLITE", raising=False)
    monkeypatch.setenv("DATABASE_URL", str(migrated_postgres_url))
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    assert db.backend == "postgresql"
    yield db
    db.close()
```

`isolated_postgres_test` truncates before/after every test; `migrated_postgres_url`
owns a unique Compose database. No SQLite fallback remains in the migrated DB
paths; `BETTER_AGENT_TEST_ALLOW_SQLITE` is actively removed inside the fixture.

## 3. Mapping and adaptations

All assertions are retained. The only adaptations are fixture construction,
PG-valid raw SQL, and one test-double contract method.

| old nodeid | new nodeid | assertion adaptation |
|---|---|---|
| `backend/tests/test_notifications.py::test_business_error_is_failure_and_secret_never_returned` | same | DB via PG fixture |
| `…::test_channel_env_name_is_validated_and_api_shape_masked` | same | DB via PG fixture |
| `…::test_private_webhook_is_blocked_by_default` | same | DB via PG fixture |
| `…::test_cross_owner_and_disabled_channel_send_nothing` | same | DB via PG fixture |
| `…::test_timeout_is_unknown_and_is_not_sent_again` | same | DB via PG fixture |
| `…::test_connect_failure_is_not_automatically_resent` | same | DB via PG fixture |
| `…::test_explicit_retry_is_a_new_send_after_unknown` | same | DB via PG fixture |
| `…::test_revoked_report_is_not_retried` | same | DB via PG fixture |
| `…::test_one_claim_sends_once` | same | DB via PG fixture |
| `…::test_disable_after_claim_does_not_post` | same | DB via PG fixture; `DisablingClient` test double gained a no-op `aclose()` because the service always closes its client (no assertion changed) |
| `…::test_legacy_pending_is_unknown_and_unowned_channels_stay_unowned` | same | real PostgreSQL: dedicated owned DB at Alembic `20261013_0029`, equivalent old rows seeded (thread/turn/job; a second job on a deleted thread for the unowned delivery), actual `alembic upgrade head` performs the transition, all 6 assertions retained |
| `…::test_fresh_database_has_delivery_identity` | same | DB via PG fixture (unique dispatch key violation still asserted with `pytest.raises(Exception)`) |
| `…::test_non_object_and_invalid_responses_stay_unknown` | same | DB via PG fixture |
| `…::test_exception_after_the_provider_accepts_is_unknown` | same | DB via PG fixture |
| `…::test_same_retry_key_sends_once_and_a_new_key_sends_again` | same | DB via PG fixture |
| `…::test_unknown_resend_requires_confirmation_and_keeps_the_original` | same | DB via PG fixture |
| `…::test_deleting_a_channel_keeps_inflight_and_unknown_history` | same | DB via PG fixture |
| `…::test_corrupt_gzip_after_accept_is_unknown` | same | DB via PG fixture |
| `…::test_inflight_sending_blocks_an_unconfirmed_retry` | same | DB via PG fixture |
| `…::test_channel_success_fields` | same (20 parametrized cases) | DB via PG fixture; all 20 classifications retained |
| `…::test_webhook_non_json_success_is_not_a_provider_code` | same | DB via PG fixture |
| `…::test_automatic_delivery_does_not_send_while_retry_is_sending` | same | DB via PG fixture |
| `…::test_automatic_delivery_does_not_resend_unknown` | same | DB via PG fixture |
| `…::test_late_automatic_delivery_does_not_repeat_a_successful_send` | same | DB via PG fixture |
| `…::test_inflight_automatic_delivery_blocks_an_ordinary_retry` | same | DB via PG fixture |
| `…::test_confirmed_retry_replay_stays_one_extra_send` | same | DB via PG fixture |
| `backend/tests/test_trace_query.py::test_by_thread_and_by_trace_merge_one_conversation` | same | DB via PG fixture |
| `…::test_trace_is_owner_isolated_and_legacy_events_are_marked` | same | DB via PG fixture |
| `…::test_deleted_thread_hides_thread_and_trace` | same | DB via PG fixture |
| `…::test_deleted_thread_hides_its_task` | same | DB via PG fixture |
| `…::test_task_query_is_scoped_to_one_task` | same | DB via PG fixture |
| `…::test_task_query_filters_before_pagination` | same | DB via PG fixture |
| `…::test_expert_task_query_and_deleted_thread` | same | DB via PG fixture |
| `…::test_trace_query_paginates_with_a_stable_cursor` | same | DB via PG fixture |
| `…::test_legacy_trace_paginates_after_the_first_page` | same | raw insert `datetime('now')` → `clock_timestamp()::text` (PG-valid timestamp text) |
| `…::test_legacy_trace_filters_before_page_cap` | same | same SQL adaptation for its raw legacy inserts |
| `…::test_trace_query_rejects_an_invalid_page` | same (4 parametrized cases) | DB via PG fixture; all invalid-page assertions retained |
| `…::test_trace_lookup_uses_the_indexed_column` | same | DB via PG fixture; PG `trace_id` column populated |
| `…::test_direct_goal_run_trace_is_owner_readable` | same | runtime built on the PG `database` via local `_make_runtime` |
| `…::test_legacy_scan_truncation_is_reported` | same | DB via PG fixture |
| `…::test_trace_api_authorizes_before_reading` | same | PG-backed runtime via `_make_runtime` |
| `…::test_http_trace_redacts_nested_secrets_and_malformed_envelopes` | same | PG-backed runtime; redaction/compatibility assertions retained |
| `…::test_same_timestamp_and_later_insert_keep_pages_stable` | same | DB via PG fixture |
| `…::test_duplicate_event_id_across_streams_is_returned_once` | same | DB via PG fixture |
| `…::test_mixed_history_pages_keep_occurred_at_and_identity` | same | DB via PG fixture |
| `…::test_same_timestamp_across_streams_uses_one_sort_key` | same | DB via PG fixture |
| `…::test_insert_ahead_of_the_cursor_does_not_shift_the_snapshot` | same | DB via PG fixture |
| `…::test_http_trace_keeps_bad_envelopes_on_the_page` | same | PG-backed runtime via `_make_runtime` |
| `…::test_expert_subtask_reads_its_own_agent_events` | same | DB via PG fixture |
| `…::test_refused_source_rows_do_not_skip_a_valid_event` | same | DB via PG fixture |
| `…::test_duplicate_event_id_is_returned_once_across_pages` | same | DB via PG fixture |
| `…::test_refused_representative_hides_the_later_copy` | same | DB via PG fixture |
| `…::test_refusal_scan_budget_stays_resumable` | same | DB via PG fixture |
| `…::test_http_legacy_cap_without_a_match_stays_resumable` | same | PG-backed runtime; legacy cap/redaction assertions retained |
| `…::test_finished_legacy_scan_without_a_match_is_missing` | same | PG-backed runtime; missing-trace assertions retained |

Machine-level fixture changes: `Database(tmp_path / "<name>.db")` →
`database` fixture; `_seed`/`_seed_research`/`_expert_db` now take the fixture;
`make_runtime(tmp_path, ...)` → local `_make_runtime(database, tmp_path, ...)`.
No test function was renamed; no assertion was removed or weakened.

## 4. Evidence

```powershell
# backend, D:/pycharm/python.exe
python -m pytest tests/test_notifications.py::test_legacy_pending_is_unknown_and_unowned_channels_stay_unowned -q --tb=short
# → 1 passed in 9.67s   (real 20261013_0029 → head on an owned DB)

python -m pytest tests/test_notifications.py tests/test_trace_query.py -q --tb=short
# → 77 passed in 49.21s  (final PostgreSQL-only batch command)

# intermediate (before the upgrade test was rewritten to real PG)
python -m pytest tests/test_notifications.py -q --tb=short
# → 45 passed in 21.28s (after adding the test-double aclose)
python -m pytest tests/test_trace_query.py -q --tb=short
# → 32 passed in 31.77s
```

Real isolated PostgreSQL (unique Compose project, per-test truncation); no paid
provider or webhook. `git diff --check` exits 0. After the run, no new
`better-agent-pgtest-*` containers remained.

## 5. Old-revision notification upgrade

`test_legacy_pending_is_unknown_and_unowned_channels_stay_unowned` now runs the
real migration on PostgreSQL. It allocates a dedicated owned database from the
session `postgres_url`, runs `alembic upgrade 20261013_0029` (the revision
immediately before `20261015_0030`), seeds the equivalent old
notification/channel/thread/turn/research_job rows, then runs
`alembic upgrade head`. The real 0030 migration performs the transition; no
manual UPDATE mimics it. The SQLite "job with no thread" input is represented by
a real `research_jobs` row pointing at a deleted thread, which the 0030 owner
backfill excludes, so that delivery remains unowned. All six business assertions
are unchanged. The owned database is dropped in `finally`; the shared migrated
database is never downgraded.

## 6. Non-claims

- Batches 1–2 of PG02 are migrated; Learning/Embedding remains.
- The PG01 inventory was not regenerated for these batches (its nodeids are
  unchanged); it is updated only after the final PG02 batch.
- Production changes are limited to the notification test-double `aclose()`
  (test support) and the PostgreSQL completion-trigger migration `20261019_0034`
  plus the matching `POSTGRES_SCHEMA_HEAD` constant.
- Notification, trace, archive, lease and TaskRuntime DB coverage in the migrated
  modules is PostgreSQL-only; no SQLite fixture/branch remains there.

## 7. Batch 2: Archive, shared lease, TaskRuntime

Migrated with the shared `backend/tests/pg02_helpers.py` fixture/helpers
(`database`, `make_runtime`, `thread_with_turns`, `completed_thread`) so the four
modules do not depend on `test_runtime.make_runtime` or
`test_static_archive_policy._thread_with_turns` SQLite construction. All 46
original nodeids are retained unchanged; the original assertions are retained.

```
backend/tests/test_archive_events.py::test_recovery_and_commit_are_visible_without_moving_the_cursor_early → same
backend/tests/test_archive_events.py::test_changed_source_version_cannot_commit_or_advance_coverage → same
backend/tests/test_archive_events.py::test_expired_lease_cannot_renew_commit_or_fail_before_recovery → same
backend/tests/test_archive_events.py::test_recovery_event_failure_keeps_the_expired_lease → same
backend/tests/test_lease_interface.py::test_task_runtime_satisfies_the_shared_lease_protocol → same
backend/tests/test_lease_interface.py::test_only_one_worker_holds_a_live_claim → same
backend/tests/test_lease_interface.py::test_expired_lease_recovers_once_and_rejects_the_old_epoch → same
backend/tests/test_lease_interface.py::test_reclaim_after_expiry_fences_the_previous_epoch → same
backend/tests/test_lease_interface.py::test_cancel_request_blocks_completion_once → same
backend/tests/test_lease_interface.py::test_archive_exposes_the_shared_lease_vocabulary → same
backend/tests/test_lease_interface.py::test_lease_loss_blocks_heartbeat_and_commit → same
backend/tests/test_lease_interface.py::test_recovery_is_idempotent_and_reports_its_events → same
backend/tests/test_lease_interface.py::test_recover_expired_retryable_keeps_coverage_and_resets_state → same
backend/tests/test_lease_interface.py::test_recover_expired_after_max_attempts_dead_letters → same
backend/tests/test_lease_interface.py::test_worker_reclaims_an_expired_lease_and_fences_the_old_worker → same
backend/tests/test_lease_interface.py::test_worker_recovery_failure_is_never_fatal → same
backend/tests/test_memory_archive.py::test_archive_retry_is_scoped_and_keeps_coverage_until_commit → same
backend/tests/test_memory_archive.py::test_old_retry_cannot_restart_a_new_failure → same
backend/tests/test_memory_archive.py::test_proactive_archive_uses_lower_hot_window_without_mutating_budget → same
backend/tests/test_memory_archive.py::test_archive_old_version_is_rejected_before_model_and_preserves_source → same
backend/tests/test_memory_archive.py::test_live_summary_declares_arrays_and_reserves_generation_budget → same
backend/tests/test_memory_archive.py::test_archive_model_call_inherits_source_turn_bundle_and_root_budget → same
backend/tests/test_memory_archive.py::test_archiver_keeps_recent_window_and_is_idempotent → same
backend/tests/test_memory_archive.py::test_archiver_never_crosses_a_pending_turn → same
backend/tests/test_memory_archive.py::test_archiver_keeps_whole_turn_bundles → same
backend/tests/test_memory_archive.py::test_archiver_accepts_ask_turn_without_duplicate_source_ids → same
backend/tests/test_memory_archive.py::test_archiver_hashes_excluded_terminal_messages_without_summarizing_them → same
backend/tests/test_memory_archive.py::test_archiver_without_summarizer_fails_closed → same
backend/tests/test_memory_archive.py::test_terminal_turn_signal_is_consumed_by_managed_worker → same
backend/tests/test_memory_archive.py::test_managed_worker_preserves_signal_when_enqueue_fails → same
backend/tests/test_memory_archive.py::test_archiver_rejects_invalid_json_summary → same
backend/tests/test_memory_archive.py::test_archiver_rejects_summary_references_outside_source_batch → same
backend/tests/test_memory_archive.py::test_archiver_rejects_assistant_only_decision → same
backend/tests/test_memory_archive.py::test_archiver_rejects_structurally_bounded_but_oversized_summary → same
backend/tests/test_memory_archive.py::test_archiver_does_not_commit_when_source_changes_before_commit → same
backend/tests/test_memory_archive.py::test_archiver_requeues_current_hash_when_source_changes_before_summary → same
backend/tests/test_memory_archive.py::test_archiver_old_lease_cannot_commit_after_takeover → same
backend/tests/test_memory_archive.py::test_archiver_commit_failure_rolls_back_episode_and_cursor → same
backend/tests/test_memory_archive.py::test_archiver_chunks_a_single_oversized_turn_and_advances_coverage → same
backend/tests/test_memory_archive.py::test_chunk_summary_cannot_cite_a_message_outside_its_chunk → same
backend/tests/test_memory_archive.py::test_chunk_merge_preserves_same_evidence_in_different_structured_fields → same
backend/tests/test_memory_archive.py::test_archiver_renews_lease_during_slow_chunk_summary → same
backend/tests/test_memory_archive.py::test_managed_worker_keeps_signal_when_ack_delete_fails → same
backend/tests/test_task_runtime.py::test_reused_worker_name_cannot_revive_old_epoch → same
backend/tests/test_task_runtime.py::test_denied_lease_decision_does_not_change_the_task → same
backend/tests/test_task_runtime.py::test_standalone_goal_requires_persisted_identity_and_policy → same
```

New PostgreSQL coverage added: `test_two_connections_allow_one_live_claim_and_fence_the_old_owner`
(two distinct `Database` pools; B waits in a worker thread; the wait is proven via
`pg_stat_activity`/`pg_blocking_pids`; A commits; B sees the live lease and returns
`None`; after expiry B takes over, the old token's heartbeat/finish are rejected,
and B finishes; raw `pg_fixtures._connect` probes use `%s`, worker-B exceptions
are captured and re-raised after join, transactions carry finite 8s lock/15s
statement timeouts, and A commit/release/join/close are all in `finally`) and
`backend/tests/test_archive_signal_trigger.py` (six deterministic regressions:
one signal on transition; no duplicate on repeated COMPLETED; completed→running→
completed re-fires but `ON CONFLICT` keeps one unchanged row; nonterminal none;
an INSERT with COMPLETED does not signal under the UPDATE-only contract; terminal
rollback rolls back the signal and leaves the turn ROUTING; plus a real
`20261019_0033 → head` upgrade on a dedicated owned DB).

Failures and fixes:
- First PG run of the four modules: **2 failed, 26 passed**. `task_budget_roots`
  parent row was missing for the PG FK (test fixture parent added), and PG had no
  turn-completion signal trigger (`0 != 1`).
- A synthetic `INSERT INTO memory_archive_signals` was tried and produced a
  **28-pass** run; that evidence was **rejected** and the insertion was removed.
  It did not represent the production transition.
- Real fix: migration `20261019_0034` adds a transactional
  `memory_archive_signal_on_completed` function and an
  `AFTER UPDATE OF status ON turns` trigger (`NEW.status='COMPLETED' AND
  OLD.status IS DISTINCT FROM 'COMPLETED'`, `INSERT ... ON CONFLICT (turn_id) DO
  NOTHING`). `POSTGRES_SCHEMA_HEAD` is now `20261019_0034`; the initial schema and
  R01/R02 logic are untouched.
- The slow-summary renewal test replaced `asyncio.sleep(.22)` with a heartbeat
  observed only after the summarizer started (`started.is_set()` and not
  `release.is_set()`), discarding the pre-summary initial renewal; it uses a
  bounded `started` wait, clears the event, waits for the real periodic
  heartbeat, asserts the summary is still blocked when seen, and releases/awaits
  the task in `finally`. Both original assertions are preserved. The bounded
  `started` wait and `renewed.clear()` are inside the same `try`, so a startup
  timeout also reaches the `finally` release and bounded task wait. Final
  single-test run: `1 passed in 7.07s`.
- Schema delta (explicit PG02 change, not a frozen-hash violation): new migration
  `20261019_0034` and the `POSTGRES_SCHEMA_HEAD` constant. Other head checks
  import that constant; no other hardcoded head reference exists.

Evidence (final):
```
D:/pycharm/python.exe -m pytest tests/test_archive_signal_trigger.py -q --tb=short
→ 6 passed

D:/pycharm/python.exe -m pytest tests/test_lease_interface.py -q --tb=short
→ 13 passed (includes the two-connection lock/fence regression)

D:/pycharm/python.exe -m pytest tests/test_archive_events.py tests/test_memory_archive.py tests/test_lease_interface.py tests/test_task_runtime.py tests/test_archive_signal_trigger.py -q --tb=short
→ 58 passed in 34.36s
```
Intermediate (not final closure): the first trigger file run was 4 passed in
12.62s; the four modules alone were 56 passed in 32.81s before the conflict /
insert-boundary / deterministic-renewal closures.
No `sqlite3`/SQLite fixture branch remains in the four modules or
`pg02_helpers.py`; `alembic heads` reports `20261019_0034`; no test Compose
container remained after the runs.

## Batch 3 — Learning persistence (in progress; paused for helper safety)

Scope started: `test_learning_targets.py` (15 functions / 17 runtime cases) and
`test_learning_promotion.py` (43 functions / 52 runtime cases). Both now use
`tests/pg02_learning_helpers.py`, which passes an explicit PostgreSQL URL and
reuses the accepted `pg02_helpers.database` fixture. The remaining Learning
modules (`test_learning_v2.py`, `test_learning_pipeline_v3.py`,
`test_learning_v3_fixes.py`, `test_learning_decision.py`,
`test_learning_v3_wiring.py`, `test_learning_characterization.py`) and the
Embedding verification are not migrated yet; the main batch-3 task remains
open.

Initial failure (kept as evidence): the first targets run was **17 failed in
51.04s**. `build_runtime` attempted an ambient embedding client and `httpx`
rejected the malformed proxy/URL (`Invalid port: ':1]'`); the runtime Database
pools were also not closed, producing pool-thread shutdown warnings. That run
is not converted into a success.

Helper safety boundary (`backend/tests/pg02_learning_helpers.py`):
- Every `build_runtime` database allocation is recorded before `Database.__init__`
  completes, so a startup failure after pool creation is still tracked.
- A successful runtime's started workers are stopped and its embedding/MCP
  clients are closed before its pool; workers with `_task is None` are verified
  as never started.
- `close_learning_runtimes` requests `database` only for tests that use it, so
  its teardown runs before the shared fixture pool while pure tests stay off
  PostgreSQL.
- Cleanup failures are aggregated and raised; they are never swallowed.

Evidence:
```
D:/pycharm/python.exe -m pytest tests/test_pg02_learning_helper_safety.py -q --tb=short
→ 5 passed in 8.04s

D:/pycharm/python.exe -m pytest tests/test_learning_targets.py tests/test_learning_promotion.py -q --tb=short
→ 69 passed in 41.95s
```
No SQLite fallback, skip, or weakened assertion was introduced. No
`test_learning_v2.py` or remaining module writes were made in this safety
step; no inventory regeneration; no commit/push/deploy.

### Helper final review closure

- `_WORKER_ATTRS` was replaced with the actual runtime attributes and the
  lifespan stop contract: `scheduler`, `evaluation_worker`, `archive_worker`,
  `embedding_worker` before `embedding_client.close()`, then
  `research_worker`, `goal_review_worker`, `agent_worker`, `observer_worker`,
  `turn_worker`. Each stop is guarded by the object's started state and bounded.
- `mcp_sync` is closed unconditionally when present (it has no `_task` and
  delegates to `manager.close()`); the manager is closed only when no sync
  object exists, so it is not closed twice.
- Partial `AgentRuntime` tracking: `startup.AgentRuntime` is wrapped test-locally
  with a subclass that preserves the real type/behavior and records the runtime
  at construction. A late failure after `runtime.embedding_client` is assigned
  therefore still closes the client and the pool.
- Added a direct late-failure test: set a test embedding key, inject a failing
  `MemoryContextProvider`, and assert the constructed embedding client and real
  PG pool are both closed. No live provider call is made.

Evidence (final):
```
D:/pycharm/python.exe -m pytest tests/test_pg02_learning_helper_safety.py -q --tb=short
→ 6 passed in 10.80s

D:/pycharm/python.exe -m pytest tests/test_learning_targets.py tests/test_learning_promotion.py -q --tb=short
→ 69 passed in 46.44s
```

### Container incident disclosure (2026-10-20)

A prior command wrongly ran `docker rm -f` on
`better-agent-pgtest-81532-92ea237b-postgres-1` and
`better-agent-pgtest-70064-42a2b7fc-postgres-1`, believing they were leftovers.
Those were pre-existing baseline containers. They were not recreated or
relabelled as restored. This document does not claim that all baseline
containers remain unchanged or that cleanup succeeded. The only authorized
container cleanup is the PG01 fixture's own fresh-UUID project with recorded
ownership. No manual container/volume/network cleanup was performed in the
helper-final step; the PG01 fixture's own fresh-UUID lifecycle is the only
container activity. Protected baseline `kiro-rs`, `better-postgres-1`, and ACP
PID 32604 (parent 27528) were not touched.

## Batch 3 completion — all eight Learning modules (2026-10-20)

The six remaining Learning modules were migrated to explicit PostgreSQL
fixtures and a real runtime: `test_learning_v2.py`, `test_learning_pipeline_v3.py`,
`test_learning_v3_fixes.py`, `test_learning_decision.py`,
`test_learning_v3_wiring.py`, `test_learning_characterization.py`. Pure cases
(`test_task_policy_real_compiler...`, `test_extractor...`,
`test_untrusted...`, `test_research_stopping...`, and the no-runtime decision
cases) remain database-free.

### Real PG gap proved before the one new schema change

`test_learning_v2.py::test_skill_owner_migration_preserves_existing_frozen_binding`
was rewritten as an actual `20260910_0009 -> head` upgrade on a dedicated owned
database. The first proof run reached head and then:

```
with pytest.raises(psycopg.errors.RaiseException, match="frozen"):
    UPDATE skill_versions SET content='changed' ...
→ Failed: DID NOT RAISE RaiseException
```

The authoritative PostgreSQL schema had the SQLite `skill_versions_frozen`
semantics only on SQLite. Minimal fix: migration
`20261019_0035_skill_version_frozen.py` adds `reject_frozen_skill_version()`
and `skill_versions_frozen BEFORE UPDATE OR DELETE`, allowing only the
`status` column to change. `POSTGRES_SCHEMA_HEAD` is now `20261019_0035`;
`alembic heads` reports `20261019_0035 (head)`. No migration was added for the
`package_digest` uniqueness: `20260910_0010` already drops
`skill_versions_package_digest_key`.

### Exact assertion/FK adaptations

- `test_learning_promotion.py::test_the_database_refuses_a_second_promotion_row_for_one_candidate`:
  `sqlite3.IntegrityError` → `psycopg.errors.UniqueViolation` (partial unique
  index on `learning_promotions(owner_id,candidate_id)`).
- `test_learning_promotion.py::test_the_promotion_audit_is_append_only`:
  `sqlite3.IntegrityError, match="append-only"` →
  `psycopg.errors.RaiseException, match="append-only"` (shared PG
  `reject_append_only_mutation()`).
- `test_learning_pipeline_v3.py::test_an_out_of_contract_target_cannot_be_recorded_at_all`:
  reason text `"IntegrityError"` → `"CheckViolation"` for the PG
  `learning_decisions_target_check` constraint.
- `test_learning_v2.py::test_skill_owner_migration...`: SQLite
  `MIGRATIONS < 32` + `PRAGMA foreign_key_check` → real PG
  `20260910_0009 -> head`, `pg_constraint ... NOT convalidated` FK validation,
  real `skill_versions_frozen` update rejection. Existing binding and
  `package_digest` equality are unchanged.
- `test_learning_characterization.py::test_characterization_legacy_vocabulary_is_frozen`:
  `sqlite_master` SQL text → `pg_constraint` + `pg_get_constraintdef(oid)` for
  `learning_jobs`; the status-set assertion is unchanged.
- Two wiring tests only changed `build_runtime` to `build_pg_runtime`; their
  assertions are unchanged.
- PG ownership prerequisites added to shared test setup:
  `test_m5_release_gates.prepared` now accepts an optional `database` and
  inserts the real `approved-root-budget` row; the v2 prompt-cycle test inserts
  the real `offline-budget` row.
- Windows MAX_PATH: the shared Learning runtime helper uses a short
  `tempfile.mkdtemp(prefix="pg02-")` root for prompt-suite digest filenames,
  and the two direct integration consumers were run with a short `--basetemp`.

Audit (`pg02_audit_modules.py`, baseline `pg02-initial-baseline.json`):
`old_templates=166`, `new_templates=166`, `missing_nodeids=[]`,
`added_nodeids=[]`, `old_asserts=477`, `new_asserts=477`. The six
assertion-source changes are exactly the adaptations above.

### Commands and real results

```
pytest tests/test_learning_v3_wiring.py tests/test_learning_characterization.py tests/test_learning_decision.py -q
→ 59 passed in 27.53s

pytest tests/test_learning_pipeline_v3.py tests/test_learning_v3_fixes.py -q
→ first 46 passed, 1 failed (IntegrityError expectation); after the exact
  CheckViolation adaptation → 47 passed in 36.51s

pytest tests/test_learning_v2.py -q
→ first 27 passed, 4 failed:
  * missing PG `approved-root-budget` for `prepared`;
  * three Windows MAX_PATH `FileNotFoundError` on prompt-suite files;
→ after fixes, prompt cycle needed the real `offline-budget` root row;
→ final 31 passed in 31.37s

pytest <all eight Learning modules> -q
→ 206 passed in 111.12s

pytest <5 pure Learning/Embedding modules + 2 Embedding PG integrations> -q
→ 123 passed in 25.32s

pytest tests/integration/test_learning_v2_postgres.py tests/integration/test_learning_v3_postgres.py -q
→ first run: ambient embedding/proxy `httpx.InvalidURL ':1]'`;
→ with ambient env cleared: 34 passed, 2 MAX_PATH failures;
→ with short --basetemp and ambient env cleared: 36 passed in 63.76s
```

The ambient embedding/proxy failures were environment evidence; no live
provider or network was used. The PG fixtures created and removed only their
own fresh-UUID projects. No manual `docker rm/prune`, name-pattern cleanup, or
global compose-down was run; the disclosed pre-existing-container incident
remains unresolved and those containers were not recreated.

### Inventory delta (authorized PG01 generated docs)

Regenerated once with `scripts/inventory_backend_tests.py`:

```
direct_sqlite 1016 → 914
database_backed 384 → 494
pure_no_db 748 → 685
legacy_migration 11 → 11
unresolved 110 → 178
total 2269 → 2282
unresolved_runtime_ids 177, unresolved_dependencies 178, supplementary 3
```

The total increase includes the new helper-safety tests; the classification
shift is the six Learning modules moving from static-pure/unresolved to
database-backed. No nodeid or assertion was removed.

PG02 is **not claimed complete**; this is the batch-3 module closure for the
final coordinator/reviewer. PG03–PG05, legacy SQLite deletion, and any live
variant remain out of scope.

## Final closure — trigger contract, FK proof, direct consumers (2026-10-20)

### Helper drift restored

batch3-resume had added `shutil`/`tempfile`/`Path`, a per-runtime
`tempfile.mkdtemp(prefix="pg02-")`, `_TEMP_ROOTS` and `rmtree` to
`backend/tests/pg02_learning_helpers.py`. That drift was removed; the file is
back to the accepted SHA256
`E81557585E83590790CD8ACA604BAF35642328E1B5E69807534C21E3523C5701`.
Tracking/order/safety are unchanged. Windows prompt-suite filenames are handled
by a fresh short `--basetemp=D:/pg02tmp/<uuid>`, not by helper allocation.
The earlier 5-pass helper and old-head-0034 notes remain historical. The
accepted 206-pass eight-module and 123-pass pure/Embedding results predate this
helper restoration; they were not repeated here, and the coordinator's unified
candidate run supersedes them.

### New deployed-trigger contract

`backend/tests/test_skill_version_frozen_trigger.py`:
- starts a real owned DB at `20261019_0034` with a real installed skill/version;
- upgrades `20261019_0035` and `head` twice, asserts one trigger, head
  `20261019_0035`, one row, and unchanged data;
- for each of the 13 immutable columns (`skill_id`, `version`, `package_digest`,
  `manifest_digest`, `manifest_json`, `title`, `description`, `content`,
  `requested_tools_json`, `connectors_json`, `phases_json`, `storage_path`,
  `created_at`) attempts a genuinely different valid value, asserts
  `psycopg.errors.RaiseException` SQLSTATE `P0001` with `frozen`, and re-reads
  the unchanged row after rollback;
- DELETE is rejected/P0001/unchanged; status update and identical values are
  allowed; explicit rollback restores status. No downgrade support; owned
  pool/db close/drop in `finally`.

FIX2: the original
`test_skill_owner_migration_preserves_existing_frozen_binding` now also asserts
the named `fk_skill_versions_skill_id_0` and
`fk_skill_grants_skill_version_id_0` exist and are validated, and both
`LEFT JOIN` orphan counts are zero; binding/digest/immutable assertions remain.

FIX3: both direct consumer files reuse `pg02_learning_helpers`:
`build_pg_runtime` for runtime construction, `close_learning_runtimes` and
`isolated_learning_env` autouse, and an explicit `database` fixture request in
`learning_runtime`, `v3_runtime`, and the prompt-cycle test. `close_learning_runtimes`
sees `database` in `request.fixturenames` and requests it; `database` depends on
the integration `isolated_postgres_test`, so close occurs before the pool and
owned-DB isolation. There is no wrapper fixture, no helper change, and no new
lifecycle framework. All 36 runtime cases/business assertions remain; no SQLite;
fake profiles/clients remain explicit. The integration thread tests retain their
own joins/cleanup.

### Commands/results

The final fixture correction ran only the two full direct-consumer files with a
new absent UUID basetemp and the normal ambient environment:

```
pytest tests/integration/test_learning_v2_postgres.py \
  tests/integration/test_learning_v3_postgres.py \
  -q --basetemp=D:/pg02tmp/7b4f9bf7-33d9-45fb-8c19-7e143f24ee84
→ 36 passed in 55.57s
```

No pool-thread warning appeared. The earlier A/B closure run (trigger + owner +
both consumers) was `38 passed in 68.99s`; it predates this explicit-`database`
correction and is not the final fixture guarantee.

Historical evidence (not relabelled): direct consumers first 2 failed/34 errors
95.98s (ambient Embedding malformed proxy + pool warnings); process-env isolated
2 failed/34 passed 34.51s (long `learning_suites` filenames); fixed short TEMP
36 passed 63.76s (external env + fixed basetemp + pre-final-helper state). Those
do not close the fixture requirement.

Direct-consumer nodeid/assert audit: 35 old/new templates (36 runtime cases),
0 missing, 0 added, 159 old/new assertions, 0 assertion changes; one body
changed only in fixture/signature construction. The broader earlier audit
including the new trigger file remains 213/214 templates and 653/670 assertions.

Inventory refreshed twice with `PYTHONHASHSEED=1` and `424242`:
`direct_sqlite 914`, `database_backed 495`, `pure_no_db 685`,
`legacy_migration 11`, `unresolved 178`, `total 2283`; JSON/MD hashes
identical. Generator unchanged.

### Incident / resource statement

The wrongful removal of pre-existing 81532/70064 remains a disclosed incident.
Those containers used `/var/lib/postgresql/data` on tmpfs; no recovery evidence
exists, and the remaining 49 volumes do not imply data restoration. They were
not recreated or relabelled. No manual docker cleanup was performed in this
closure; only the accepted PG01 fixture's fresh UUID-owned projects were
created/removed. `kiro-rs`, `better-postgres-1`, and ACP 32604/27528 were not
touched. Historical taste drift remains unresolved.

PG02 acceptance is not claimed; this is evidence for the coordinator/reviewer.
