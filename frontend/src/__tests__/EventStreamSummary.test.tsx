import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import EventStream from "../components/EventStream";
import type { ThreadEvent } from "../types";

function event(type: string, seq: number, data: Record<string, unknown> = {}): ThreadEvent {
  return {
    schema_version: 1,
    event_id: `event-${seq}`,
    seq,
    thread_id: "thread-1",
    turn_id: "turn-1",
    type,
    occurred_at: `2026-08-18T00:00:0${seq}Z`,
    actor: "worker",
    data,
  };
}

describe("event stream summary", () => {
  it("hides noisy events by default and exposes them in debug mode", () => {
    render(<EventStream mode="thread" events={[]} threadEvents={[
      event("turn.accepted", 1),
      event("message.delta", 2, { delta: "partial" }),
      event("turn.metrics.updated", 3, {
        queue_wait_ms: 320, context_ms: 40, model_ttft_ms: 1234, stream_ms: 2100, total_ms: 3694,
      }),
    ]} />);

    expect(screen.queryByText("回答正在实时生成")).toBeNull();
    expect(screen.getByText("0.32s")).toBeTruthy();
    expect(screen.getByText("1.23s")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "调试" }));
    expect(screen.getByText("回答正在实时生成")).toBeTruthy();
    expect(screen.getAllByText("查看原始事件").length).toBeGreaterThan(0);
  });
});
