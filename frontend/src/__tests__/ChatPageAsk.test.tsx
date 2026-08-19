import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatPage from "../pages/ChatPage";

const api = vi.hoisted(() => ({
  answerAsk: vi.fn(),
  cancelTurn: vi.fn(),
  getThread: vi.fn(),
  getThreadEvents: vi.fn(),
  getThreadMessages: vi.fn(),
  subscribeToThreadEvents: vi.fn(),
  getSkills: vi.fn(),
  getEvents: vi.fn(),
  getMessages: vi.fn(),
  getStats: vi.fn(),
  subscribeToEvents: vi.fn(),
  submitTurn: vi.fn(),
  createThread: vi.fn(),
  createGoal: vi.fn(),
  sendMessage: vi.fn(),
  getRun: vi.fn(),
  selectDirection: vi.fn(),
  approvePlan: vi.fn(),
  cancelRun: vi.fn(),
  continueOutcome: vi.fn(),
  getPlans: vi.fn(),
  grantApproval: vi.fn(),
  rejectApproval: vi.fn(),
  resumeRun: vi.fn(),
  addBudget: vi.fn(),
}));

vi.mock("../api", () => api);

afterEach(cleanup);

describe("ChatPage ask flow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getThread.mockResolvedValue({
      id: "thread-ask",
      title: "Chat",
      version: 3,
      active_turn_id: "turn-ask",
      next_event_seq: 6,
      turns: [{
        id: "turn-ask",
        thread_id: "thread-ask",
        client_turn_id: "client-ask",
        parent_turn_id: null,
        status: "AWAITING_INPUT",
        policy: "ask",
        content_shape: "ask",
        reason_code: "model_requested_input",
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
      event_id: "ask-requested",
      seq: 5,
      thread_id: "thread-ask",
      turn_id: "turn-ask",
      type: "ask.requested",
      occurred_at: "2026-08-19T00:00:01Z",
      actor: "model",
      data: {
        ask_id: "ask-1",
        questions: [{
          id: "goal",
          header: "目标",
          question: "你想优先实现什么？",
          options: [{ label: "计划", description: "先生成方案" }],
          multi_select: false,
          allow_free_text: false,
        }],
      },
    }] });
    api.getThreadMessages.mockResolvedValue({ messages: [] });
    api.subscribeToThreadEvents.mockReturnValue(() => undefined);
    api.getSkills.mockResolvedValue({ skills: [] });
    api.getEvents.mockResolvedValue({ events: [] });
    api.getMessages.mockResolvedValue({ messages: [] });
    api.getStats.mockResolvedValue(null);
    api.subscribeToEvents.mockReturnValue(() => undefined);
    api.answerAsk.mockResolvedValue({ ask_id: "ask-1", turn: { id: "turn-2" } });
    api.cancelTurn.mockResolvedValue({ id: "turn-ask", status: "CANCELLED" });
  });

  it("answers a pending ask through the structured endpoint and can stop it", async () => {
    render(
      <ChatPage
        csrfToken="csrf"
        run={null}
        threadId="thread-ask"
        onRun={vi.fn()}
        onOpenTrajectory={vi.fn()}
        onOpenPlan={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByRole("region", { name: "等待你的回答" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "目标：计划" }));
    fireEvent.click(screen.getByRole("button", { name: "提交回答" }));

    await waitFor(() => expect(api.answerAsk).toHaveBeenCalledWith(
      "turn-ask",
      expect.objectContaining({
        expected_version: 2,
        answers: [{ question_id: "goal", selected_options: ["计划"], free_text: "" }],
      }),
      "csrf",
    ));

    fireEvent.click(within(screen.getByRole("region", { name: "等待你的回答" })).getByRole("button", { name: "停止询问" }));
    await waitFor(() => expect(api.cancelTurn).toHaveBeenCalledWith("turn-ask", "csrf"));
  });
});
