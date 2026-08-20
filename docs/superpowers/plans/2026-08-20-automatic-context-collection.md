# LLM-Driven Ask Tool Implementation Plan

> This revision supersedes the earlier deterministic-context version. The model decides whether clarification is needed and what to ask; application code owns only the tool contract, validation, persistence, and streaming protocol.

## Goal

Allow a user to submit a personalized, long-term, or goal-driven request without knowing about `ask_user`. The conversation model should decide whether to answer immediately or call `ask_user`, and should generate the minimum questions tailored to the request. General questions and useful assumption-based guides should still receive a direct answer.

## Architecture

Every conversation turn enters `LiveConversationModel.route_and_respond` and calls the configured `ModelGateway` with the existing `ASK_TOOL_SCHEMA`. The system prompt describes when asking is useful, but does not classify requests or manufacture questions in Python. If the model calls `ask_user`, `parse_ask_tool_call` validates its bounded schema and returns `AskRequest`; the existing worker persists `AWAITING_INPUT`, the AskCard, answer API, continuation history, and SSE events. If the model returns a control header and Markdown, the existing answer path remains unchanged.

The application must not contain a request-keyword policy, a fixed question set, or a silent non-LLM fallback for Ask. Provider output remains untrusted: malformed tool calls are rejected through the existing gateway/turn error path, and raw tool JSON is never shown to the user.

## Task 1: Prove the model-driven boundary

**Files:**

- Modify: `backend/tests/test_live_model.py`
- Modify: `backend/tests/test_conversation_worker.py`

- [x] Replace the old test that expected a gateway bypass with a fake gateway returning a request-specific `ask_user` call.
- [x] Assert the gateway is called, the request includes `ASK_TOOL_SCHEMA`, and generated question IDs/content come from the model response rather than a fixed cycling questionnaire.
- [x] Assert the worker still persists the result as `AWAITING_INPUT` without creating goal/run rows.

## Task 2: Remove the deterministic short-circuit

**Files:**

- Modify: `backend/app/live_model.py`
- Delete: `backend/app/context_policy.py`
- Delete: `backend/tests/test_context_policy.py`

- [x] Remove the pre-gateway `requires_context_collection` branch.
- [x] Remove `_automatic_context_request` and its hard-coded cycling questions.
- [x] Keep the existing single `ask_user` tool call limit, schema validation, control-header repair, cancellation, and SSE callbacks.
- [x] Strengthen the model instruction so it distinguishes personalized missing context from direct-answer requests while leaving the decision with the model.

## Task 3: Verification and delivery

- [x] Run focused Ask/model/worker tests: 41 passed.
- [x] Run the full backend suite: 152 passed.
- [x] Run frontend tests and production build: 16 files / 78 tests passed; Vite production build passed.
- [x] Run the deterministic suite and the LLM smoke test using `D:\Users\王一鸣\Desktop\直到尽头\LLM_AP.txt` without printing or committing the key: deterministic 12/12; live smoke 1/1.
- [x] Run `git diff --check`, inspect staged paths, and ensure `data/`, `memory/`, evaluation results, `.env`, and the LLM key file remain untracked/unstaged.
- [ ] Commit the correction on `codex/personal-agent-v1`; do not modify `main`.

## Verification record

- The real model routed `我想制作一个长期的训练计划，学习骑行` to `AskRequest` with four model-generated question IDs (`experience`, `goal`, `weekly_hours`, `bike_environment`).
- The same real model routed `想花费一个星期，在广西旅游一下，推荐一下攻略` to a normal `ModelResponse` without Ask.
- No model answer text, key, local database, memory, or evaluation report was printed to the repository or staged.
