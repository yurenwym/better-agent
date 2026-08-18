# Adaptive ReAct Safety Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Let the model decide when a step is complete while presenting the runtime iteration limit as a hidden safety guard that users can extend only after a budget block.

**Architecture:** Keep the existing ModelDecision loop and hard runtime safeguards. Restrict backend budget recovery to the specific ReAct-budget block reason, then make the frontend show executed iteration count and expose one recovery action only for that state. No model protocol change or unlimited loop is introduced.

**Tech Stack:** FastAPI/Python runtime, React + TypeScript, Vitest/Testing Library, pytest, existing CSS tokens.

---

## Files

- Modify backend/app/runtime.py: reject budget additions outside ReAct-budget recovery.
- Modify backend/tests/test_react_budget.py: cover the new backend contract.
- Modify frontend/src/components/ActivityRail.tsx: replace remaining-budget display with executed-loop count.
- Modify frontend/src/pages/ChatPage.tsx: gate the recovery action and chain add-one-budget with resume.
- Modify frontend/src/__tests__/ActivityRail.test.tsx: assert the new progress semantics.
- Modify frontend/src/__tests__/ChatPage.test.tsx: assert button visibility and recovery call order.
- Create docs/superpowers/specs/2026-08-18-adaptive-react-safety-budget-amendment.md: approved product/architecture amendment.

### Task 1: Write failing backend contract tests

**Files:** backend/tests/test_react_budget.py

- [ ] Add a test that creates a normal executing run and asserts runtime.add_budget(run.id, 1) raises ValueError mentioning budget recovery.
- [ ] Add a test that creates a blocked run for a non-ReAct reason and asserts the same rejection.
- [ ] Run:

~~~powershell
pytest backend/tests/test_react_budget.py -q
~~~

Expected: the new tests fail because add_budget currently accepts every state after positive amount validation.

### Task 2: Write failing frontend behavior tests

**Files:** frontend/src/__tests__/ActivityRail.test.tsx, frontend/src/__tests__/ChatPage.test.tsx

- [ ] Change the ActivityRail fixture to include react_iteration: 3 and assert the UI shows an executed-loop label/value and does not show “剩余预算”.
- [ ] Add a ChatPage test with an EXECUTING run and assert no budget-extension button is rendered.
- [ ] Add a ChatPage test with a BLOCKED run whose budget contains blocked_reason: "react iteration budget exhausted"; assert the recovery button is rendered, and clicking it calls addBudget(run.id, 1, csrf) before resumeRun(run.id, csrf).
- [ ] Run:

~~~powershell
Set-Location D:\RAG\better\frontend
npm test -- --run src/__tests__/ActivityRail.test.tsx src/__tests__/ChatPage.test.tsx
~~~

Expected: the new assertions fail against the current always-visible “追加 1 轮预算” behavior.

### Task 3: Implement backend recovery guard

**Files:** backend/app/runtime.py

- [ ] In AgentRuntime.add_budget, after loading the Run and before mutating the budget, require:

~~~python
if run.state != AgentState.BLOCKED or run.budget.get("blocked_reason") != "react iteration budget exhausted":
    raise ValueError("budget recovery is only available after react iteration budget exhaustion")
~~~

- [ ] Keep the existing positive amount validation, increment only react_iterations_remaining, preserve the existing budget.warning event, and return the updated snapshot.
- [ ] Run the focused backend test and then the complete backend suite:

~~~powershell
pytest backend/tests/test_react_budget.py -q
pytest backend/tests -q
~~~

Expected: focused and complete backend tests pass.

### Task 4: Implement frontend safety-state presentation

**Files:** frontend/src/components/ActivityRail.tsx, frontend/src/pages/ChatPage.tsx

- [ ] Replace the budgetValue helper with a helper reading run.budget.react_iteration; render a label equivalent to “已执行循环” and a value such as “3 次”. Do not render the remaining counter in the primary progress facts.
- [ ] Add a narrow helper in ChatPage.tsx:

~~~tsx
function isReactBudgetBlocked(run: Run): boolean {
  return run.state === "BLOCKED"
    && run.budget.blocked_reason === "react iteration budget exhausted";
}
~~~

- [ ] For that state, render one button labeled “继续执行一次” whose handler awaits addBudget(run.id, 1, csrfToken) and then returns resumeRun(run.id, csrfToken) through the existing runAction path.
- [ ] Keep the generic blocked “继续执行” action for other blocked reasons, but remove the unconditional budget button from EXECUTING, AWAITING_OUTCOME, and other non-budget states.
- [ ] Keep the existing outcome buttons, cancellation button, trajectory and decision controls unchanged.
- [ ] Run the focused frontend tests and build:

~~~powershell
Set-Location D:\RAG\better\frontend
npm test -- --run src/__tests__/ActivityRail.test.tsx src/__tests__/ChatPage.test.tsx
npm run build
~~~

Expected: focused tests and production build pass.

### Task 5: Full verification and commit

**Files:** all files above.

- [ ] Run pytest backend/tests -q and npm test -- --run from the frontend directory.
- [ ] Run npm run build from D:\RAG\better\frontend.
- [ ] Run git diff --check and confirm no data, secrets, memory, eval output, or graphify output is staged.
- [ ] Commit with:

~~~powershell
git add backend/app/runtime.py backend/tests/test_react_budget.py frontend/src/components/ActivityRail.tsx frontend/src/pages/ChatPage.tsx frontend/src/__tests__/ActivityRail.test.tsx frontend/src/__tests__/ChatPage.test.tsx docs/superpowers/specs/2026-08-18-adaptive-react-safety-budget-amendment.md docs/superpowers/plans/2026-08-18-adaptive-react-safety-budget.md
git commit -m "feat: make react budget a safety guard"
~~~

Expected: one focused commit on codex/personal-agent-v1; main history remains untouched.

## Self-review

- The amendment preserves the approved hard safety limits and changes only the user-facing budget semantics and recovery gate.
- The plan covers backend authorization, frontend display, recovery sequencing, and full regression verification.
- No task gives the model authority to increase its own budget or creates an unbounded execution loop.

