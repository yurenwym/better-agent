# Rendered Plan Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the visible raw Markdown textarea in the saved-plan page with a rendered, block-structured editor while preserving Markdown as the internal versioned source of truth.

**Architecture:** Parse the current Markdown into a small typed block model for editing, render those blocks as semantic HTML elements with inline content editing, and serialize the model back to Markdown only when the existing save action is used. Reading mode continues to use the existing Markdown renderer; the backend API, CAS checks, file projection, and version history remain unchanged.

**Tech Stack:** React + TypeScript, existing Markdown renderer, native `contentEditable`, Vitest + Testing Library, CSS design tokens already used by the workspace. No new runtime dependency.

---

### Task 1: Add a typed Markdown block model and serializer

**Files:**
- Create: `frontend/src/planEditor.ts`
- Test: `frontend/src/__tests__/planEditor.test.ts`

- [ ] **Step 1: Write failing parser and serializer tests**

Cover the supported visual blocks: headings, paragraphs, unordered and ordered lists, blockquotes, tables, fenced code blocks, horizontal rules, and raw fallback blocks. Assert that a representative plan is parsed into typed blocks and that serializing the edited block model produces Markdown with the expected structure.

```typescript
it("parses the plan structures shown by the visual editor", () => {
  expect(parseMarkdownForEditor("# Trip\n\n- Train\n- Hotel\n\n| Day | Place |\n| --- | --- |\n| 1 | 桂林 |"))
    .toEqual([
      { id: "block-0", type: "heading", level: 1, text: "Trip" },
      { id: "block-1", type: "bullet-list", items: ["Train", "Hotel"] },
      { id: "block-2", type: "table", headers: ["Day", "Place"], rows: [["1", "桂林"]] },
    ]);
});

it("serializes edited rendered blocks back to Markdown", () => {
  expect(serializeEditorBlocks([
    { id: "block-0", type: "heading", level: 2, text: "Updated title" },
    { id: "block-1", type: "paragraph", text: "Updated body." },
    { id: "block-2", type: "bullet-list", items: ["One", "Two"] },
  ])).toBe("## Updated title\n\nUpdated body.\n\n- One\n- Two");
});
```

- [ ] **Step 2: Run the focused test and confirm the expected missing-module failure**

Run from `D:\RAG\better\frontend`:

```powershell
npm test -- --run src/__tests__/planEditor.test.ts
```

Expected: FAIL because `frontend/src/planEditor.ts` does not exist yet.

- [ ] **Step 3: Implement the minimal parser and serializer**

Export `EditorBlock`, `parseMarkdownForEditor(markdown: string)`, and `serializeEditorBlocks(blocks: EditorBlock[])`. Parse contiguous list items into one list block, detect GitHub-style table separator rows, preserve fenced code language and contents, and emit unknown lines as `raw` blocks so unsupported Markdown is never silently discarded. Use stable `block-N` IDs for the editor session.

- [ ] **Step 4: Run the focused parser tests**

```powershell
npm test -- --run src/__tests__/planEditor.test.ts
```

Expected: all parser and serializer tests pass.

### Task 2: Build the rendered block editor

**Files:**
- Create: `frontend/src/components/PlanVisualEditor.tsx`
- Test: `frontend/src/__tests__/PlanVisualEditor.test.tsx`

- [ ] **Step 1: Write failing component tests**

Test that the editor renders semantic headings, paragraphs, lists, and tables instead of a `textarea`; editing a heading/list item calls `onChange` with serialized Markdown; deleting a block removes it; and the add-paragraph action inserts editable content.

```typescript
it("edits rendered content and emits Markdown", () => {
  const onChange = vi.fn();
  render(<PlanVisualEditor markdown={"# Trip\n\n- Train"} onChange={onChange} />);

  expect(screen.queryByRole("textbox", { name: "Markdown editor" })).toBeNull();
  fireEvent.input(screen.getByRole("heading", { name: "Trip" }), {
    target: { textContent: "Updated trip" },
  });
  fireEvent.input(screen.getByRole("listitem", { name: "Train" }), {
    target: { textContent: "Updated train" },
  });

  expect(onChange).toHaveBeenLastCalledWith("# Updated trip\n\n- Updated train");
});
```

- [ ] **Step 2: Run the component test and verify it fails for the missing component**

```powershell
npm test -- --run src/__tests__/PlanVisualEditor.test.tsx
```

Expected: FAIL because the component and its editable block behavior are not implemented.

- [ ] **Step 3: Implement the component with native semantic elements**

Use `parseMarkdownForEditor` for the initial model. Render headings as `h1`–`h6`, paragraphs as `p`, lists as `ul`/`ol`, quotes as `blockquote`, tables as `table`, code as `pre`, and raw blocks as editable paragraphs. Add `contentEditable`, `suppressContentEditableWarning`, `aria-label`, and `onInput` handlers only to the editable text nodes. Add keyboard-accessible buttons for deleting a block and inserting a paragraph. Keep block IDs as React keys and emit `serializeEditorBlocks` after each edit.

- [ ] **Step 4: Run the component tests and check the green result**

```powershell
npm test -- --run src/__tests__/PlanVisualEditor.test.tsx
```

Expected: all rendered-editing tests pass without exposing a raw Markdown textbox.

### Task 3: Integrate read/edit modes into the saved-plan page

**Files:**
- Modify: `frontend/src/pages/PlanPage.tsx`
- Modify: `frontend/src/__tests__/PlanPage.test.tsx`

- [ ] **Step 1: Add failing PlanPage behavior tests**

Assert that a loaded plan starts in reading mode, the visible content is rendered Markdown, clicking `编辑计划` shows the block editor, clicking `取消编辑` restores the saved content, and clicking `保存计划` sends the edited serialized Markdown through the existing `putPlanDocument` CAS payload.

```typescript
it("saves edits made in the rendered plan view", async () => {
  render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);
  await screen.findByRole("heading", { name: "Travel plan" });
  expect(screen.queryByRole("textbox", { name: "Markdown editor" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "编辑计划" }));
  fireEvent.input(screen.getByRole("heading", { name: "Travel plan" }), {
    target: { textContent: "Updated plan" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存计划" }));

  await waitFor(() => expect(api.putPlanDocument).toHaveBeenCalledWith(
    "plan-1",
    expect.objectContaining({ markdown: expect.stringContaining("# Updated plan") }),
    "csrf",
  ));
});
```

- [ ] **Step 2: Run the PlanPage test and confirm the expected failure**

```powershell
npm test -- --run src/__tests__/PlanPage.test.tsx
```

Expected: FAIL because the page still renders the raw editor and has no rendered edit mode.

- [ ] **Step 3: Replace the raw editor with read/edit mode controls**

Keep `markdown`, `title`, `saveDocument`, history loading, conflict handling, and version CAS unchanged. Add an `editing` state initialized to `false`. In reading mode render `MarkdownMessage`; in editing mode render `PlanVisualEditor`. Add `编辑计划`, `取消编辑`, and `保存计划` actions with disabled/busy states. Keep title editing inside the visual editor header and never render the raw Markdown textarea in the normal page.

- [ ] **Step 4: Run the PlanPage tests**

```powershell
npm test -- --run src/__tests__/PlanPage.test.tsx
```

Expected: existing plan selection, save, conflict, restore, delete, pending, and execution tests plus the new read/edit tests pass.

### Task 4: Add responsive visual-editor styling

**Files:**
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Add styles for the rendered editor surface**

Add styles for `.plan-visual-editor`, `.plan-visual-editor-toolbar`, `.plan-editor-block`, `.plan-editor-block-actions`, editable headings/paragraphs/list items, `.plan-editor-table`, and `.plan-editor-empty`. Reuse existing semantic color tokens, focus-visible rules, 44px button targets, the current card radius/shadow, and the existing breakpoints. Keep the read-mode preview unchanged.

- [ ] **Step 2: Run the frontend build to catch CSS and TypeScript integration errors**

```powershell
npm run build
```

Expected: TypeScript compilation and Vite production build succeed.

### Task 5: Full verification and delivery

**Files:**
- No additional source files; verify the complete diff.

- [ ] **Step 1: Run all frontend tests**

```powershell
npm test -- --run
```

Expected: all frontend test files pass with zero failures.

- [ ] **Step 2: Run backend regression tests because the persistence API remains shared**

```powershell
& 'D:\pycharm\python.exe' -m pytest tests -q
```

Run from `D:\RAG\better\backend`. Expected: existing backend suite passes; no backend behavior changes are expected.

- [ ] **Step 3: Restart the local service and verify the deployed bundle**

```powershell
$env:LLM_AP_PATH = 'D:\Users\王一鸣\Desktop\直到尽头\LLM_AP.txt'
& 'D:\pycharm\python.exe' scripts\start.py
```

Verify `http://127.0.0.1:8000`, `GET /api/plans` returns `200`, and the served HTML references the current Vite bundle.

- [ ] **Step 4: Commit the implementation**

```powershell
git add docs/superpowers/plans/2026-08-21-rendered-plan-editor.md frontend/src/planEditor.ts frontend/src/components/PlanVisualEditor.tsx frontend/src/__tests__/planEditor.test.ts frontend/src/__tests__/PlanVisualEditor.test.tsx frontend/src/pages/PlanPage.tsx frontend/src/__tests__/PlanPage.test.tsx frontend/src/styles.css
git commit -m "feat: add rendered plan editor"
```

Expected: a clean `codex/personal-agent-v1` working tree with no `data/`, secrets, or evaluation outputs staged.
