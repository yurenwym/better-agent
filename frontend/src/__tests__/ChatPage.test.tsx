import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatPage from "../pages/ChatPage";
import type { Run } from "../types";

const api = vi.hoisted(() => ({
  createGoal: vi.fn(),
  getSkills: vi.fn(),
  getRun: vi.fn(),
  sendMessage: vi.fn(),
  getEvents: vi.fn(),
  getMessages: vi.fn(),
  getStats: vi.fn(),
  subscribeToEvents: vi.fn(),
  addBudget: vi.fn(),
  approvePlan: vi.fn(),
  cancelRun: vi.fn(),
  continueOutcome: vi.fn(),
  getPlans: vi.fn(),
  grantApproval: vi.fn(),
  rejectApproval: vi.fn(),
  resumeRun: vi.fn(),
}));

vi.mock("../api", () => api);

afterEach(cleanup);

const initialRun: Run = {
  id: "run-1",
  goal_id: "goal-1",
  session_id: "session-1",
  state: "RECEIVED",
  resume_state: null,
  current_plan_version_id: null,
  current_step_id: null,
  version: 0,
  budget: {},
  pending_approvals: [],
};

describe("ChatPage streaming bootstrap", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.createGoal.mockResolvedValue({ id: "goal-1", run_id: "run-1", state: "RECEIVED" });
    api.getSkills.mockResolvedValue({ skills: [{ name: "reflection", title: "执行复盘", description: "从结果中提炼记忆。", enabled: true }] });
    api.getRun.mockResolvedValue(initialRun);
    api.sendMessage.mockResolvedValue({ ...initialRun, state: "AWAITING_APPROVAL", version: 1 });
    api.getEvents.mockResolvedValue({ events: [] });
    api.getMessages.mockResolvedValue({ messages: [] });
    api.getStats.mockResolvedValue({ run_id: "run-1" });
    api.subscribeToEvents.mockReturnValue(() => undefined);
  });

  it("mounts the new run before sending its first message", async () => {
    render(
      <ChatPage
        csrfToken="csrf"
        run={null}
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByRole("textbox", { name: "输入消息" }), { target: { value: "Ship it" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(api.sendMessage).toHaveBeenCalled());
    expect(api.getRun).toHaveBeenCalledWith("run-1");
    expect(api.getRun.mock.invocationCallOrder[0]).toBeLessThan(api.sendMessage.mock.invocationCallOrder[0]);
  });

  it("does not expose budget recovery during ordinary execution", () => {
    render(
      <ChatPage
        csrfToken="csrf"
        run={{ ...initialRun, state: "EXECUTING", budget: { react_iterations_remaining: 4, react_iteration: 1 } }}
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "继续执行一次" })).toBeNull();
    expect(screen.queryByRole("button", { name: "追加 1 轮预算" })).toBeNull();
  });

  it("keeps a visible cancel task action available during execution", async () => {
    const onRun = vi.fn();
    api.cancelRun.mockResolvedValue({ ...initialRun, state: "CANCELLED" });

    render(
      <ChatPage
        csrfToken="csrf"
        run={{ ...initialRun, state: "EXECUTING" }}
        onRun={onRun}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));

    await waitFor(() => expect(api.cancelRun).toHaveBeenCalledWith("run-1", "csrf"));
    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({ state: "CANCELLED" }));
  });

  it("offers one-step recovery only after a react budget block", async () => {
    const blockedRun: Run = {
      ...initialRun,
      state: "BLOCKED",
      budget: {
        react_iterations_remaining: 0,
        react_iteration: 5,
        blocked_reason: "react iteration budget exhausted",
      },
    };
    api.addBudget.mockResolvedValue({ ...blockedRun, budget: { ...blockedRun.budget, react_iterations_remaining: 1 } });
    api.resumeRun.mockResolvedValue({ ...blockedRun, state: "EXECUTING" });

    render(
      <ChatPage
        csrfToken="csrf"
        run={blockedRun}
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    expect(screen.getByText("Agent 已达到当前步骤的安全保护阈值")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "继续执行一次" }));

    await waitFor(() => expect(api.resumeRun).toHaveBeenCalledWith(blockedRun.id, "csrf"));
    expect(api.addBudget).toHaveBeenCalledWith(blockedRun.id, 1, "csrf");
    expect(api.addBudget.mock.invocationCallOrder[0]).toBeLessThan(api.resumeRun.mock.invocationCallOrder[0]);
  });

  it("sends the selected skills with the current conversation message", async () => {
    render(
      <ChatPage
        csrfToken="csrf"
        run={initialRun}
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /技能/ }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "执行复盘" })).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "执行复盘" }));
    fireEvent.change(screen.getByRole("textbox", { name: "输入消息" }), { target: { value: "继续" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(api.sendMessage).toHaveBeenCalledWith("goal-1", "继续", "csrf", ["reflection"]));
  });

  it("clears selected skills when the user starts a new conversation", async () => {
    const props = {
      csrfToken: "csrf",
      onRun: vi.fn(),
      onOpenTrajectory: vi.fn(),
      onOpenPlan: vi.fn(),
    };
    const view = render(<ChatPage {...props} run={initialRun} />);

    fireEvent.click(screen.getByRole("button", { name: /技能/ }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "执行复盘" })).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: "执行复盘" }));
    expect((screen.getByRole("checkbox", { name: "执行复盘" }) as HTMLInputElement).checked).toBe(true);

    view.rerender(<ChatPage {...props} run={null} />);

    await waitFor(() => expect((screen.getByRole("checkbox", { name: "执行复盘" }) as HTMLInputElement).checked).toBe(false));
  });
});
