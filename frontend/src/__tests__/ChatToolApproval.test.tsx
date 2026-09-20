import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ChatPage from "../pages/ChatPage";
import ChatToolApprovalCard from "../components/ChatToolApprovalCard";
import type { ChatToolCall } from "../types";

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
  getSkills: vi.fn().mockResolvedValue({ skills: [] }),
  getResearchJobs: vi.fn().mockResolvedValue({ jobs: [] }),
  getGoalAction: vi.fn(),
  cancelResearch: vi.fn(),
  retryResearch: vi.fn(),
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
  saveMessagePlan: vi.fn(),
  getTurnToolCall: vi.fn(),
  decideTurnToolCall: vi.fn(),
}));

vi.mock("../api", () => api);

afterEach(() => { cleanup(); window.history.replaceState({}, "", "/"); });

const draftCall: ChatToolCall = {
  id: "chat-tool-1",
  turn_id: "turn-1",
  tool_name: "create_plan_draft",
  params: { title: "桂林旅游攻略", markdown_content: "# 桂林\n第一天：象鼻山" },
  risk: "WRITE",
  status: "PENDING_APPROVAL",
  approval_id: "approval-1",
  binding: {},
  error_code: null,
  continuation_turn_id: null,
  created_at: "2026-09-20T00:00:00Z",
  acted_at: null,
};

const activationCall: ChatToolCall = {
  ...draftCall,
  id: "chat-tool-2",
  tool_name: "activate_goal_plan",
  params: { mode: "activate", program_id: "program-1", expected_version: 1 },
  binding: {
    goal_activation: {
      snapshot_hash: "hash-1",
      program_version: 1,
      preview: {
        objective_title: "五周训练计划",
        start_date: "2026-09-01",
        end_date: "2026-09-05",
        timezone: "Asia/Shanghai",
        daily_minutes: 60,
        structure: { actions: [{ title: "力量训练", scheduled_date: "2026-09-01", estimated_minutes: 60 }] },
      },
    },
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getResearchJobs.mockResolvedValue({ jobs: [] });
  api.getSkills.mockResolvedValue({ skills: [] });
  api.getThreadEvents.mockResolvedValue({ events: [] });
  api.getThreadMessages.mockResolvedValue({ messages: [] });
  api.subscribeToThreadEvents.mockReturnValue(() => undefined);
  api.getTurnToolCall.mockResolvedValue(draftCall);
  api.decideTurnToolCall.mockResolvedValue({
    id: "turn-2", thread_id: "thread-1", client_turn_id: "c", parent_turn_id: "turn-1",
    status: "ACCEPTED", policy: null, content_shape: null, reason_code: null, version: 0,
    materialized_goal_id: null, materialized_run_id: null, direction_action: null,
    direction_idempotency_key: null, created_at: "", updated_at: "",
  });
  api.getThread.mockResolvedValue({
    id: "thread-1", title: "Chat", version: 2, active_turn_id: "turn-1", next_event_seq: 5,
    turns: [{
      id: "turn-1", thread_id: "thread-1", client_turn_id: "client-1", parent_turn_id: null,
      status: "AWAITING_TOOL_APPROVAL", policy: null, content_shape: null, reason_code: null,
      version: 3, skill_names: [], materialized_goal_id: null, materialized_run_id: null,
      direction_action: null, direction_idempotency_key: null,
      created_at: "2026-09-20T00:00:00Z", updated_at: "2026-09-20T00:00:00Z",
    }],
  });
});

it("shows the exact write content and locks the composer while awaiting approval", async () => {
  render(<ChatPage csrfToken="csrf" threadId="thread-1" run={null} onRun={vi.fn()} onOpenPlan={vi.fn()} onOpenTrajectory={vi.fn()} />);

  expect(await screen.findByText("保存计划文档 · 需要确认")).toBeTruthy();
  expect(screen.getByText("桂林旅游攻略")).toBeTruthy();
  fireEvent.click(screen.getByText("查看将保存的内容"));
  expect(await screen.findByText(/第一天：象鼻山/)).toBeTruthy();
  expect((screen.getByLabelText("输入消息") as HTMLTextAreaElement).disabled).toBe(true);
});

it("renders the compiled schedule bound to the activation approval", async () => {
  api.getTurnToolCall.mockResolvedValue(activationCall);
  render(<ChatPage csrfToken="csrf" threadId="thread-1" run={null} onRun={vi.fn()} onOpenPlan={vi.fn()} onOpenTrajectory={vi.fn()} />);

  expect(await screen.findByText("开启执行管理 · 需要确认")).toBeTruthy();
  expect(screen.getByText("五周训练计划")).toBeTruthy();
  expect(screen.getByText(/2026-09-01 至 2026-09-05/)).toBeTruthy();
  expect(screen.getByText(/力量训练/)).toBeTruthy();
});

it("shows compile-preview settings instead of a generic prompt", () => {
  const previewCall: ChatToolCall = {
    ...draftCall,
    id: "chat-tool-preview",
    tool_name: "activate_goal_plan",
    params: {
      mode: "preview",
      document_id: "plan_abc",
      expected_document_version_id: "planv_v1",
      start_date: "2026-09-01",
      end_date: "2026-09-05",
      timezone: "Asia/Shanghai",
      daily_minutes: 45,
      constraints: { available_weekdays: [0, 2, 4], excluded_dates: ["2026-09-03"] },
    },
  };
  render(<ChatToolApprovalCard toolCall={previewCall} onApprove={vi.fn()} onReject={vi.fn()} onRefresh={vi.fn()} />);

  expect(screen.getByText("生成执行预览 · 需要确认")).toBeTruthy();
  expect(screen.getByText("plan_abc")).toBeTruthy();
  expect(screen.getByText("planv_v1")).toBeTruthy();
  expect(screen.getByText("2026-09-01")).toBeTruthy();
  expect(screen.getByText("2026-09-05")).toBeTruthy();
  expect(screen.getByText("Asia/Shanghai")).toBeTruthy();
  expect(screen.getByText("45 分钟")).toBeTruthy();
  expect(screen.getByText("周一、周三、周五")).toBeTruthy();
  expect(screen.getByText("2026-09-03")).toBeTruthy();
  expect(screen.getByText(/仍需你再次确认具体安排后才会激活/)).toBeTruthy();
});

it("shows cleared feedback fields for a correction", () => {
  const correctionCall: ChatToolCall = {
    ...draftCall,
    id: "chat-tool-correction",
    tool_name: "record_action_feedback",
    params: {
      action_id: "action_1",
      expected_version: 2,
      kind: "correction",
      cleared_fields: ["actual_minutes", "note"],
    },
  };
  render(<ChatToolApprovalCard toolCall={correctionCall} onApprove={vi.fn()} onReject={vi.fn()} onRefresh={vi.fn()} />);

  expect(screen.getByText("更正已有记录")).toBeTruthy();
  expect(screen.getByText("实际时长、备注")).toBeTruthy();
});

it("renders deferral facts without feedback wording", () => {
  const deferCall: ChatToolCall = {
    ...draftCall,
    id: "chat-tool-defer",
    tool_name: "defer_action",
    params: { action_id: "action_9", expected_version: 4, scheduled_date: "2026-09-25" },
  };
  render(<ChatToolApprovalCard toolCall={deferCall} onApprove={vi.fn()} onReject={vi.fn()} onRefresh={vi.fn()} />);

  expect(screen.getByText("延期行动 · 需要确认")).toBeTruthy();
  expect(screen.getByText("把行动改到 2026-09-25")).toBeTruthy();
  expect(screen.getByText("action_9")).toBeTruthy();
  expect(screen.queryByText("记录部分进展")).toBeNull();
  expect(screen.queryByText("记录你亲自陈述的进展")).toBeNull();
});

it("decides through the API and replays the same key after a failure", async () => {
  api.decideTurnToolCall.mockRejectedValueOnce(new Error("网络错误"));
  render(<ChatPage csrfToken="csrf" threadId="thread-1" run={null} onRun={vi.fn()} onOpenPlan={vi.fn()} onOpenTrajectory={vi.fn()} />);
  await screen.findByText("保存计划文档 · 需要确认");

  fireEvent.click(screen.getByRole("button", { name: "批准" }));
  await screen.findByText("网络错误");

  fireEvent.click(screen.getByRole("button", { name: "批准" }));
  await waitFor(() => expect(api.decideTurnToolCall).toHaveBeenCalledTimes(2));

  expect(api.decideTurnToolCall.mock.calls[0][1]).toEqual({
    action: "approve", expected_version: 3, idempotency_key: expect.any(String),
  });
  expect(api.decideTurnToolCall.mock.calls[1][1].idempotency_key)
    .toBe(api.decideTurnToolCall.mock.calls[0][1].idempotency_key);
});

it("shows the modified document content and the version it is based on", () => {
  const modifyCall: ChatToolCall = {
    ...draftCall,
    id: "chat-tool-modify",
    tool_name: "modify_plan_document",
    params: {
      document_id: "plan_doc",
      expected_version_id: "planv_v3",
      title: "攻略（减量）",
      markdown_content: "# 攻略\n\n- 减少一个景点",
    },
  };
  render(<ChatToolApprovalCard toolCall={modifyCall} onApprove={vi.fn()} onReject={vi.fn()} onRefresh={vi.fn()} />);

  expect(screen.getByText("修改计划文档 · 需要确认")).toBeTruthy();
  expect(screen.getByText("攻略（减量）")).toBeTruthy();
  expect(screen.getByText(/文档 plan_doc，基于版本 planv_v3/)).toBeTruthy();
  expect(screen.getByText(/不会自动重编译/)).toBeTruthy();
  fireEvent.click(screen.getByText("查看修改后的内容"));
  expect(screen.getByText(/减少一个景点/)).toBeTruthy();
});

it("offers refresh and rejection without granting", async () => {
  const onApprove = vi.fn();
  const onReject = vi.fn();
  const onRefresh = vi.fn();
  render(<ChatToolApprovalCard toolCall={draftCall} onApprove={onApprove} onReject={onReject} onRefresh={onRefresh} />);

  fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
  fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
  expect(onRefresh).toHaveBeenCalledTimes(1);
  expect(onReject).toHaveBeenCalledTimes(1);
  expect(onApprove).not.toHaveBeenCalled();
});
