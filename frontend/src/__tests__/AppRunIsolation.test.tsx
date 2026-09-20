import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";
import { navigateTo } from "../navigation";
import type { Run } from "../types";

interface ChatProbeProps {
  run: Run | null;
  threadId?: string | null;
  sourceActionId?: string | null;
  onRun: (run: Run) => void;
  onThread: (id: string) => void;
}

const probes = vi.hoisted(() => ({ chat: null as ChatProbeProps | null }));
const api = vi.hoisted(() => ({ getBootstrap: vi.fn(), listThreads: vi.fn(), getLatestExpertRun: vi.fn() }));

vi.mock("../api", async importOriginal => ({ ...await importOriginal<typeof import("../api")>(), ...api }));
vi.mock("../pages/ChatPage", () => ({ default: (props: ChatProbeProps) => {
  probes.chat = props;
  return <section><output data-testid="chat-run">{props.run?.id ?? "none"}</output><output data-testid="chat-thread">{props.threadId ?? "new"}</output><output data-testid="chat-action">{props.sourceActionId ?? "none"}</output></section>;
} }));
vi.mock("../pages/GoalWorkspacePage", () => ({ default: () => <section data-testid="goal-library" /> }));
vi.mock("../pages/TrajectoryPage", () => ({ default: ({ run }: { run: Run | null }) => <output data-testid="trajectory-run">{run?.id ?? "none"}</output> }));

const runA: Run = {
  id: "run-a", goal_id: "goal-a", session_id: "session-a", state: "RECEIVED", resume_state: null,
  current_plan_version_id: null, current_step_id: null, version: 1, budget: {}, pending_approvals: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  probes.chat = null;
  window.history.replaceState({}, "", "/threads/thread-a");
  api.getBootstrap.mockResolvedValue({ csrf_token: "csrf", api_key_configured: true });
  api.getLatestExpertRun.mockResolvedValue(null);
  api.listThreads.mockResolvedValue({ threads: ["a", "b"].map(id => ({
    id: `thread-${id}`, title: `${id.toUpperCase()} 会话`, updated_at: "2026-09-12T00:00:00Z",
  })) });
});

afterEach(() => {
  cleanup();
  window.history.replaceState({}, "", "/");
});

async function renderWithRun() {
  render(<App />);
  await screen.findByRole("heading", { name: "A 会话" });
  const oldCallbacks = probes.chat!;
  act(() => oldCallbacks.onRun(runA));
  expect(screen.getByTestId("chat-run").textContent).toBe("run-a");
  return oldCallbacks;
}

it("clears the old run when starting a new goal and rejects late run and thread callbacks", async () => {
  const oldCallbacks = await renderWithRun();
  fireEvent.click(screen.getByRole("link", { name: "计划" }));
  expect(window.location.pathname).toBe("/workspace");
  expect(await screen.findByRole("navigation", {name:"计划视图"}, { timeout: 8000 })).toBeTruthy();
  act(() => oldCallbacks.onRun({ ...runA, id: "late-run-a" }));
  fireEvent.click(screen.getByRole("button", { name: "新建计划" }));
  expect(window.location.pathname).toBe("/");
  expect(screen.getByTestId("chat-run").textContent).toBe("none");
  expect(screen.getByTestId("chat-thread").textContent).toBe("new");

  await act(async () => {
    oldCallbacks.onRun({ ...runA, id: "later-run-a" });
    oldCallbacks.onThread("late-created-thread-a");
  });
  expect(window.location.pathname).toBe("/");
  expect(screen.getByTestId("chat-run").textContent).toBe("none");
  expect(screen.getByTestId("chat-thread").textContent).toBe("new");
});

it("clears a run on a different thread URL and accepts updates only from the current thread", async () => {
  const oldCallbacks = await renderWithRun();
  act(() => navigateTo("/threads/thread-b"));
  expect(screen.getByTestId("chat-thread").textContent).toBe("thread-b");
  expect(screen.getByTestId("chat-run").textContent).toBe("none");
  const currentCallbacks = probes.chat!;
  act(() => {
    oldCallbacks.onRun({ ...runA, id: "late-run-a" });
    currentCallbacks.onRun({ ...runA, id: "run-b", goal_id: "goal-b" });
  });
  expect(screen.getByTestId("chat-run").textContent).toBe("run-b");
  await act(async () => oldCallbacks.onThread("thread-a"));
  expect(window.location.pathname).toBe("/threads/thread-b");
  expect(screen.getByTestId("chat-run").textContent).toBe("run-b");
});

it("isolates runs when browser history changes the selected thread", async () => {
  const oldCallbacks = await renderWithRun();
  act(() => {
    window.history.replaceState({}, "", "/threads/thread-b");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  expect(screen.getByTestId("chat-thread").textContent).toBe("thread-b");
  expect(screen.getByTestId("chat-run").textContent).toBe("none");
  act(() => oldCallbacks.onRun(runA));
  expect(screen.getByTestId("chat-run").textContent).toBe("none");
});

it("preserves the same thread run and action origin across activity and back to conversation", async () => {
  window.history.replaceState({}, "", "/threads/thread-a?action=action-a&from=today");
  await renderWithRun();
  expect(screen.getByTestId("chat-action").textContent).toBe("action-a");
  fireEvent.click(screen.getByRole("button", { name: "运行详情" }));
  expect(window.location.pathname).toBe("/threads/thread-a");
  expect(new URLSearchParams(window.location.search).get("view")).toBe("activity");
  expect(new URLSearchParams(window.location.search).get("action")).toBe("action-a");
  expect(new URLSearchParams(window.location.search).get("from")).toBe("today");
  expect((await screen.findByTestId("trajectory-run", undefined, { timeout: 8000 })).textContent).toBe("run-a");

  fireEvent.click(screen.getByRole("button", { name: "返回对话" }));
  await waitFor(() => expect(screen.getByTestId("chat-run").textContent).toBe("run-a"));
  expect(screen.getByTestId("chat-thread").textContent).toBe("thread-a");
  expect(screen.getByTestId("chat-action").textContent).toBe("action-a");
  expect(new URLSearchParams(window.location.search).has("view")).toBe(false);
  expect(new URLSearchParams(window.location.search).get("from")).toBe("today");
});
