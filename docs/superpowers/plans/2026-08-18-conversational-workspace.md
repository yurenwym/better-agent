# Conversational Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every model response and present it in a DeepSeek-inspired conversation workspace while keeping the V1 plan, approval, statistics, and trajectory surfaces available.

**Architecture:** Extend the existing `messages` table as the canonical conversation log. Each successful model gateway call writes an assistant message and a `model.response` trajectory event; the API exposes messages separately so the frontend can hydrate and then append SSE responses without polling. The React shell becomes a responsive two-column workspace: persistent left navigation/session context and a right conversation surface with an optional inspector for run controls and readable activity.

**Tech Stack:** FastAPI, SQLite WAL, React 18+, TypeScript, Vitest, CSS design tokens, native HTML controls, SSE.

---

### Task 1: Define and prove the conversation response contract

**Files:**
- Create: `backend/tests/test_messages.py`
- Modify: `backend/app/runtime.py`
- Modify: `backend/app/api.py`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`

- [x] **Step 1: Write the failing backend test**

Add a model double that exposes a `last_response` object and assert that a handled user message creates both a user message and an assistant message, emits `model.response`, and returns them from `GET /api/runs/{run_id}/messages`.

- [x] **Step 2: Run the focused test and confirm the expected failure**

Run `D:\pycharm\python.exe -m pytest backend/tests/test_messages.py -q` from `D:\RAG\better`.
Expected failure: the messages route is missing and no assistant message is persisted.

- [x] **Step 3: Implement the smallest persistence/API change**

Add `_append_model_message()` in `AgentRuntime` and call it from `_model_call()` after a successful `ModelResponse`. Insert an assistant row into `messages`, append `model.response` with the message id, interaction id, model kind, and response content, and use the latest interaction when an execution call has no active user interaction. Add `GET /api/runs/{run_id}/messages` returning id, role, content, created_at, and interaction_id. Add matching TypeScript types and `getMessages()`.

- [x] **Step 4: Run the focused test and the backend suite**

Run `D:\pycharm\python.exe -m pytest backend/tests/test_messages.py backend/tests/test_runtime.py backend/tests/test_api.py -q`, then `D:\pycharm\python.exe -m pytest -q` from `D:\RAG\better\backend`.

### Task 2: Hydrate and stream conversation messages in React

**Files:**
- Modify: `frontend/src/hooks/useRunTelemetry.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/trajectory.ts`
- Create: `frontend/src/conversation.ts`
- Create: `frontend/src/__tests__/conversation.test.ts`

- [x] **Step 1: Write the failing formatter tests**

Cover a user message, a clarification JSON response, a plan JSON response, a ReAct JSON response, and an invalid/non-JSON response. Each test must assert the visible human summary and that raw model output remains available.

- [x] **Step 2: Run the focused frontend test and confirm it fails**

Run `npm.cmd test -- --run src/__tests__/conversation.test.ts` from `D:\RAG\better\frontend`.
Expected failure: the conversation formatter module does not exist.

- [x] **Step 3: Implement message hydration and SSE append behavior**

Extend `useRunTelemetry` with `messages`, load them beside events/stats, refresh on the Run version, and append assistant messages from `model.response` events using message-id/sequence deduplication. Keep existing trajectory deduplication unchanged.

- [x] **Step 4: Implement readable response formatting**

Create pure helpers that map structured model JSON to short Chinese summaries (clarification result, plan summary/steps, ReAct action, reflection candidates) and preserve the original response for an expandable raw-details block.

- [x] **Step 5: Run focused and full frontend tests**

Run `npm.cmd test -- --run src/__tests__/conversation.test.ts src/__tests__/ActivityRail.test.tsx src/__tests__/trajectory.test.ts` and then `npm.cmd test -- --run`.

### Task 3: Replace the single-page shell with the conversation workspace

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/pages/ChatPage.tsx`
- Create: `frontend/src/components/WorkspaceSidebar.tsx`
- Create: `frontend/src/components/ConversationThread.tsx`
- Modify: `frontend/src/components/ActivityRail.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/tokens.css`
- Modify: `frontend/index.html`

- [x] **Step 1: Add component tests for the user-visible contract**

Assert that the conversation view renders the user prompt and assistant summary, that raw JSON is behind a disclosure control, that submitting a follow-up remains available, and that the sidebar exposes chat, plan, trajectory, memory, and settings labels with accessible names.

- [x] **Step 2: Run the focused component test and confirm it fails**

Run `npm.cmd test -- --run src/__tests__/ConversationThread.test.tsx` from `D:\RAG\better\frontend`.
Expected failure: the new conversation components are not implemented.

- [x] **Step 3: Implement the two-column workspace**

Keep top-level navigation in a persistent left sidebar with new conversation, current run, settings/model status, and links to plan/trajectory/memory. Render the main content as a scrollable conversation thread with user bubbles, assistant cards, response status, follow-up composer, approval controls, and a compact right-side activity inspector on wide screens. On small screens collapse the sidebar into a top bar and move the inspector below the composer.

- [x] **Step 4: Apply the visual system and accessibility rules**

Use light neutral surfaces, dark ink, one restrained blue/green accent, 8px spacing rhythm, visible focus rings, native buttons/inputs, `aria-live="polite"` for new assistant responses, reduced-motion handling, and no emoji icons. Keep trajectory as a named navigation destination and keep raw audit details expandable.

- [x] **Step 5: Run component tests and production build**

Run `npm.cmd test -- --run` and `npm.cmd run build` from `D:\RAG\better\frontend`.

### Task 4: End-to-end verification and delivery

**Files:**
- Modify: `README.md` if the workspace interaction/start instructions change.

- [x] **Step 1: Run all backend and frontend checks**

Run `D:\pycharm\python.exe -m pytest -q` from `D:\RAG\better\backend`, `npm.cmd test -- --run`, `npm.cmd run build`, and `git diff --check`.

- [x] **Step 2: Verify the running local service**

Confirm `GET http://127.0.0.1:8000/`, `/api/bootstrap`, and a real run's `/api/runs/{run_id}/messages` return 200 without printing API keys or local data contents.

- [x] **Step 3: Commit the implementation**

Use `git add` only for source, tests, docs, and frontend metadata; never add `data/`, `frontend/dist/`, `frontend/node_modules/`, memory, eval output, or key files. Commit with `feat: show model responses in conversation workspace`.
