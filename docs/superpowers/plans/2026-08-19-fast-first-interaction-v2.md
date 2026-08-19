# Fast First Interaction V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a durable Conversation Runtime so ordinary questions stream useful Markdown directly, explicit execution requests wait for task-specific confirmation, and only confirmed work enters the existing Agent Runtime.

**Architecture:** Keep the existing Goal/Run/Plan/Approval/ReAct domain intact and add Thread → Turn → TurnJob tables beside it. A ConversationService accepts turns transactionally, a RouteAndRespondModel emits one control-header-plus-Markdown response, and a single-process ManagedTurnWorker owns leases, streaming batches, cancellation, and restart recovery. ExecutionMaterializer creates the existing Goal/Session/Run only after a version-checked continue_execution action. Thread SSE is the canonical conversation state source; Run SSE remains the trajectory source after materialization.

**Tech Stack:** FastAPI, SQLite WAL, Python asyncio with a persisted job ledger, existing ModelGateway, React + TypeScript, Vitest, pytest, native SSE.

---

## Scope guard and invariants

- Preserve the existing V1 Agent Runtime and /api/runs/* APIs for already-materialized runs.
- Add no RAG, multi-agent execution, MCP, Shell, Redis, Celery, microservices, message queues, or parallel tool calls.
- Do not add a general “转为目标” or “仅保存方案” action.
- answer and clarify never create rows in goals, sessions, runs, or plan_versions.
- propose_execution has no tools and no side effects before a successful, idempotent direction action.
- Persist state changes, messages, and their corresponding semantic event in one SQLite transaction.
- Never persist or display the route control header, internal prompt, or raw structured JSON as the user-facing assistant message.
- Keep data/, memory/, evals/, graphify-out/, local .env files, and model keys out of commits.

## Files and responsibilities

| Area | Files | Responsibility |
|---|---|---|
| Persistence | backend/app/db.py | Add idempotent Thread/Turn/Job/ThreadMessage/ThreadEvent schema and migration columns. |
| Conversation domain | backend/app/conversation.py | Turn snapshots, transactional acceptance, semantic event store, control-head decoder, route model protocol, materializer, managed worker. |
| Model adapter | backend/app/live_model.py, backend/app/startup.py | Add one-call conversation adapter with no tool schema and no JSON-repair call. |
| Agent atomicity | backend/app/events.py, backend/app/runtime.py | Allow existing Run events in a caller transaction and expose the V1 initial budget/source-turn insertion needed by materialization. |
| HTTP | backend/app/api.py, backend/app/main.py | Add Thread/Turn REST/SSE, 202 Accepted, cancellation, direction selection, and worker lifecycle. |
| Backend tests | backend/tests/test_conversation.py, test_conversation_worker.py, test_conversation_api.py, test_materializer.py | Prove idempotency, atomicity, routing, streaming, recovery, cancellation, and permission boundaries. |
| Frontend state/UI | frontend/src/types.ts, api.ts, hooks/useThreadTelemetry.ts, pages/ChatPage.tsx, components/ConversationThread.tsx, App.tsx | Make Thread/Turn state canonical before Run materialization and keep existing trajectory controls after handoff. |
| Frontend tests | frontend/src/__tests__/threadTelemetry.test.ts, ChatPage.test.tsx, ConversationWorkspace.test.tsx | Prove optimistic visibility, policy buttons, canonical reconciliation, gap recovery, and no raw header. |

### Task 1: Add durable Conversation schema and transactional event primitives

**Files:**
- Modify: backend/app/db.py
- Modify: backend/app/events.py
- Create: backend/tests/test_conversation.py

- [ ] Step 1: Write failing schema and sequence tests.

Create a temporary Database, assert threads, turns, turn_jobs, thread_messages, and thread_events plus runs.source_turn_id exist. Insert a Thread and append two events through the caller transaction; assert seq 1 and 2. Raise inside a transaction after inserting a Turn/message/event and assert the rollback leaves all three absent.

~~~python
def test_conversation_schema_and_thread_seq_are_durable(tmp_path):
    db = Database(tmp_path / "agent.db")
    with db.connection() as connection:
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"threads", "turns", "turn_jobs", "thread_messages", "thread_events"} <= tables
        assert "source_turn_id" in {
            row["name"] for row in connection.execute("PRAGMA table_info(runs)")
        }
~~~

- [ ] Step 2: Run RED.

Run from D:\RAG\better\backend:

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_conversation.py -q
~~~

Expected: FAIL because the V2 tables and source_turn_id do not exist.

- [ ] Step 3: Implement the minimum migration and event store.

Extend SCHEMA with the five tables and append source_turn_id using the existing PRAGMA migration pattern. Use thread_id + seq as a unique key and atomically increment threads.next_event_seq inside BEGIN IMMEDIATE. Add ThreadEvent and ThreadEventStore.append(connection=...). Keep EventStore.append() compatible when no connection is supplied.

- [ ] Step 4: Run focused and legacy persistence tests.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_conversation.py tests/test_db.py tests/test_events.py -q
~~~

Expected: PASS with all existing persistence tests green.

- [ ] Step 5: Commit.

~~~powershell
git add backend/app/db.py backend/app/events.py backend/tests/test_conversation.py
git commit -m "feat: add durable conversation tables and thread events"
~~~

### Task 2: Implement and test the one-call route-and-respond protocol

**Files:**
- Create: backend/app/conversation.py
- Modify: backend/app/live_model.py
- Create: backend/tests/test_conversation.py
- Modify: backend/tests/test_live_model.py

- [ ] Step 1: Write failing decoder/model tests.

Cover control headers split at arbitrary boundaries, a header plus first Markdown chunk in one delta, overlong/unknown/invalid headers, and a route model request with tools=[]. Assert the decoder returns only body text and invalid structure does not cause a repair call.

~~~python
def test_control_head_releases_only_markdown_after_valid_json_line():
    decoder = ControlHeadDecoder(max_header_bytes=1024)
    assert decoder.feed('{"v":1,"policy":"answer","content_shape":"guide",') == ""
    assert decoder.feed('"reason_code":"content_only"}\n## 桂林') == "## 桂林"
    assert decoder.header == RouteDecision("answer", "guide", "content_only")

def test_control_head_rejects_unknown_policy():
    decoder = ControlHeadDecoder()
    with pytest.raises(RouteProtocolError):
        decoder.feed('{"v":1,"policy":"goal"}\n')
~~~

- [ ] Step 2: Run RED.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_conversation.py tests/test_live_model.py -q
~~~

Expected: FAIL because the decoder and conversation model adapter do not exist.

- [ ] Step 3: Implement the restricted protocol.

Create RouteDecision, RouteResponse, RouteProtocolError, ControlHeadDecoder, and the RouteAndRespondModel protocol. Add LiveConversationModel: one ModelGateway.complete() call, no tool schema, forwarded cancellation/delta/reset callbacks, and no second JSON-repair call. Keep LiveRuntimeModel._json() unchanged for V1 structured calls.

- [ ] Step 4: Run model tests and commit.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_conversation.py tests/test_live_model.py tests/test_model_gateway.py -q
git add backend/app/conversation.py backend/app/live_model.py backend/tests/test_conversation.py backend/tests/test_live_model.py
git commit -m "feat: add single-call conversation routing protocol"
~~~

Expected: PASS; legacy structured tests remain green and the conversation request contains no tools.

### Task 3: Accept Turns with 202 Accepted and idempotency

**Files:**
- Modify: backend/app/conversation.py
- Modify: backend/app/runtime.py
- Modify: backend/app/api.py
- Create: backend/tests/test_conversation_api.py

- [ ] Step 1: Write failing API tests.

Create a Thread, submit a Turn with a blocking model, assert the response is 202 before the model future completes, the user message and queued job already exist, and repeating the same client_turn_id returns the same turn_id with one job/message. Assert goals/runs remain empty.

~~~python
def test_turn_submission_is_durable_and_idempotent_before_model_finishes(tmp_path):
    runtime = make_runtime(tmp_path, BlockingConversationModel())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", headers=_headers(app), json={"title": "Chat"})
    payload = {"client_turn_id": "client-1", "content": "创建桂林 7 天攻略", "skill_names": []}
    first = client.post(f"/api/threads/{thread.json()['id']}/turns",
                        headers=_headers(app), json=payload)
    second = client.post(f"/api/threads/{thread.json()['id']}/turns",
                         headers=_headers(app), json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json()["turn_id"] == second.json()["turn_id"]
    assert count_rows(runtime.db, "goals") == 0
~~~

- [ ] Step 2: Run RED.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_conversation_api.py -q
~~~

Expected: FAIL because Thread routes and ConversationService.accept_turn() are missing.

- [ ] Step 3: Implement acceptance.

Implement create_thread(), get_thread(), accept_turn(), and list_thread_messages(). In one transaction insert/reuse Thread, Turn, user thread_messages row, turn_jobs row, update Thread version/active turn, and append turn.accepted. Use UNIQUE(thread_id, client_turn_id), and do not run a model from the HTTP handler. Construct AgentRuntime.conversation with an injectable route model and a deterministic fallback for legacy V1 test doubles.

- [ ] Step 4: Add routes and verify.

Add POST /api/threads, GET /api/threads/{thread_id}, POST /api/threads/{thread_id}/turns with spec fields. Validate content, client_turn_id, skill_names, and CSRF.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_conversation_api.py tests/test_api.py -q
~~~

Expected: PASS; legacy Goal message routes remain compatible.

- [ ] Step 5: Commit.

~~~powershell
git add backend/app/conversation.py backend/app/runtime.py backend/app/api.py backend/tests/test_conversation_api.py
git commit -m "feat: accept conversation turns asynchronously"
~~~

### Task 4: Build the managed single-process Turn Worker

**Files:**
- Modify: backend/app/conversation.py
- Modify: backend/app/main.py
- Create: backend/tests/test_conversation_worker.py

- [ ] Step 1: Write failing Worker tests.

Cover queued claim/lease attempts, expired-lease recovery, partial-generation interruption/new generation, message.started/delta/completed, answer completion, propose_execution waiting, safe model failure, persistent cancellation, and SSE disconnect not cancelling a job.

~~~python
@pytest.mark.asyncio
async def test_worker_streams_markdown_and_finishes_answer_without_agent_rows(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedConversationModel(
        '{"v":1,"policy":"answer","content_shape":"guide","reason_code":"content_only"}\n'
        '## 桂林\n第一天…'
    ))
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-1", "给我攻略", [])
    await runtime.turn_worker.run_once()
    message = runtime.conversation.messages(thread.id)[1]
    assert message.content.startswith("## 桂林")
    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert count_rows(runtime.db, "goals") == 0
~~~

- [ ] Step 2: Run RED.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_conversation_worker.py -q
~~~

Expected: FAIL because no lease/worker/stream persistence exists.

- [ ] Step 3: Implement claim, lease, batching, and recovery.

Implement claim_next(), run_once(), start(), and stop() with one owned asyncio task and a poll loop. Claim QUEUED or expired RUNNING jobs in BEGIN IMMEDIATE, increment attempts, set lease_owner/lease_until, and emit turn.started. If a prior streaming message exists, mark its generation interrupted and start the next generation. Use an asyncio queue around the model callback so model generation and persistence run concurrently; flush every 25–50 ms or 64–256 Unicode characters. Each flush updates message content and appends message.delta in the same transaction with generation and exact offset.

- [ ] Step 4: Implement terminal states and cancellation.

answer/clarify finish the message and Turn as COMPLETED; propose_execution finishes the message, sets AWAITING_DIRECTION, and appends turn.awaiting_direction. Persist /cancel as cancel_requested_at, set an in-memory event for the active model call, finish with finish_reason=cancelled, and append turn.cancel_requested/turn.cancelled. SSE disconnect must not call this path. Keep internal errors in last_error_json and use a fixed safe user message.

- [ ] Step 5: Add lifespan startup/recovery.

Start the worker in create_app lifespan when a configured runtime exists and stop it at shutdown. It discovers queued and expired jobs from SQLite after restart; no request-owned detached task is required for correctness.

- [ ] Step 6: Run Worker and legacy tests.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_conversation_worker.py tests/test_restart_recovery.py tests/test_runtime.py -q
~~~

Expected: PASS.

- [ ] Step 7: Commit.

~~~powershell
git add backend/app/conversation.py backend/app/main.py backend/tests/test_conversation_worker.py
git commit -m "feat: add recoverable conversation turn worker"
~~~

### Task 5: Add explicit direction selection and ExecutionMaterializer

**Files:**
- Modify: backend/app/conversation.py
- Modify: backend/app/runtime.py
- Modify: backend/app/events.py
- Modify: backend/app/api.py
- Create: backend/tests/test_materializer.py

- [ ] Step 1: Write failing permission/idempotency tests.

Use a scripted propose_execution model. Assert no Goal/Run before confirmation; answer/clarify reject continue_execution; version mismatch returns conflict; modify_plan completes without materialization; repeating one idempotency key creates exactly one Goal/Session/Run; source_turn_id and execution.materialized are recorded.

~~~python
@pytest.mark.asyncio
async def test_execution_materializes_once_only_after_confirmed_direction(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedConversationModel(
        '{"v":1,"policy":"propose_execution","content_shape":"tracking",'
        '"reason_code":"external_effect"}\n我可以按确认后的攻略更新清单。'
    ))
    turn = submit_and_run_once(runtime, "按攻略更新清单")
    assert count_rows(runtime.db, "goals") == 0
    first = await runtime.conversation.select_direction(
        turn.turn_id, "continue_execution", turn.version, "action-1"
    )
    second = await runtime.conversation.select_direction(
        turn.turn_id, "continue_execution", first.version, "action-1"
    )
    assert first.materialized_run_id == second.materialized_run_id
    assert count_rows(runtime.db, "goals") == 1
    with runtime.db.connection() as connection:
        row = connection.execute(
            "SELECT source_turn_id FROM runs WHERE id = ?",
            (first.materialized_run_id,),
        ).fetchone()
    assert row["source_turn_id"] == turn.turn_id
~~~

- [ ] Step 2: Run RED.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_materializer.py -q
~~~

Expected: FAIL because direction selection/materialization is not implemented.

- [ ] Step 3: Implement direction selection.

Add select_direction(turn_id, action, expected_version, idempotency_key). Validate AWAITING_DIRECTION, policy propose_execution, supported action, version, and unique key. modify_plan records turn.direction_selected and completes the Turn without Agent rows. continue_execution enters MATERIALIZING and calls ExecutionMaterializer.

- [ ] Step 4: Implement one-transaction materialization.

Refactor V1 initial budget creation into AgentRuntime.initial_budget(). In one database transaction insert or reuse goals/sessions/runs with source_turn_id, update Turn direction/materialized IDs/final status, append Thread turn.direction_selected and execution.materialized, and append Run run.created through the same connection. A duplicate key returns stored IDs. After commit invoke existing AgentRuntime.handle_message() with the original Turn content so normal planning/approval begins; no tools run before the existing approval boundary.

- [ ] Step 5: Add direction endpoint and verify.

Add POST /api/turns/{turn_id}/direction with action, expected_version, and idempotency_key. Return Turn state, materialized IDs, and public Run snapshot when present; map invalid action/version/policy to 409.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_materializer.py tests/test_security.py tests/test_approval.py tests/test_checkpoint.py -q
D:\pycharm\python.exe -m pytest -q
~~~

Expected: PASS; no pre-confirmation tool calls.

- [ ] Step 6: Commit.

~~~powershell
git add backend/app/conversation.py backend/app/runtime.py backend/app/events.py backend/app/api.py backend/tests/test_materializer.py
git commit -m "feat: materialize agent runs only after direction confirmation"
~~~

### Task 6: Expose Thread messages/events/SSE with resumable semantics

**Files:**
- Modify: backend/app/api.py
- Modify: backend/app/conversation.py
- Create: backend/tests/test_conversation_api.py

- [ ] Step 1: Write failing REST/SSE tests.

Test Thread messages/events reads and stream with Last-Event-ID. Verify no duplicate IDs, seq starts at 1 and remains contiguous, reconnect returns only missing message.delta, message.snapshot is available for generation/offset gaps, and /cancel is explicit and separate from SSE disconnect.

- [ ] Step 2: Run RED.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_conversation_api.py -q
~~~

Expected: FAIL because Thread read/stream endpoints are missing.

- [ ] Step 3: Implement REST and SSE.

Add GET /api/threads/{id}/messages, GET /api/threads/{id}/events, GET /api/threads/{id}/events/stream, POST /api/turns/{id}/cancel, and the Thread read endpoint. Encode id: seq, event: conversation, JSON data, and keep-alive comments. Use Last-Event-ID without cancelling jobs on disconnect; append-only storage plus client sequence deduplication makes reconnect safe.

- [ ] Step 4: Verify and commit.

~~~powershell
D:\pycharm\python.exe -m pytest tests/test_conversation_api.py tests/test_sse.py tests/test_api.py -q
git add backend/app/api.py backend/app/conversation.py backend/tests/test_conversation_api.py
git commit -m "feat: add resumable conversation REST and SSE APIs"
~~~

Expected: PASS with old Run SSE and new Thread SSE.

### Task 7: Make React use Thread/Turn state before Run telemetry

**Files:**
- Modify: frontend/src/types.ts
- Modify: frontend/src/api.ts
- Create: frontend/src/hooks/useThreadTelemetry.ts
- Modify: frontend/src/App.tsx
- Modify: frontend/src/pages/ChatPage.tsx
- Modify: frontend/src/components/ConversationThread.tsx
- Create: frontend/src/__tests__/threadTelemetry.test.ts
- Modify: frontend/src/__tests__/ChatPage.test.tsx
- Modify: frontend/src/__tests__/ConversationWorkspace.test.tsx

- [ ] Step 1: Write failing frontend state tests.

Cover local user visibility before POST resolves, submitted → streaming → ready/error/cancelled, exact generation/offset append, gap-triggered snapshot refresh, answer hiding all direction buttons, propose_execution showing only Continue/Modify, and no control JSON in rendered Markdown.

~~~typescript
test("does not append a delta when the offset has a gap", () => {
  const current = [{ id: "m1", generation: 1, content: "abc" }];
  const event = threadEvent("message.delta", {
    message_id: "m1", generation: 1, offset: 5, delta: "x",
  });
  expect(applyThreadEvent(current, event)).toEqual(current);
  expect(needsMessageSnapshot(current, event)).toBe(true);
});
~~~

- [ ] Step 2: Run RED.

~~~powershell
Set-Location D:\RAG\better\frontend
npm.cmd test -- --run src/__tests__/threadTelemetry.test.ts src/__tests__/ChatPage.test.tsx
~~~

Expected: FAIL because Thread API/state helpers do not exist.

- [ ] Step 3: Add Thread/Turn types and API functions.

Define TurnStatus, Turn, Thread, ThreadMessage, and ThreadEvent. Add createThread, submitTurn, getThread, getThreadMessages, getThreadEvents, cancelTurn, selectDirection, and subscribeToThreadEvents. Preserve Run API functions.

- [ ] Step 4: Implement canonical Thread telemetry.

Create pure appendThreadEvent, applyThreadEvent, needsMessageSnapshot, and hydrateThreadMessages helpers. Append only when generation matches and offset equals current content length; on a gap or generation change fetch /messages and replace the affected snapshot. Reconcile the optimistic user message by client_turn_id/canonical message ID instead of adding a second row. Derive active Turn policy/status from Thread events and refresh Thread after materialization.

- [ ] Step 5: Refactor ChatPage and ConversationThread.

On the first message create a Thread and immediately render the local user message, then submit a Turn and disable input only while that Turn is non-terminal. Do not create a Goal in the conversation path. Subscribe to Thread SSE; when execution.materialized arrives, fetch Run and switch the activity/trajectory data to existing Run telemetry. Keep V1 plan/approval/Run cancel controls after handoff and use Turn cancel before handoff. Render one assistant status slot. answer/clarify show no generic upgrade controls. propose_execution shows only “继续执行” and “修改方案”; modify_plan completes the old Turn and focuses the composer. Keep the installed Skill selector and readable Markdown renderer.

- [ ] Step 6: Run frontend tests/build and commit.

~~~powershell
npm.cmd test -- --run
npm.cmd run build
git add frontend/src
git commit -m "feat: drive chat from durable thread turns"
~~~

Expected: PASS with trajectory, plan, memory, and skill UI tests preserved.

### Task 8: Deterministic acceptance, startup verification, and delivery

**Files:**
- Create: backend/tests/test_v2_acceptance.py
- Create: frontend/src/__tests__/v2Acceptance.test.tsx
- Modify: README.md only if startup/API instructions change

- [ ] Step 1: Add deterministic route cases.

Cover ordinary knowledge, Guilin 7-day guide, six-week learning plan, explicit reminders, explicit local-note WRITE, vague request, prompt injection, invalid header, cancel during stream, duplicate submit, reconnect, and direction conflict. Assert policy, visible Markdown, zero direct-run leaks, zero pre-confirmation tool calls, and exactly one Run after confirmation.

- [ ] Step 2: Run all verification commands.

From D:\RAG\better\backend:

~~~powershell
D:\pycharm\python.exe -m pytest -q
~~~

From D:\RAG\better\frontend:

~~~powershell
npm.cmd test -- --run
npm.cmd run build
~~~

From D:\RAG\better:

~~~powershell
git diff --check
git status --short --branch
~~~

Expected: backend/frontend suites pass, build succeeds, diff check is clean, and only source/tests/docs are modified.

- [ ] Step 3: Verify startup without exposing secrets.

Start the existing local command using configured LLM_AP_PATH/environment, request /api/health and /api/bootstrap, create a Thread, submit a deterministic Turn when available, and verify 202, Thread events, cancellation, and the built frontend. Do not print .env, LLM_AP.txt, data/, memory/, or provider responses containing secrets.

- [ ] Step 4: Commit final tests/docs after verification.

~~~powershell
git add backend/tests/test_v2_acceptance.py frontend/src/__tests__/v2Acceptance.test.tsx README.md docs/superpowers/plans/2026-08-19-fast-first-interaction-v2.md
git commit -m "test: verify fast first interaction v2"
~~~

- [ ] Step 5: Report implemented endpoints, persistence/event semantics, test evidence, branch/commits, startup instructions, and any incomplete specification item. Never describe a missing recovery, snapshot, or evaluation path as complete.

## Plan self-review

- Sections 1–8 are covered by Tasks 2–5 and the scope guard.
- Schema/state sections 9–10 are covered by Tasks 1 and 4–5.
- Control protocol section 11 is covered by Task 2.
- API/event sections 12–13 are covered by Tasks 3 and 6.
- Atomicity, leases, recovery, cancellation, and disconnect sections 14–15 are covered by Tasks 1 and 4.
- Frontend source-of-truth and decision UI section 16 are covered by Task 7.
- Acceptance, metrics, examples, and explicit non-goals in sections 17–24 are covered by Task 8 and the scope guard.
- No TBD/TODO placeholder or unowned architecture gap is left in this plan.

