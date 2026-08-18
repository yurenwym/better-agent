# Readable Markdown Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Render ordinary model Markdown as readable, safe React message content while preserving structured JSON presentation, streaming safeguards, trajectory UI, and existing conversation controls.

**Architecture:** Add a focused MarkdownMessage React component with a small block/inline parser that emits semantic React elements only. ConversationThread will use it for visible summary/detail/user content; presentMessage remains the boundary that converts structured JSON and hides incomplete streaming JSON. Scoped message styles will add readable hierarchy, tables, code blocks, lists, and responsive overflow without changing backend contracts.

**Tech Stack:** React 19, TypeScript, Vitest, Testing Library, Vite, existing CSS custom properties. No new runtime dependency.

---

## Files and responsibilities

- Create: frontend/src/components/MarkdownMessage.tsx — safe Markdown subset parser and semantic renderer; never use dangerouslySetInnerHTML.
- Create: frontend/src/__tests__/MarkdownMessage.test.tsx — component tests for supported syntax, safety, and malformed-input fallback.
- Modify: frontend/src/components/ConversationThread.tsx — replace visible plain-text message bodies with MarkdownMessage while preserving existing presentation policy and controls.
- Modify: frontend/src/__tests__/ConversationWorkspace.test.tsx — regression test for an ordinary assistant Markdown reply.
- Modify: frontend/src/styles.css — scoped hierarchy, table, code-block, list, quote, and narrow-screen styles.
- Create: docs/superpowers/plans/2026-08-18-readable-markdown-messages.md — this implementation record.

### Task 1: Add failing component tests for the Markdown renderer

**Files:**

- Create: frontend/src/__tests__/MarkdownMessage.test.tsx

- [x] **Step 1: Write the failing test**

Create a test that asks for the semantic output required by the approved preview:

~~~tsx
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import MarkdownMessage from "../components/MarkdownMessage";

afterEach(cleanup);

describe("MarkdownMessage", () => {
  it("renders readable semantic Markdown instead of source markers", () => {
    render(
      <MarkdownMessage
        content={[
          "## 2天桂林游",
          "",
          "### 费用预算",
          "",
          "**经济型**：人均约 ¥700–900",
          "",
          "| 项目 | 预算 |",
          "| --- | --- |",
          "| 门票 | ¥180–250 |",
          "",
          "- 提前订票",
          "- 带好雨衣",
          "",
          "> 以官方平台实时信息为准。",
        ].join("\n")}
      />,
    );

    expect(screen.getByRole("heading", { name: "2天桂林游" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "费用预算" })).toBeTruthy();
    expect(screen.getByText("经济型").tagName).toBe("STRONG");
    expect(screen.getByRole("table")).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "项目" })).toBeTruthy();
    expect(screen.getByRole("list")).toBeTruthy();
    expect(screen.getByRole("blockquote")).toBeTruthy();
    expect(screen.queryByText("## 2天桂林游")).toBeNull();
    expect(screen.queryByText("**经济型**：人均约 ¥700–900")).toBeNull();
    expect(screen.queryByText("| 项目 | 预算 |")).toBeNull();
  });

  it("renders fenced code and keeps raw HTML as text", () => {
    const fence = String.fromCharCode(96).repeat(3);
    render(
      <MarkdownMessage
        content={fence + "json\n{\"state\":\"COMPLETED\"}\n" + fence + "\n\n<script>alert(\"x\")</script>"}
      />,
    );

    expect(screen.getByRole("code")).toHaveTextContent("{\"state\":\"COMPLETED\"}");
    expect(screen.queryByRole("script")).toBeNull();
    expect(screen.getByText("<script>alert(\"x\")</script>")).toBeTruthy();
  });

  it("degrades incomplete blocks to visible code without throwing", () => {
    const fence = String.fromCharCode(96).repeat(3);
    render(<MarkdownMessage content={fence + "\npartial output"} />);

    expect(screen.getByRole("code")).toHaveTextContent("partial output");
  });
});
~~~

- [x] **Step 2: Run the focused test and verify it fails for the missing component**

Run from D:\RAG\better\frontend:

~~~powershell
npm test -- --run src/__tests__/MarkdownMessage.test.tsx
~~~

Expected result: FAIL because ../components/MarkdownMessage does not exist. Do not weaken assertions to make current plain-text behavior pass.

### Task 2: Implement the smallest safe Markdown renderer

**Files:**

- Create: frontend/src/components/MarkdownMessage.tsx
- Test: frontend/src/__tests__/MarkdownMessage.test.tsx

- [x] **Step 1: Define the component contract and block parser**

Use this public shape and keep parser helpers private to the file:

~~~tsx
interface MarkdownMessageProps {
  content: string;
  className?: string;
}

export default function MarkdownMessage({ content, className }: MarkdownMessageProps) {
  const classes = className ? "message-markdown " + className : "message-markdown";
  return <div className={classes}>{renderBlocks(content)}</div>;
}
~~~

The block scanner should walk content.split(/\r?\n/) in order and emit nodes for:

1. fenced code beginning with the three-backtick marker, collecting until the next fence or end-of-input and rendering pre.message-code-block > code;
2. headings matching one to six hashes, mapping one/two hashes to h2 and three or more to h3 so the existing ##/### model output matches the approved hierarchy;
3. valid pipe tables only when the next line is a separator whose cells match :?-{3,}:?, rendered inside a message-table-scroll wrapper with table, thead, and tbody;
4. consecutive unordered or ordered list lines as ul or ol with li children;
5. consecutive quote lines as blockquote;
6. horizontal rules (---, ***, ___);
7. non-empty paragraph runs as p with inline line breaks.

Unrecognized lines remain visible as text. Empty input returns an empty message container. Use stable keys based on block start index and block type.

- [x] **Step 2: Implement safe inline rendering**

The inline renderer should split text into plain React text nodes plus these elements, in this precedence order: backtick code, strong (** or __), and emphasis (* or _). One level of nesting is enough for this V1. Do not parse or inject HTML, URLs, images, or scripts.

The token branches should have the equivalent behavior:

~~~tsx
if (token.startsWith(String.fromCharCode(96))) {
  return <code key={key}>{token.slice(1, -1)}</code>;
}
if (token.startsWith("**") || token.startsWith("__")) {
  return <strong key={key}>{token.slice(2, -2)}</strong>;
}
if (token.startsWith("*") || token.startsWith("_")) {
  return <em key={key}>{token.slice(1, -1)}</em>;
}
return token;
~~~

Plain strings are passed as React children, which is the escaping boundary. No dangerouslySetInnerHTML may appear in the new file.

- [x] **Step 3: Run the focused tests and verify they pass**

Run:

~~~powershell
npm test -- --run src/__tests__/MarkdownMessage.test.tsx
~~~

Expected result: all MarkdownMessage tests PASS, with no raw Markdown markers rendered as text and no DOM script element created.

### Task 3: Wire the renderer into the conversation without changing presentation policy

**Files:**

- Modify: frontend/src/components/ConversationThread.tsx
- Modify: frontend/src/__tests__/ConversationWorkspace.test.tsx

- [x] **Step 1: Add the integration regression test before wiring the component**

Add a focused test with this assistant content:

~~~tsx
it("renders ordinary assistant Markdown as readable content", () => {
  render(
    <ConversationThread
      messages={[{
        id: "message-markdown",
        run_id: run.id,
        interaction_id: "interaction-1",
        role: "assistant",
        content: "## 行程建议\n\n**第一天**：游览象鼻山。\n\n| 项目 | 建议 |\n| --- | --- |\n| 门票 | 提前预订 |",
        created_at: "2026-08-18T00:00:02Z",
      }]}
      busy={false}
      onSubmit={() => undefined}
    />,
  );

  expect(screen.getByRole("heading", { name: "行程建议" })).toBeTruthy();
  expect(screen.getByText("第一天").tagName).toBe("STRONG");
  expect(screen.getByRole("table")).toBeTruthy();
  expect(screen.queryByText("## 行程建议")).toBeNull();
});
~~~

- [x] **Step 2: Run the integration test and verify it fails**

Run:

~~~powershell
npm test -- --run src/__tests__/ConversationWorkspace.test.tsx
~~~

Expected result: FAIL because the current p element renders the heading source as text and creates no table or heading elements.

- [x] **Step 3: Replace only visible message text nodes with MarkdownMessage**

Import MarkdownMessage and use it for view.summary, view.detail, authored user content, pending user content, and the generic pending status. Keep view.bullets.map(...) because it is already structured data. The relevant shape is:

~~~tsx
<MarkdownMessage content={view.summary} className="message-summary" />
{view.detail && <MarkdownMessage content={view.detail} className="message-detail" />}
{view.bullets.length > 0 && (
  <ul className="message-bullets">
    {view.bullets.map((bullet) => <li key={bullet}>{bullet}</li>)}
  </ul>
)}
~~~

Do not pass message.content directly for assistant messages; presentMessage remains the policy boundary that converts JSON and hides internal fields. Incomplete JSON streaming must continue to produce the existing generic status from presentMessage.

- [x] **Step 4: Run the integration tests and verify they pass**

Run:

~~~powershell
npm test -- --run src/__tests__/ConversationWorkspace.test.tsx src/__tests__/conversation.test.ts
~~~

Expected result: the new Markdown integration test and all existing raw-JSON, streaming, and decision tests PASS.

### Task 4: Add scoped message styles and responsive behavior

**Files:**

- Modify: frontend/src/styles.css

- [x] **Step 1: Add styles scoped to the new message classes**

Add rules beside the existing message styles. Use existing tokens and cover hierarchy, lists, quote, inline code, fenced code, tables, and local overflow:

~~~css
.message-markdown { display: grid; gap: 10px; min-width: 0; color: var(--color-ink); font-size: 14px; line-height: 1.68; overflow-wrap: anywhere; }
.message-markdown > :first-child { margin-top: 0; }
.message-markdown > :last-child { margin-bottom: 0; }
.message-markdown h2, .message-markdown h3 { color: var(--color-ink); letter-spacing: -0.02em; }
.message-markdown h2 { margin: 4px 0 0; font-size: 20px; }
.message-markdown h3 { margin: 12px 0 0; font-size: 16px; }
.message-markdown p { margin: 0; }
.message-markdown ul, .message-markdown ol { margin: 0; padding-left: 22px; }
.message-markdown li + li { margin-top: 5px; }
.message-markdown blockquote { margin: 2px 0 0; padding: 9px 12px; border-left: 3px solid var(--color-warning); background: var(--color-warning-soft); color: var(--color-muted); }
.message-markdown code { border-radius: 4px; background: var(--color-line); padding: 1px 4px; font-family: var(--font-mono); font-size: 0.9em; }
.message-code-block { max-width: 100%; margin: 0; overflow-x: auto; border: 1px solid var(--color-line); border-radius: var(--radius-sm); background: var(--color-paper); padding: 12px; color: var(--color-muted); }
.message-code-block code { display: block; padding: 0; background: transparent; white-space: pre; }
.message-table-scroll { max-width: 100%; overflow-x: auto; }
.message-markdown table { width: 100%; min-width: 420px; border-collapse: collapse; font-size: 12px; }
.message-markdown th, .message-markdown td { padding: 8px 10px; border-bottom: 1px solid var(--color-line); text-align: left; vertical-align: top; }
.message-markdown th { background: var(--color-panel-soft); color: var(--color-ink); font-weight: 700; }
.message-markdown td { color: var(--color-muted); }
~~~

Do not change global pre, table, or button rules unless the scoped rules cannot override the current message layout.

- [x] **Step 2: Add narrow-screen safeguards**

Inside the existing mobile media query, keep table overflow local and body text readable:

~~~css
@media (max-width: 680px) {
  .message-markdown { font-size: 16px; }
  .message-markdown table { min-width: 420px; }
  .message-code-block { font-size: 12px; }
}
~~~

Only message-table-scroll and message-code-block may scroll horizontally; the conversation remains vertically scrollable.

### Task 5: Verify the finished change and commit it

**Files:**

- Modify: frontend/src/components/MarkdownMessage.tsx
- Modify: frontend/src/components/ConversationThread.tsx
- Modify: frontend/src/styles.css
- Test: frontend/src/__tests__/MarkdownMessage.test.tsx
- Test: frontend/src/__tests__/ConversationWorkspace.test.tsx

- [x] **Step 1: Run the complete frontend test suite**

From D:\RAG\better\frontend:

~~~powershell
npm test -- --run
~~~

Expected result: all existing and new tests PASS. If a test fails, identify whether it is a parser assertion, presentation-policy regression, or build issue before editing.

- [x] **Step 2: Run the TypeScript/Vite production build**

~~~powershell
npm run build
~~~

Expected result: TypeScript and Vite complete successfully and produce frontend/dist. Do not commit generated frontend/dist files if they are ignored or local build output.

- [x] **Step 3: Inspect the final diff and repository hygiene**

~~~powershell
git diff --check
git status --short
git diff --stat
~~~

Expected result: only the renderer, conversation wiring, scoped styles, and tests are present; no data, secrets, memory, eval output, or temporary visualization content is staged.

- [x] **Step 4: Commit the implementation**

~~~powershell
git add frontend/src/components/MarkdownMessage.tsx frontend/src/components/ConversationThread.tsx frontend/src/styles.css frontend/src/__tests__/MarkdownMessage.test.tsx frontend/src/__tests__/ConversationWorkspace.test.tsx
git commit -m "feat: render readable markdown messages"
~~~

Expected result: a focused commit on codex/personal-agent-v1, with main history untouched.

## Plan self-review

- Spec coverage: safe semantic rendering, supported Markdown subset, valid-table detection, JSON/streaming policy preservation, responsive styling, error fallback, tests, build verification, and non-goals are covered by Tasks 1–5.
- Placeholder scan: no unfinished or unspecified steps remain; every implementation action names a file, behavior, command, and expected result.
- Type consistency: MarkdownMessage accepts content and optional className; ConversationThread passes strings from MessagePresentation; parser output is React nodes; tests use the existing Vitest/jsdom setup and no new dependency.
