import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import EventStream from "../components/EventStream";
import type { ThreadEvent, TraceEvent } from "../types";

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

afterEach(cleanup);

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

  it("shows retry, unknown results, a missing parent and redacted history on one trace", () => {
    const events: TraceEvent[] = [
      {
        event_id: "approval-1",
        seq: 1,
        type: "approval.rejected",
        occurred_at: "2026-08-18T00:00:01Z",
        actor: "user",
      },
      {
        event_id: "retry-1",
        seq: 2,
        type: "tool.execution.finished",
        occurred_at: "2026-08-18T00:00:02Z",
        attempt: 3,
        causation_event_id: "not-loaded",
        outcome: {
          schema_version: 1,
          scope: "tool",
          status: "reconciliation_required",
          error: {
            code: "TOOL_EXECUTION_FAILED",
            phase: "tool",
            retryable: true,
            public_message: "工具未能完成请求。",
            diagnostic_ref: null,
            attempts: 1,
          },
          reason: null,
          continuation_ref: null,
          target_ref: null,
          artifact_ref: null,
          effect: "unknown",
          reconciliation_ref: "recon-1",
        },
        compatibility: "malformed_envelope",
        payload: { token: "sekrit-token", name: "calendar" },
      },
    ];
    render(<EventStream mode="trace" events={[]} traceEvents={events} />);

    expect(screen.getByText("审批已拒绝")).toBeTruthy();
    expect(screen.getByText(/第 3 次尝试/)).toBeTruthy();
    expect(screen.getByText(/结果未知/)).toBeTruthy();
    expect(screen.getByText(/失败阶段 tool/)).toBeTruthy();
    expect(screen.getByText(/可以重试/)).toBeTruthy();
    expect(screen.getByText(/引用父事件 not-loaded，该事件尚未加载/)).toBeTruthy();
    expect(screen.getByText(/无法按当前格式读取/)).toBeTruthy();
    expect(screen.queryByText("sekrit-token")).toBeNull();
  });

  it("reads failure phase from the serialized outcome and skips it when error is null", () => {
    const events: TraceEvent[] = [
      {
        event_id: "failed-1",
        seq: 1,
        type: "tool.execution.finished",
        occurred_at: "2026-08-18T00:00:01Z",
        outcome: {
          schema_version: 1,
          scope: "tool",
          status: "failed",
          error: {
            code: "TOOL_EXECUTION_FAILED",
            phase: "tool",
            retryable: false,
            public_message: "工具未能完成请求。",
            diagnostic_ref: null,
            attempts: 1,
          },
          reason: null,
          effect: "not_applicable",
          reconciliation_ref: null,
        },
      },
      {
        event_id: "done-1",
        seq: 2,
        type: "tool.execution.finished",
        occurred_at: "2026-08-18T00:00:02Z",
        outcome: {
          schema_version: 1,
          scope: "tool",
          status: "completed",
          error: null,
          reason: null,
          effect: "applied",
          reconciliation_ref: null,
        },
        payload: { name: "calendar" },
      },
    ];
    render(<EventStream mode="trace" events={[]} traceEvents={events} />);
    expect(screen.getByText(/失败阶段 tool/)).toBeTruthy();
    expect(screen.getByText(/不能重试/)).toBeTruthy();
    expect(screen.queryByText(/结果未知/)).toBeNull();
    expect(screen.getAllByText(/工具：calendar|当前工具/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/失败阶段/).length).toBe(1);
    expect(screen.getAllByText(/不能重试|可以重试/).length).toBe(1);
  });
});
