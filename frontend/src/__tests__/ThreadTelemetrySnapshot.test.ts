import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useThreadTelemetry } from "../hooks/useThreadTelemetry";
import type { ThreadEvent } from "../types";

const api = vi.hoisted(() => ({ getThread: vi.fn(), getThreadEvents: vi.fn(), getThreadMessages: vi.fn(), subscribeToThreadEvents: vi.fn() }));
vi.mock("../api", () => api);
afterEach(cleanup);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let settled = false;
  const promise = new Promise<T>((res) => { resolve = (value) => { settled = true; res(value); }; });
  return { promise, resolve, get settled() { return settled; } };
}

function event(seq: number, type: string, data: Record<string, unknown> = {}): ThreadEvent {
  return {
    schema_version: 1,
    event_id: `event-${seq}`,
    seq,
    thread_id: "thread-a",
    turn_id: "turn-a",
    type,
    occurred_at: "2026-10-19T00:00:00Z",
    actor: "worker",
    data,
  };
}

function thread(status: string, version: number) {
  return { id: "thread-a", active_turn_id: "turn-a", turns: [{ id: "turn-a", status, version }] };
}

const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];

beforeEach(() => {
  vi.resetAllMocks();
  api.getThreadMessages.mockResolvedValue({ messages: [] });
  api.subscribeToThreadEvents.mockReturnValue(vi.fn());
});

// The terminal turn.completed is already in the event history while the thread row
// is still STREAMING. The hook must resolve to the terminal turn from the initial
// load alone, i.e. without waiting for a fresh SSE lifecycle event.
it("takes a terminal thread snapshot when the terminal event is already in history", async () => {
  const events = deferred<{ events: ThreadEvent[] }>();
  api.getThreadEvents.mockReturnValue(events.promise);
  api.getThread.mockImplementation(async () => events.settled ? thread("COMPLETED", 2) : thread("STREAMING", 1));

  const { result } = renderHook(() => useThreadTelemetry("thread-a"));
  // The thread row must not be read before the event history is known, or an
  // older STREAMING snapshot can outlive the terminal events it already recorded.
  expect(api.getThread).not.toHaveBeenCalled();

  await act(async () => {
    events.resolve({ events: [event(1, "turn.accepted"), event(2, "turn.completed")] });
    await Promise.resolve();
  });
  await waitFor(() => expect(result.current.activeTurn?.status).toBe("COMPLETED"));
  expect(TERMINAL).toContain(result.current.activeTurn?.status);
  // The subscription resumes from the event cursor, so later events replay.
  expect(api.subscribeToThreadEvents).toHaveBeenCalledWith("thread-a", 2, expect.any(Function));
});

it("reloads a terminal snapshot when followGeneration changes on the same thread", async () => {
  api.getThreadEvents.mockResolvedValue({ events: [event(1, "turn.accepted"), event(2, "turn.completed")] });
  api.getThread.mockResolvedValue(thread("COMPLETED", 2));

  const { result, rerender } = renderHook(({ gen }) => useThreadTelemetry("thread-a", undefined, gen), { initialProps: { gen: 0 } });
  await waitFor(() => expect(result.current.activeTurn?.status).toBe("COMPLETED"));

  rerender({ gen: 1 });
  await waitFor(() => expect(api.getThread).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(result.current.activeTurn?.status).toBe("COMPLETED"));
});

it("does not let an older refresh response overwrite a newer terminal snapshot", async () => {
  api.getThreadEvents.mockResolvedValue({ events: [event(1, "turn.accepted")] });
  api.getThread.mockResolvedValue(thread("STREAMING", 1));

  const { result } = renderHook(() => useThreadTelemetry("thread-a"));
  await waitFor(() => expect(result.current.activeTurn?.status).toBe("STREAMING"));
  const subscription = api.subscribeToThreadEvents.mock.calls[0][2];

  const stale = deferred<ReturnType<typeof thread>>();
  let call = 0;
  api.getThread.mockImplementation(() => {
    call += 1;
    return call === 1 ? stale.promise : Promise.resolve(thread("COMPLETED", 2));
  });

  await act(async () => { subscription(event(2, "turn.completed")); await Promise.resolve(); });
  await act(async () => { subscription(event(3, "turn.failed")); await Promise.resolve(); });
  await waitFor(() => expect(result.current.activeTurn?.status).toBe("COMPLETED"));

  await act(async () => { stale.resolve(thread("STREAMING", 1)); await Promise.resolve(); });
  expect(result.current.activeTurn?.status).toBe("COMPLETED");
});

it("ignores a thread snapshot from a reload that followGeneration superseded", async () => {
  const firstEvents = deferred<{ events: ThreadEvent[] }>();
  api.getThreadEvents.mockReturnValueOnce(firstEvents.promise);
  api.getThread.mockResolvedValue(thread("STREAMING", 1));

  const { result, rerender } = renderHook(({ gen }) => useThreadTelemetry("thread-a", undefined, gen), { initialProps: { gen: 0 } });

  api.getThreadEvents.mockResolvedValue({ events: [event(1, "turn.completed")] });
  api.getThread.mockResolvedValue(thread("COMPLETED", 2));
  rerender({ gen: 1 });
  await waitFor(() => expect(result.current.activeTurn?.status).toBe("COMPLETED"));

  await act(async () => { firstEvents.resolve({ events: [event(1, "turn.accepted")] }); await Promise.resolve(); });
  expect(result.current.activeTurn?.status).toBe("COMPLETED");
});
