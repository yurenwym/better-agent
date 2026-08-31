import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatPage from "../pages/ChatPage";
import type { Run } from "../types";

const api = vi.hoisted(() => ({
  createGoal: vi.fn(),
  createThread: vi.fn(),
  submitTurn: vi.fn(),
  getThread: vi.fn(),
  getThreadEvents: vi.fn(),
  getThreadMessages: vi.fn(),
  subscribeToThreadEvents: vi.fn(),
  cancelTurn: vi.fn(),
  selectDirection: vi.fn(),
  getSkills: vi.fn(),
  getResearchJobs: vi.fn().mockResolvedValue({ jobs: [] }),
  getGoalAction: vi.fn(),
  cancelResearch: vi.fn(),
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
  createExpertRun: vi.fn(),
  getAgentRun: vi.fn(),
  getAgentTasks: vi.fn(),
  getAgentArtifacts: vi.fn(),
  cancelAgentRun: vi.fn(),
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
    api.createThread.mockResolvedValue({ id: "thread-1", title: "Chat", version: 0, active_turn_id: null, next_event_seq: 1, turns: [] });
    api.submitTurn.mockResolvedValue({ thread_id: "thread-1", turn_id: "turn-1", status: "ACCEPTED", version: 0, event_cursor: 1 });
    api.getThread.mockResolvedValue({ id: "thread-1", title: "Chat", version: 1, active_turn_id: "turn-1", next_event_seq: 2, turns: [{ id: "turn-1", thread_id: "thread-1", client_turn_id: "client-1", parent_turn_id: null, status: "ACCEPTED", policy: null, content_shape: null, reason_code: null, version: 0, materialized_goal_id: null, materialized_run_id: null, direction_action: null, direction_idempotency_key: null, created_at: "2026-08-19T00:00:00Z", updated_at: "2026-08-19T00:00:00Z" }] });
    api.getThreadEvents.mockResolvedValue({ events: [] });
    api.getThreadMessages.mockResolvedValue({ messages: [] });
    api.subscribeToThreadEvents.mockReturnValue(() => undefined);
    api.getSkills.mockResolvedValue({ skills: [{ name: "reflection", title: "执行复盘", description: "从结果中提炼记忆。", enabled: true }] });
    api.getRun.mockResolvedValue(initialRun);
    api.getGoalAction.mockResolvedValue({action:{scheduled_date:"2026-09-01",title:"完成一道题"},program:{objective_title:"一周力扣"}});
    api.sendMessage.mockResolvedValue({ ...initialRun, state: "AWAITING_APPROVAL", version: 1 });
    api.getEvents.mockResolvedValue({ events: [] });
    api.getMessages.mockResolvedValue({ messages: [] });
    api.getStats.mockResolvedValue({ run_id: "run-1" });
    api.subscribeToEvents.mockReturnValue(() => undefined);
    api.createExpertRun.mockResolvedValue({ id: "agent-run-1", thread_id: "thread-1", objective: "深入比较", mode: "expert", status: "QUEUED", runtime_bundle_id: "bundle-1", budget_units: 16, reserved_budget_units: 0, version: 0, cancel_requested_at: null, created_at: "2026-08-24T00:00:00Z", updated_at: "2026-08-24T00:00:00Z", finished_at: null });
    api.getAgentRun.mockImplementation(async () => api.createExpertRun.mock.results.at(-1)?.value);
    api.getAgentTasks.mockResolvedValue({ tasks: [] });
    api.getAgentArtifacts.mockResolvedValue({ artifacts: [] });
  });

  it("explicitly starts an expert run when deep processing is enabled", async () => {
    render(
      <ChatPage csrfToken="csrf" run={null} onThread={vi.fn()} onRun={vi.fn()} onOpenTrajectory={vi.fn()} onOpenPlan={vi.fn()} />,
    );

    fireEvent.click(screen.getByRole("switch", { name: "深入处理" }));
    fireEvent.change(screen.getByRole("textbox", { name: "输入消息" }), { target: { value: "深入比较" } });
    fireEvent.click(screen.getByRole("button", { name: "启动专家协同" }));

    await waitFor(() => expect(api.createExpertRun).toHaveBeenCalledWith("thread-1", "深入比较", expect.any(String), "csrf"));
    expect(api.submitTurn).not.toHaveBeenCalled();
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

  it("submits a new conversation turn without creating a goal", async () => {
    render(
      <ChatPage
        csrfToken="csrf"
        run={null}
        onThread={vi.fn()}
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByRole("textbox", { name: "输入消息" }), { target: { value: "普通问题" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(api.submitTurn).toHaveBeenCalledWith(
      "thread-1",
      expect.objectContaining({ content: "普通问题", skill_names: [] }),
      "csrf",
    ));
    expect(api.createGoal).not.toHaveBeenCalled();
  });

  it("shows the thread activity rail before a Run is materialized", async () => {
    api.getThread.mockResolvedValue({
      id: "thread-1",
      title: "Chat",
      version: 1,
      active_turn_id: "turn-1",
      next_event_seq: 3,
      turns: [{
        id: "turn-1",
        thread_id: "thread-1",
        client_turn_id: "client-1",
        parent_turn_id: null,
        status: "COMPLETED",
        policy: "answer",
        content_shape: "markdown",
        reason_code: "content_only",
        version: 2,
        skill_names: [],
        materialized_goal_id: null,
        materialized_run_id: null,
        direction_action: null,
        direction_idempotency_key: null,
        created_at: "2026-08-19T00:00:00Z",
        updated_at: "2026-08-19T00:00:01Z",
      }],
    });
    api.getThreadEvents.mockResolvedValue({ events: [{
      schema_version: 1,
      event_id: "event-2",
      seq: 2,
      thread_id: "thread-1",
      turn_id: "turn-1",
      type: "turn.completed",
      occurred_at: "2026-08-19T00:00:01Z",
      actor: "worker",
      data: {},
    }] });

    render(
      <ChatPage
        csrfToken="csrf"
        run={null}
        threadId="thread-1"
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByRole("complementary", { name: "当前对话轨迹" })).toBeTruthy());
    const rail = screen.getByRole("complementary", { name: "当前对话轨迹" });
    expect(rail.querySelector(".activity-list")?.textContent).toContain("本轮对话完成");
  });

  it("shows a dismissible context banner for an action-linked turn", async () => {
    api.getThread.mockResolvedValue({
      id:"thread-1",title:"Chat",version:1,active_turn_id:"turn-1",next_event_seq:2,
      turns:[{id:"turn-1",thread_id:"thread-1",client_turn_id:"goal-help",parent_turn_id:null,status:"ACCEPTED",policy:null,content_shape:null,reason_code:null,goal_action_id:"action-12345678",version:0,skill_names:[],materialized_goal_id:null,materialized_run_id:null,direction_action:null,direction_idempotency_key:null,created_at:"2026-08-19T00:00:00Z",updated_at:"2026-08-19T00:00:00Z"}],
    });
    render(<ChatPage csrfToken="csrf" run={null} threadId="thread-1" onRun={vi.fn()} onOpenTrajectory={vi.fn()} onOpenPlan={vi.fn()}/>);
    expect(await screen.findByRole("complementary",{name:"当前行动上下文"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"关闭行动上下文"}));
    expect(screen.queryByRole("complementary",{name:"当前行动上下文"})).toBeNull();
  });

  it("shows the document title and fixed version before execution confirmation", async () => {
    api.getThread.mockResolvedValue({
      id: "thread-1",
      title: "Chat",
      version: 1,
      active_turn_id: "turn-1",
      next_event_seq: 3,
      turns: [{
        id: "turn-1",
        thread_id: "thread-1",
        client_turn_id: "client-1",
        parent_turn_id: null,
        status: "AWAITING_DIRECTION",
        policy: "propose_execution",
        content_shape: "tracking",
        reason_code: "explicit_tracking",
        version: 1,
        skill_names: [],
        materialized_goal_id: null,
        materialized_run_id: null,
        direction_action: null,
        direction_idempotency_key: null,
        created_at: "2026-08-19T00:00:00Z",
        updated_at: "2026-08-19T00:00:01Z",
      }],
    });
    api.getThreadEvents.mockResolvedValue({ events: [{
      schema_version: 1,
      event_id: "event-context",
      seq: 2,
      thread_id: "thread-1",
      turn_id: "turn-1",
      type: "plan.context_loaded",
      occurred_at: "2026-08-19T00:00:01Z",
      actor: "worker",
      data: { title: "Training plan", version: 3, plan_document_id: "plan-1" },
    }] });

    render(
      <ChatPage
        csrfToken="csrf"
        run={null}
        threadId="thread-1"
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByRole("region", { name: "这项请求需要确认" }).textContent).toContain("Training plan · v3"));
    expect(screen.getByRole("textbox", { name: "输入消息" }).hasAttribute("disabled")).toBe(false);
  });

  it("keeps the conversation composer available while a materialized run awaits approval", () => {
    render(
      <ChatPage
        csrfToken="csrf"
        run={{ ...initialRun, state: "AWAITING_APPROVAL" }}
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    expect(screen.getByRole("textbox", { name: "输入消息" }).hasAttribute("disabled")).toBe(false);
  });

  it("routes follow-up messages through the conversation thread after execution materializes", async () => {
    api.getThread.mockResolvedValue({
      id: "thread-1",
      title: "Chat",
      version: 2,
      active_turn_id: "turn-1",
      next_event_seq: 2,
      turns: [{
        id: "turn-1",
        thread_id: "thread-1",
        client_turn_id: "client-1",
        parent_turn_id: null,
        status: "COMPLETED",
        policy: "answer",
        content_shape: "markdown",
        reason_code: "content_only",
        version: 1,
        skill_names: [],
        materialized_goal_id: null,
        materialized_run_id: null,
        direction_action: null,
        direction_idempotency_key: null,
        created_at: "2026-08-19T00:00:00Z",
        updated_at: "2026-08-19T00:00:01Z",
      }],
    });
    render(
      <ChatPage
        csrfToken="csrf"
        run={{ ...initialRun, state: "AWAITING_APPROVAL" }}
        threadId="thread-1"
        onThread={vi.fn()}
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByRole("textbox", { name: "输入消息" }).hasAttribute("disabled")).toBe(false));
    fireEvent.change(screen.getByRole("textbox", { name: "输入消息" }), { target: { value: "补充信息" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(api.submitTurn).toHaveBeenCalledWith(
      "thread-1",
      expect.objectContaining({ content: "补充信息" }),
      "csrf",
    ));
    expect(api.sendMessage).not.toHaveBeenCalled();
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
        blocked_reason_code: "REACT_ITERATION_BUDGET_EXHAUSTED",
        blocked_message: "本步骤的执行轮次已用完",
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

    expect(screen.getByText("本步骤的执行轮次已用完")).toBeTruthy();
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

  it("explains a pending ask conflict and offers to stop it while keeping the draft", async () => {
    api.submitTurn.mockRejectedValue(new Error("answer the pending ask before sending another message"));
    api.getThread.mockResolvedValue({
      id: "thread-1",
      title: "Chat",
      version: 2,
      active_turn_id: "turn-1",
      next_event_seq: 2,
      turns: [{
        id: "turn-1",
        thread_id: "thread-1",
        client_turn_id: "client-1",
        parent_turn_id: null,
        status: "AWAITING_INPUT",
        policy: "ask",
        content_shape: "ask",
        reason_code: "model_requested_input",
        version: 1,
        skill_names: [],
        materialized_goal_id: null,
        materialized_run_id: null,
        direction_action: null,
        direction_idempotency_key: null,
        created_at: "2026-08-19T00:00:00Z",
        updated_at: "2026-08-19T00:00:01Z",
      }],
    });
    api.getThreadEvents.mockResolvedValue({ events: [] });
    api.getThreadMessages.mockResolvedValue({ messages: [] });
    api.cancelTurn.mockResolvedValue({ id: "turn-1", status: "CANCELLED" });

    render(
      <ChatPage
        csrfToken="csrf"
        run={null}
        threadId="thread-1"
        onThread={vi.fn()}
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByRole("textbox", { name: "输入消息" }).hasAttribute("disabled")).toBe(false));
    fireEvent.change(screen.getByRole("textbox", { name: "输入消息" }), { target: { value: "力量训练计划" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("当前对话正在等待你的回答"));
    expect(screen.getByRole("alert").textContent).not.toContain("{\"detail\"");
    fireEvent.click(screen.getByRole("button", { name: "停止询问，保留当前输入" }));

    await waitFor(() => expect(api.cancelTurn).toHaveBeenCalledWith("turn-1", "csrf"));
    expect((screen.getByRole("textbox", { name: "输入消息" }) as HTMLTextAreaElement).value).toBe("力量训练计划");
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
