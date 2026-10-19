import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TrajectoryPage from "../pages/TrajectoryPage";
import type { AgentRun } from "../types";

const api = vi.hoisted(() => ({
  getTrace: vi.fn(),
  getAgentRun: vi.fn(),
  getAgentTasks: vi.fn(),
  getAgentArtifacts: vi.fn(),
  cancelAgentRun: vi.fn(),
}));

vi.mock("../api", () => api);

afterEach(cleanup);

const expertRun: AgentRun = {
  id: "expert-1",
  thread_id: "thread-1",
  objective: "核对实验记录",
  mode: "expert",
  status: "QUEUED",
  runtime_bundle_id: "bundle-1",
  budget_units: 4,
  reserved_budget_units: 0,
  version: 1,
  cancel_requested_at: null,
  created_at: "2026-08-20T00:00:00Z",
  updated_at: "2026-08-20T00:00:00Z",
  finished_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getTrace.mockResolvedValue({
    scope: { thread_id: "thread-1" },
    count: 2,
    next_cursor: null,
    events: [
      {
        event_id: "thread-event-1",
        seq: 2,
        type: "message.completed",
        occurred_at: "2026-08-20T00:00:01Z",
        actor: "worker",
        attempt: 2,
        causation_event_id: "parent-missing",
        outcome: {
          schema_version: 1,
          scope: "model",
          status: "reconciliation_required",
          error: {
            code: "MODEL_ERROR",
            phase: "model",
            retryable: false,
            public_message: "模型没有返回可用结果。",
            diagnostic_ref: null,
            attempts: 2,
          },
          reason: null,
          effect: "unknown",
          reconciliation_ref: "recon-1",
        },
        compatibility: "legacy_context_missing",
        payload: { token: "sekrit-token" },
      },
      {
        event_id: "approval-1",
        seq: 3,
        type: "approval.rejected",
        occurred_at: "2026-08-20T00:00:02Z",
        actor: "user",
      },
    ],
    completeness: {
      status: "partial",
      reasons: [
        { code: "source_not_connected", sources: ["archive"] },
        { code: "historical_unlinked" },
        { code: "truncated" },
      ],
    },
    legacy_scan_truncated: true,
  });
  api.getAgentRun.mockResolvedValue(expertRun);
  api.getAgentTasks.mockResolvedValue({ tasks: [] });
  api.getAgentArtifacts.mockResolvedValue({ artifacts: [] });
});

describe("trajectory page", () => {
  it("renders the unified trace, partial reasons and the expert card", async () => {
    render(<TrajectoryPage run={null} threadId="thread-1" expertRun={expertRun} />);

    await waitFor(() => expect(screen.getByRole("region", { name: "统一轨迹" })).toBeTruthy());
    expect(screen.getByText("回答生成完成")).toBeTruthy();
    expect(screen.getByText(/第 2 次尝试/)).toBeTruthy();
    expect(screen.getByText(/结果未知/)).toBeTruthy();
    expect(screen.getByText(/失败阶段 model/)).toBeTruthy();
    expect(screen.getByText(/不能重试/)).toBeTruthy();
    expect(screen.getByText(/引用父事件 parent-missing，该事件尚未加载/)).toBeTruthy();
    expect(screen.getByText(/历史事件缺少可关联的上下文/)).toBeTruthy();
    expect(screen.getByText("审批已拒绝")).toBeTruthy();
    expect(screen.getByText("这些来源尚未接入：archive")).toBeTruthy();
    expect(screen.getByText("有历史事件无法关联到当前轨迹")).toBeTruthy();
    expect(screen.getByText("这次扫描尚未读完，后面可能还有记录")).toBeTruthy();
    expect(screen.queryByText("sekrit-token")).toBeNull();
    expect(screen.getByRole("region", { name: "专家协同任务" })).toBeTruthy();
    expect(api.getAgentTasks).toHaveBeenCalledWith("expert-1");
  });

  it("starts a new trace query when the reader asks to refresh", async () => {
    render(<TrajectoryPage run={null} threadId="thread-1" />);
    await waitFor(() => expect(api.getTrace).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "重新读取" }));
    await waitFor(() => expect(api.getTrace.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(api.getTrace.mock.calls.at(-1)?.[1]).toBeNull();
  });
});
