# Agent UI and Trace Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with checkpoints.

**Goal:** Fix the OpenAI-compatible tool payload so real LLM Runs do not fail at request validation, then redesign the React workspace so progress and trajectory are readable without exposing raw event IDs as the primary UX.

**Architecture:** Keep the existing FastAPI/SSE/event contracts. Convert internal ToolSpec values at the model boundary into the standard type=function wire shape. Add a small frontend trajectory view-model that maps append-only events into human labels, stages, summaries, and grouped timeline items; share one telemetry hook between the chat activity rail and the full trajectory page. Refresh the visual system around a calm editorial workbench: strong type hierarchy, restrained dark surfaces, clear status colors, and responsive two-column layouts.

**Tech Stack:** Python 3.11, FastAPI, SQLite event API, React, TypeScript, Vitest, Vite, CSS tokens, SSE, installed ui-ux-pro-max-skill guidance.

---

### Task 1: Make model tools valid for the OpenAI-compatible API

**Files:**
- Modify: backend/app/tools.py:88-97
- Test: backend/tests/test_tools.py

- [ ] Step 1: Write the failing contract test

Add a test that creates the default registry and asserts every public model tool has the provider wire shape while preserving its JSON Schema parameters:

    def test_tool_descriptions_use_openai_function_wire_format(tmp_path) -> None:
        from app.tools import create_default_registry

        registry = create_default_registry(tmp_path / "workspace")
        descriptions = registry.describe()

        assert descriptions
        assert {tool["type"] for tool in descriptions} == {"function"}
        first = descriptions[0]
        assert set(first["function"]) == {"name", "description", "parameters"}
        assert first["function"]["parameters"]["type"] == "object"

- [ ] Step 2: Run the contract test and verify the expected failure

Run from D:\RAG\better\backend:

    python -m pytest -q tests/test_tools.py -k wire_format

Expected: failure because ToolRegistry.describe() currently returns a flat object without type and function.

- [ ] Step 3: Implement the smallest boundary adapter

Change ToolRegistry.describe() to return only provider-safe fields:

    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.schema,
            },
        }
        for spec in self._tools.values()
    ]

Do not send internal risk or timeout_seconds fields to the provider; those remain registry/runtime concerns.

- [ ] Step 4: Run the contract and full backend tests

    python -m pytest -q tests/test_tools.py -k wire_format
    python -m pytest -q

Expected: the new contract passes and the complete backend suite has zero failures.

- [ ] Step 5: Verify the real model boundary with the configured local profile

Use D:\Users\王一鸣\Desktop\直到尽头\LLM_AP.txt through LLM_AP_PATH, run one minimal LiveRuntimeModel.needs_clarification() call, and report only status/result metadata. Never print the key or persist the request.

### Task 2: Install and read the requested UI/UX skill

**Files:**
- External skill destination: $CODEX_HOME/skills/ui-ux-pro-max-skill/
- No repository code changes from the installation

- [ ] Step 1: Install from the requested GitHub URL after Task 1 passes

    python C:\Users\wym\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py --url https://github.com/nextlevelbuilder/ui-ux-pro-max-skill

- [ ] Step 2: Read the installed SKILL.md and only the references/scripts it routes to for React UI redesign

Use its visual direction, component guidance, and validation commands; do not copy unrelated assets or add an unnecessary frontend dependency.

- [ ] Step 3: Record the selected direction before editing UI code

Use a dark editorial operations desk: warm paper-green background, one high-contrast accent, compact monospace telemetry, a readable serif/display title, and a visible activity spine. Keep the existing four pages and backend APIs.

### Task 3: Add a tested readable trajectory view-model

**Files:**
- Create: frontend/src/trajectory.ts
- Create: frontend/src/__tests__/trajectory.test.ts
- Modify: frontend/src/types.ts

- [ ] Step 1: Write failing tests for human-readable event mapping

Cover these behaviors:
- state.transitioned maps to stage state and a label such as entering planning.
- interaction.started, model.invocation_started, and interaction.ended form one sequence-ordered group.
- model.attempt_finished with error_kind=request maps to a model request failure label without exposing api_key or raw provider payloads.

- [ ] Step 2: Run the new tests and verify they fail because the view-model is absent

    npm.cmd test -- --run src/__tests__/trajectory.test.ts

- [ ] Step 3: Implement describeEvent, groupEvents, and typed stage metadata

The mapping must cover interaction, context, model, plan, tool/approval, memory, checkpoint, state, and run terminal events. Each item returns stage, title, detail, tone, seq, occurredAt, and the original event for an optional raw-data disclosure.

- [ ] Step 4: Run the trajectory tests and the existing frontend tests

    npm.cmd test -- --run

Expected: all existing tests plus the new mapping tests pass.

### Task 4: Share live telemetry and expose progress in the chat workspace

**Files:**
- Create: frontend/src/hooks/useRunTelemetry.ts
- Create: frontend/src/components/ActivityRail.tsx
- Modify: frontend/src/pages/ChatPage.tsx
- Modify: frontend/src/pages/TrajectoryPage.tsx
- Modify: frontend/src/components/EventStream.tsx

- [ ] Step 1: Add a failing component test for the readable activity rail

Render a run with interaction.started, model.invocation_started, plan.created, and state.transitioned events. Assert the chat page shows human stage names such as “正在理解目标”, “正在生成计划”, and “已进入规划”, while raw interaction_id text is absent from the primary view.

- [ ] Step 2: Implement useRunTelemetry

Load /events and /stats on run change, subscribe through the existing SSE client with the current sequence cursor, deduplicate by seq, and expose events, stats, loading, and error. Keep cleanup closing EventSource.

- [ ] Step 3: Implement ActivityRail

Show a compact “现在发生什么” card on ChatPage with current run state and a plain-language description, a vertical three-to-five item recent activity timeline, current plan progress and budget remaining when available, and a direct “查看完整轨迹” action supplied by the parent.

- [ ] Step 4: Refactor TrajectoryPage and EventStream to use the shared view-model

The full page must provide stage filters, search over human labels/details, grouped interaction/plan/ReAct sections, sequence/time ordering controls, and a collapsed raw JSON disclosure per item. Raw IDs remain available only inside details or copyable metadata.

- [ ] Step 5: Run component tests and verify SSE cleanup/dedup behavior

    npm.cmd test -- --run

### Task 5: Apply the downloaded design system without changing product scope

**Files:**
- Modify: frontend/src/tokens.css
- Modify: frontend/src/styles.css
- Modify: frontend/src/App.tsx
- Modify: frontend/src/pages/ChatPage.tsx
- Modify: frontend/src/pages/PlanPage.tsx
- Modify: frontend/src/pages/TrajectoryPage.tsx
- Modify: frontend/src/pages/MemoryPage.tsx
- Modify: frontend/src/components/StatsBar.tsx
- Modify: frontend/src/components/ApprovalCard.tsx
- Modify: frontend/index.html

- [ ] Step 1: Replace the current flat page rhythm with a responsive workbench shell

Keep the four routes but introduce a utility header, active-run status block, two-column chat layout, card hierarchy, visible dividers, hover/focus states, and mobile collapse behavior.

- [ ] Step 2: Improve typography, color, and status semantics

Use the installed skill’s recommended scale and contrast checks. Preserve accessible focus outlines, prefers-reduced-motion, keyboard labels, and semantic buttons. Do not add gradients, decorative stock imagery, or a UI library solely for styling.

- [ ] Step 3: Make failures and approvals understandable

Show “模型请求失败 / 需要审批 / 已完成 / 已阻塞” as user-facing labels with a short next action. Keep approval buttons explicit and visually distinct from ordinary actions.

- [ ] Step 4: Run frontend tests and production build

    npm.cmd test -- --run
    npm.cmd run build

### Task 6: End-to-end verification and commits

**Files:**
- No new runtime data or evaluation results committed

- [ ] Step 1: Run backend and frontend verification from D:\RAG\better

    Set-Location backend
    python -m pytest -q
    Set-Location ..\frontend
    npm.cmd test -- --run
    npm.cmd run build
    Set-Location ..
    git diff --check

- [ ] Step 2: Start the app with the requested local LLM profile and verify

    $env:LLM_AP_PATH = "D:\Users\王一鸣\Desktop\直到尽头\LLM_AP.txt"
    python scripts\start.py

Confirm /api/health is 200 and execute one real goal request. Do not commit data/, frontend/dist/, node_modules/, keys, or live eval output.

- [ ] Step 3: Review the diff for scope and commit in focused changes

    git status --short
    git diff --stat
    git add backend/app/tools.py backend/tests/test_tools.py
    git commit -m "fix: send provider-compatible tool schemas"
    git add frontend/src frontend/index.html frontend/package.json frontend/package-lock.json
    git commit -m "feat: redesign workspace and readable trajectory"

The final report must include implemented items, fresh test evidence, start command, branch/commits, and any remaining specification gaps.

