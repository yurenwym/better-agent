# Automatic Context Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the first turn of a personalized training or learning plan automatically collect the minimum user context without requiring the user to know about `ask_user`.

**Architecture:** Add a small deterministic conversation-context policy at the `LiveConversationModel` boundary. When the current request is a personalized training/learning plan and the latest history is not an answered Ask continuation, the adapter returns a validated `AskRequest` directly; the existing worker, `AWAITING_INPUT` persistence, AskCard, answer API, SSE, and continuation history remain the source of truth. Ordinary questions, one-off guides, templates, and already-answered Ask continuations continue through the normal model route.

**Tech Stack:** Python 3.11, existing FastAPI conversation worker, pytest/pytest-asyncio, existing React AskCard and SSE flow.

---

### Task 1: Define and test the automatic context policy

**Files:**
- Create: `backend/app/context_policy.py`
- Test: `backend/tests/test_context_policy.py`

- [ ] **Step 1: Write failing policy tests**

Cover the user-visible boundary:

```python
def test_personalized_training_plan_requires_context_collection():
    assert requires_context_collection("我想制作一个长期的训练计划，学习骑行", []) is True
    assert requires_context_collection("帮我制定一个六周英语学习计划", []) is True


def test_ordinary_content_requests_do_not_require_context_collection():
    assert requires_context_collection("什么是自行车 FTP？", []) is False
    assert requires_context_collection("给我一份桂林 7 天旅游攻略", []) is False
    assert requires_context_collection("写一个训练计划模板", []) is False


def test_answered_ask_continuation_is_not_asked_again():
    history = [
        {"role": "assistant", "content": "为了更准确地完成这个目标，请先补充以下信息。"},
        {"role": "tool", "tool_call_id": "call-1", "content": "{\"answers\": []}"},
    ]
    assert requires_context_collection("我已经补充了信息，请继续", history) is False
```

- [ ] **Step 2: Run the focused policy tests and confirm RED**

Run from `D:\RAG\better\backend`:

```powershell
python -m pytest tests/test_context_policy.py -q
```

Expected: collection failure because `app.context_policy` does not exist.

- [ ] **Step 3: Implement the minimal deterministic policy**

Create a narrow rule set for personalized training/learning requests, exclude ordinary knowledge, travel guides, and templates, and return `False` when the latest history item is a tool result from an Ask continuation. Keep the policy pure and independent of FastAPI, SQLite, and the model gateway.

- [ ] **Step 4: Run the focused policy tests and commit the policy slice**

Run `python -m pytest tests/test_context_policy.py -q`; expected result is all policy tests passing. Commit with:

```powershell
git add backend/app/context_policy.py backend/tests/test_context_policy.py
git commit -m "feat: detect personalized context needs"
```

### Task 2: Generate an automatic AskCard for personalized plans

**Files:**
- Modify: `backend/app/live_model.py`
- Test: `backend/tests/test_live_model.py`
- Test: `backend/tests/test_conversation_worker.py`

- [ ] **Step 1: Write failing model and worker tests**

Add a gateway that raises if called, then assert the route returns an `AskRequest` for the user's natural request without requiring an explicit `ask_user` phrase. Add a worker assertion that the resulting Turn is `AWAITING_INPUT`, the Ask contains questions about level, goal, schedule, and constraints, and no Goal/Run is created.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_live_model.py tests/test_conversation_worker.py -k "automatic_context or personalized" -q
```

Expected: failure because `LiveConversationModel` currently always delegates the first route to the model gateway.

- [ ] **Step 3: Implement the automatic AskRequest path**

At the start of `LiveConversationModel.route_and_respond`, call the pure policy. If it returns `True`, return a unique internal `AskRequest` with four bounded, user-facing questions and do not make a model request. Otherwise preserve the current model/tool route unchanged.

- [ ] **Step 4: Run focused model/worker tests and the existing ask suite**

Run:

```powershell
python -m pytest tests/test_context_policy.py tests/test_live_model.py tests/test_conversation_worker.py -q
```

Expected: all pass, including existing Ask tool, continuation, cancellation, and normal answer tests.

### Task 3: Full verification and delivery

**Files:**
- Modify: `docs/superpowers/plans/2026-08-20-automatic-context-collection.md`
- No frontend production changes expected; the existing AskCard renders the generated questions.

- [ ] **Step 1: Run backend and frontend verification**

Run `python -m pytest -q` from `backend`, then `npm test -- --run` and `npm run build` from `frontend`.

- [ ] **Step 2: Run the deterministic evaluation and inspect the diff**

Run `python -m app.eval run --suite v1 --mode deterministic`, `git diff --check`, and confirm no `data/`, `memory/`, `evals/results/`, `.env`, or LLM key file is staged.

- [ ] **Step 3: Mark this plan complete and commit**

Record the test counts, commit the plan, and preserve the current `codex/personal-agent-v1` branch without modifying `main`.
