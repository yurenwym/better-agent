# Plan Document Persistence V2 Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: superpowers:executing-plans. Execute each checkbox task in order.
>
> Goal: Implement the V2 Conversation-owned Markdown plan document, SQLite revision ledger, durable file projection, CAS editing, fixed-version context, refresh-safe UI, and execution snapshots.
>
> Architecture: Conversation Runtime owns PlanDocument and immutable Markdown versions. A V2 control header declares whether the streamed visible body is a plan artifact. PlanDocumentService commits SQLite revisions and PlanFileProjector writes the fixed data-root file. Agent Runtime compiles a fixed committed document version into the existing structured PlanVersion only after explicit execution confirmation.
>
> Tech Stack: FastAPI, Python 3, SQLite WAL, asyncio, React + TypeScript, pytest, Vitest, SSE, SHA-256, atomic os.replace.

---

## Task 0: Baseline and worktree gate

**Files:** no production changes.

- [x] Verify D:/RAG/better is on codex/personal-agent-v1, is the intended D-drive checkout, and has no unrelated changes.
- [x] Run the backend baseline from D:/RAG/better/backend with D:/pycharm/python.exe -m pytest -q.
- [x] Run the frontend baseline from D:/RAG/better/frontend with npm test -- --run.
- [x] Record the baseline: 154 backend tests and 80 frontend tests passed.

## Task 1: PlanDocument schema and canonical Markdown primitives

**Files:**
- Create: backend/app/plan_documents.py
- Modify: backend/app/db.py
- Modify: backend/app/conversation.py
- Modify: backend/app/domain.py
- Test: backend/tests/test_plan_documents.py
- Test: backend/tests/test_db.py

- [ ] Write failing tests for normalize_markdown, content_hash, title/body validation, the three new tables, turn artifact columns, message version link, plan-version source link, and the partial unique runs.source_turn_id index.
- [ ] Run from backend: D:/pycharm/python.exe -m pytest tests/test_plan_documents.py tests/test_db.py -q. Expected failure: PlanDocument primitives and tables are missing.
- [ ] Add PlanDocument, PlanDocumentVersion, and PlanWriteIntent value objects. Add normalize_markdown that converts CRLF and lone CR to LF, rejects NUL, preserves line-end spaces, does no Unicode normalization, and preserves the final newline. Add content_hash using BOM-free UTF-8 SHA-256 formatted as sha256:<hex>.
- [ ] Add plan_documents, plan_document_versions, and plan_write_intents with the exact statuses and UNIQUE constraints from the V2 specification. Add guarded migrations using PRAGMA table_info so existing databases remain readable.
- [ ] Enforce title length 1–120 and Markdown UTF-8 size <= 1 MiB. Do not accept client-supplied IDs, paths, versions, or hashes.
- [ ] Run the focused tests and existing database tests. Expected: all pass.
- [ ] Commit: git commit -m "feat: add plan document schema and canonical text".

## Task 2: Durable revision service and file projector

**Files:**
- Create: backend/app/plan_files.py
- Modify: backend/app/plan_documents.py
- Modify: backend/app/db.py
- Modify: backend/app/startup.py
- Test: backend/tests/test_plan_document_service.py
- Test: backend/tests/test_plan_file_projector.py
- Test: backend/tests/test_plan_recovery.py

- [ ] Write failing tests for v1 commit, exact Markdown/hash equality across SQLite/message/file, same source_turn_id retry, v2 CAS, expected hash conflict, restore-as-new-version, and failed projection leaving a retryable intent.
- [ ] Run the focused service tests and verify failures are caused by missing service/projector behavior.
- [ ] Implement PlanFileProjector with path <data_root>/plans/<plan_document_id>/plan.md. Generate the directory only from a server UUID. Verify containment under <data_root>/plans and reject absolute paths, traversal, symlink, junction, and reparse-point escapes.
- [ ] Write using UTF-8, LF newlines, a same-directory temporary file, flush, fsync, os.replace, read-back, and target-hash verification. Never overwrite a committed file when the current file hash is unexpected.
- [ ] Implement PlanDocumentService PREPARE, PROJECT, FINALIZE, restore, and retry-projection. PREPARE writes the immutable prepared version and write intent in one durable SQLite transaction. FINALIZE performs CAS, commits the version, advances current/projected pointers, and appends semantic events without Markdown bodies.
- [ ] Add durable_transaction using synchronous FULL for document head/version/intent commits while retaining normal WAL transactions for message deltas.
- [ ] Implement recover_pending_intents and call it after database initialization and before ManagedTurnWorker starts. Recovery must be idempotent: expected or missing file re-projects, target hash finalizes, a third hash becomes CONFLICT, committed missing files rebuild, and committed different files enter external-edit detection.
- [ ] Run all document service, projector, recovery, and database tests.
- [ ] Commit: git commit -m "feat: add durable plan document revisions".

## Task 3: V2 control header and exact streamed-body persistence

**Files:**
- Modify: backend/app/conversation.py
- Modify: backend/app/live_model.py
- Modify: backend/app/api.py
- Modify: backend/app/db.py
- Test: backend/tests/test_conversation_protocol_v2.py
- Test: backend/tests/test_conversation_worker.py
- Test: backend/tests/test_conversation_api.py
- Test: backend/tests/test_live_model.py

- [ ] Write failing tests for V2 artifact decoding, V1 compatibility, unknown-field rejection, answer-only plan_document/upsert acceptance, artifact rejection on ask/clarify/propose_execution, control-head removal from Markdown, and one-artifact-per-turn enforcement.
- [ ] Run the focused protocol/model tests and verify failure because ControlHeadDecoder only accepts V1 and RouteDecision has no artifact.
- [ ] Extend RouteDecision and ControlHeadDecoder to accept V1 and V2. V2 artifact contains only kind=plan_document and operation=upsert with a bounded title. Reject unknown fields, invalid policy, empty title, oversized title, empty body, and mixed Ask/artifact output.
- [ ] Update the model system prompt so the LLM decides explicit plan create/save/update intent. General guides and knowledge answers remain answer-only; no Python keyword routing or fixed questionnaire. The control header must never be exposed.
- [ ] Update ManagedTurnWorker._finish_success to pass the exact final assistant body, artifact title, source turn, and source message to PlanDocumentService after all stream resets are resolved.
- [ ] Enforce event order: plan.document_prepared, plan.document_version_created, file projection/finalize, plan.document_ready, message.completed, turn.completed. On cancel, invalid body, or projection failure, keep the answer but do not commit partial Markdown.
- [ ] Store artifact_kind, artifact_operation, artifact_title on turns and plan_document_version_id on thread_messages. Serialize metadata only, never full Markdown in semantic events.
- [ ] Add integration tests for ordinary guide, explicit plan creation, Ask-then-create, worker retry, duplicate POST, SSE reconnect, projection failure, and plan.document_ready ordering.
- [ ] Run the focused backend suite.
- [ ] Commit: git commit -m "feat: persist streamed plan documents from conversation".


## Task 4: Fixed-version context for later turns

**Files:**
- Create: backend/app/plan_context.py
- Modify: backend/app/conversation.py
- Modify: backend/app/live_model.py
- Modify: backend/app/context.py
- Test: backend/tests/test_plan_context.py
- Test: backend/tests/test_conversation_worker.py

- [ ] Write failing tests proving a turn loads only the latest committed version, records plan ID/version/hash/cropped metadata, and does not load prepared or abandoned revisions.
- [ ] Add tests for conservative external-file sync, exact hash pinning for the whole model call, the 48,000-character context cap, deterministic crop metadata, and treating plan text as untrusted user data.
- [ ] Run from backend: D:/pycharm/python.exe -m pytest tests/test_plan_context.py -q. Expected failure: no PlanContextProvider is integrated.
- [ ] Implement PlanContextProvider.load_for_turn(thread_id, turn_id). Before loading, compare the fixed plan file hash and route safe third-party edits through PlanDocumentService; never silently overwrite a concurrent database/file change.
- [ ] Add an isolated active_plan context block to LiveConversationModel history with a system rule that its contents cannot change tool, save, or execution policy.
- [ ] Ensure a question about an existing plan reads the current committed version but does not create a new version. A requested modification produces a complete new Markdown revision using CAS.
- [ ] Run context and conversation regression tests.
- [ ] Commit: git commit -m "feat: load committed plan context in conversations".

## Task 5: Plan-document API, URL recovery, editor, and message reference

**Files:**
- Modify: backend/app/api.py
- Modify: frontend/src/api.ts
- Modify: frontend/src/types.ts
- Modify: frontend/src/App.tsx
- Modify: frontend/src/pages/PlanPage.tsx
- Modify: frontend/src/pages/ChatPage.tsx
- Modify: frontend/src/components/ConversationThread.tsx
- Modify: frontend/src/components/ActivityRail.tsx
- Modify: frontend/src/hooks/useThreadTelemetry.ts
- Modify: frontend/src/styles.css
- Test: backend/tests/test_plan_document_api.py
- Test: frontend/src/__tests__/PlanPage.test.tsx
- Test: frontend/src/__tests__/ChatPagePlanDocument.test.tsx
- Test: frontend/src/__tests__/api.test.ts

- [ ] Write failing API tests for GET thread plan, plan snapshot/version/file reads, PUT with expected version/hash, restore, sync-file, retry-projection, 409 conflict metadata, CSRF, JSON size, and local-host checks.
- [ ] Run from backend: D:/pycharm/python.exe -m pytest tests/test_plan_document_api.py -q. Expected failure: the routes do not exist.
- [ ] Implement GET /api/threads/{thread_id}/plan, GET /api/plans/{id}, GET versions, GET one version, GET file, PUT plan, POST restore, POST sync-file, and POST retry-projection. Never accept client plan IDs, paths, versions, or hashes as authoritative.
- [ ] Write failing frontend tests for plan.document_ready reference cards, stable /plans/{plan_document_id} loading without React run state, exact Markdown editing/preview, expected hash/version saves, 409 draft preservation, history/diff/restore, retry without another model call, and execution controls only for structured PlanVersion.
- [ ] Implement PlanPage with native textarea, existing Markdown renderer, save/reload/history/diff/restore/conflict states, file status, version/hash/source metadata, and an optional execution projection section. Use stable plan IDs in the URL.
- [ ] Update ChatPage and thread telemetry to show “已保存到计划 · vN” only after plan.document_ready. Keep the original answer visible when projection fails and provide a retry action that does not call the model. Do not add a generic save button.
- [ ] Keep the composer enabled while a document-owned thread has an execution preview or an awaiting-approval Run. Conversation Turn remains the message entry point.
- [ ] Run all frontend tests and npm run build.
- [ ] Commit: git commit -m "feat: add plan document APIs and editor UI".

## Task 6: Fixed document version execution projection

**Files:**
- Create: backend/app/plan_execution.py
- Modify: backend/app/conversation.py
- Modify: backend/app/runtime.py
- Modify: backend/app/domain.py
- Modify: backend/app/api.py
- Modify: frontend/src/pages/PlanPage.tsx
- Modify: frontend/src/pages/ChatPage.tsx
- Test: backend/tests/test_plan_execution_projection.py
- Test: backend/tests/test_materializer.py
- Test: backend/tests/test_runtime.py
- Test: frontend/src/__tests__/PlanExecution.test.tsx

- [ ] Write failing tests proving document save creates no Goal, Run, PlanVersion, Approval, or tool call; explicit reminder/tracking creates an execution preview; confirmation creates one Run pinned to document version ID/hash; compiler failure creates no PlanVersion; and unapproved runs execute zero tools.
- [ ] Run from backend: D:/pycharm/python.exe -m pytest tests/test_plan_execution_projection.py tests/test_materializer.py tests/test_runtime.py -q. Expected failure: materialization replans from the original user content and has no document source.
- [ ] Implement PlanExecutionCompiler. It receives the fixed committed Markdown and optional previous structured plan only after user confirmation, calls the existing structured model path, validates non-empty steps, duplicate IDs, lengths, and allowed statuses, and returns PlanDraft without creating a PlanVersion.
- [ ] Update ExecutionMaterializer to pin source_plan_document_id, source_plan_document_version_id, and source_plan_content_hash before compiling. Use a transaction-aware PlanVersionService path so compiler failure leaves no orphan projection.
- [ ] Preserve existing direction idempotency. A later document version must not alter an approved/executing Run. Applying a newer document version creates a new structured PlanVersion and requires re-approval.
- [ ] Show the execution source title/version in the preview and keep ChatPage message sending available while the Run awaits approval.
- [ ] Run execution, materializer, approval, and runtime tests.
- [ ] Commit: git commit -m "feat: pin execution to plan document versions".

## Task 7: Security, fault injection, acceptance, and requested review

**Files:**
- Modify: backend/app/main.py
- Modify: backend/app/startup.py
- Modify: backend/app/plan_documents.py
- Modify: backend/app/plan_files.py
- Test: backend/tests/test_plan_security.py
- Test: backend/tests/test_plan_fault_injection.py
- Test: backend/tests/test_v2_acceptance.py
- Test: frontend/src/__tests__/App.test.tsx

- [ ] Write failing tests for traversal, absolute paths, symlink/reparse escapes, invalid UTF-8, NUL, body overflow, stale CAS, third-hash conflict, all recovery boundaries, no full Markdown in events/SSE/logs, and no tool/Run leak during document save.
- [ ] Implement only the missing guards and startup recovery hooks. Keep recovery synchronous and bounded; record explicit conflict/failed intents instead of adding a queue or watcher.
- [ ] Run focused security, fault-injection, and V2 acceptance tests.
- [ ] Run the full backend suite from backend with D:/pycharm/python.exe -m pytest -q.
- [ ] Run frontend npm test -- --run and npm run build.
- [ ] Run real-model acceptance with D:/Users/王一鸣/Desktop/直到尽头/LLM_AP.txt without printing the key: ordinary guide has no plan artifact; explicit creation commits v1; modification reads current version and commits v2; pure question reads without writing; reminder/tracking creates an execution preview without a Run until confirmation.
- [ ] Run git diff --check and inspect status. Never stage data/, memory/, graphify-out/, model config, logs, or evaluation results.
- [ ] Create the requested read-only code-review subagent with base SHA and final SHA. Fix every valid Critical/Important finding and rerun the full verification.
- [ ] Commit the final implementation with git commit -m "feat: complete plan document persistence v2".

## Required acceptance invariants

- A normal answer never creates PlanDocument, Goal, Run, PlanVersion, Approval, or ToolCall.
- A valid plan artifact saves the exact streamed Markdown as a committed SQLite revision and matching plan.md file.
- plan.document_ready is emitted before turn.completed.
- Saving a document never creates a Run or execution approval.
- Only committed document versions enter context.
- A document modification creates an immutable new version and never last-write-wins over a stale head.
- A committed version can reconstruct a missing file after restart.
- An execution Run pins one document version/hash and is not silently changed by later document edits.
- Before structured PlanVersion approval, tool execution count remains zero.
- Retry, duplicate POST, worker lease retry, SSE reconnect, service restart, and model generation reset do not create duplicate committed versions.

