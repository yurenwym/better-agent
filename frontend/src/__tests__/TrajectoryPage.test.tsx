import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TrajectoryPage from "../pages/TrajectoryPage";

const api = vi.hoisted(() => ({
  getThread: vi.fn(),
  getThreadEvents: vi.fn(),
  getThreadMessages: vi.fn(),
  subscribeToThreadEvents: vi.fn(),
}));

vi.mock("../api", () => api);

afterEach(cleanup);

describe("trajectory page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getThread.mockResolvedValue({
      id: "thread-1",
      title: "骑行计划",
      version: 2,
      active_turn_id: "turn-1",
      next_event_seq: 3,
      turns: [{ id: "turn-1", status: "COMPLETED" }],
    });
    api.getThreadEvents.mockResolvedValue({ events: [{
      schema_version: 1,
      event_id: "thread-event-1",
      seq: 2,
      thread_id: "thread-1",
      turn_id: "turn-1",
      type: "message.completed",
      occurred_at: "2026-08-20T00:00:01Z",
      actor: "worker",
      data: {},
    }] });
    api.getThreadMessages.mockResolvedValue({ messages: [] });
    api.subscribeToThreadEvents.mockReturnValue(() => undefined);
  });

  it("renders the current thread events when opened without a materialized run", async () => {
    render(<TrajectoryPage run={null} threadId="thread-1" />);

    await waitFor(() => expect(screen.getByRole("region", { name: "对话时间线" })).toBeTruthy());
    expect(screen.getByText("回答生成完成")).toBeTruthy();
    expect(screen.getByText("1 个关键节点，原始事件仍保留在调试视图")).toBeTruthy();
  });
});
