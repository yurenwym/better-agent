import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TRACE_EMPTY_PAGE_BUDGET, useTraceTelemetry } from "../hooks/useTraceTelemetry";
import type { TraceEvent, TracePage } from "../types";

const api = vi.hoisted(() => ({ getTrace: vi.fn() }));
vi.mock("../api", () => api);
afterEach(cleanup);

function event(id: string, extra: Partial<TraceEvent> = {}): TraceEvent {
  return { event_id: id, seq: 1, type: "note.saved", occurred_at: "2026-01-01T00:00:00Z", ...extra };
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (error: unknown) => void = () => undefined;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function page(events: TraceEvent[], cursor: string | null, reasons: TracePage["completeness"]["reasons"] = []): TracePage {
  return {
    scope: { thread_id: "thread-1" },
    count: events.length,
    next_cursor: cursor,
    events,
    completeness: { status: reasons.length ? "partial" : "exact", reasons },
  };
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe("trace telemetry", () => {
  it("follows an empty page and drops an event id that appears again", async () => {
    api.getTrace
      .mockResolvedValueOnce(page([event("duplicate")], "cursor-1"))
      .mockResolvedValueOnce(page([event("duplicate"), event("kept")], null));
    const { result } = renderHook(() => useTraceTelemetry({ kind: "thread", id: "thread-1" }, { pollMs: 60_000 }));
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toEqual(["duplicate", "kept"]));
    expect(api.getTrace.mock.calls[1][1]).toBe("cursor-1");
    expect(result.current.cursor).toBeNull();
  });

  it("stops after the empty-page budget instead of chasing cursors forever", async () => {
    api.getTrace.mockImplementation(async () => page([], `cursor-${api.getTrace.mock.calls.length}`, [{ code: "truncated" }]));
    const { result } = renderHook(() => useTraceTelemetry({ kind: "trace", id: "trace-1" }, { pollMs: 60_000 }));
    await waitFor(() => expect(api.getTrace).toHaveBeenCalledTimes(TRACE_EMPTY_PAGE_BUDGET));
    expect(result.current.events).toEqual([]);
    expect(result.current.cursor).toBe(`cursor-${TRACE_EMPTY_PAGE_BUDGET}`);
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(api.getTrace).toHaveBeenCalledTimes(TRACE_EMPTY_PAGE_BUDGET);
  });

  it("continues from the opaque cursor and then reads a later target", async () => {
    api.getTrace.mockImplementation(async (_scope: unknown, cursor: string | null) => {
      if (cursor === "cursor-3") return page([event("tevt-legacy-5", { type: "legacy.target" })], null);
      const seen = api.getTrace.mock.calls.length;
      return page([], `cursor-${seen}`, [{ code: "truncated" }]);
    });
    const { result } = renderHook(() => useTraceTelemetry({ kind: "thread", id: "thread-1" }, { pollMs: 60_000 }));
    await waitFor(() => expect(result.current.cursor).toBe("cursor-3"));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toEqual(["tevt-legacy-5"]));
    expect(api.getTrace.mock.calls.some((call) => call[1] === "cursor-3")).toBe(true);
  });

  it("ignores a slow response after the scope changes", async () => {
    let resolveFirst: (value: TracePage) => void = () => undefined;
    api.getTrace.mockImplementation((scope: { id: string }) => {
      if (scope.id === "thread-a") return new Promise<TracePage>((resolve) => { resolveFirst = resolve; });
      return Promise.resolve(page([event("from-b")], null));
    });
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useTraceTelemetry({ kind: "thread", id }, { pollMs: 60_000 }),
      { initialProps: { id: "thread-a" } },
    );
    rerender({ id: "thread-b" });
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toEqual(["from-b"]));
    await act(async () => { resolveFirst(page([event("from-a")], null)); });
    expect(result.current.events.map((item) => item.event_id)).toEqual(["from-b"]);
  });

  it("replaces stale data when refresh starts a new query", async () => {
    let resolveFirst: (value: TracePage) => void = () => undefined;
    api.getTrace
      .mockImplementationOnce(() => new Promise<TracePage>((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValue(page([event("refreshed")], null));
    const { result } = renderHook(() => useTraceTelemetry({ kind: "thread", id: "thread-1" }, { pollMs: 60_000 }));
    await waitFor(() => expect(api.getTrace).toHaveBeenCalledTimes(1));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toEqual(["refreshed"]));
    expect(api.getTrace.mock.calls.at(-1)?.[1]).toBeNull();
    await act(async () => { resolveFirst(page([event("stale")], null)); });
    expect(result.current.events.map((item) => item.event_id)).toEqual(["refreshed"]);
  });

  it("reports a missing trace and a network failure without another request", async () => {
    api.getTrace.mockRejectedValue(new Error("请求失败（404）"));
    const missing = renderHook(() => useTraceTelemetry({ kind: "thread", id: "missing" }, { pollMs: 60_000 }));
    await waitFor(() => expect(missing.result.current.error).toBe("请求失败（404）"));
    expect(missing.result.current.events).toEqual([]);
    expect(api.getTrace).toHaveBeenCalledTimes(1);

    api.getTrace.mockRejectedValue(new Error("Failed to fetch"));
    const failed = renderHook(() => useTraceTelemetry({ kind: "task", taskKind: "expert", id: "run-1" }, { pollMs: 60_000 }));
    await waitFor(() => expect(failed.result.current.error).toBe("Failed to fetch"));
    expect(api.getTrace).toHaveBeenCalledTimes(2);
  });

  it("stops polling when a fresh read adds nothing", async () => {
    api.getTrace.mockResolvedValue(page([event("only")], null));
    const quiet = renderHook(() => useTraceTelemetry({ kind: "thread", id: "thread-1" }, { pollMs: 20 }));
    await waitFor(() => expect(api.getTrace.mock.calls.length).toBeGreaterThanOrEqual(2));
    const seen = api.getTrace.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(api.getTrace.mock.calls.length).toBe(seen);
    quiet.unmount();
    expect(api.getTrace.mock.calls.every((call) => call[1] === null)).toBe(true);
  });

  it("drops a finished resume that arrives after the thread changes", async () => {
    const late = deferred<TracePage>();
    api.getTrace.mockImplementation((scope: { id: string }, cursor: string | null) => {
      if (scope.id === "thread-a" && cursor === "cursor-a") return late.promise;
      if (scope.id === "thread-a") {
        const seen = api.getTrace.mock.calls.filter((call) => call[0]?.id === "thread-a").length;
        return Promise.resolve(page([], seen >= TRACE_EMPTY_PAGE_BUDGET ? "cursor-a" : `cursor-a-${seen}`));
      }
      return Promise.resolve(page([event("from-b")], null));
    });
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useTraceTelemetry({ kind: "thread", id }, { pollMs: 60_000 }),
      { initialProps: { id: "thread-a" } },
    );
    await waitFor(() => expect(result.current.cursor).toBe("cursor-a"));
    act(() => result.current.loadMore());
    await waitFor(() => expect(api.getTrace.mock.calls.some((call) => call[1] === "cursor-a")).toBe(true));
    rerender({ id: "thread-b" });
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toEqual(["from-b"]));
    await act(async () => {
      late.resolve(page([event("late-a")], null, [{ code: "truncated" }]));
      await late.promise;
    });
    expect(result.current.events.map((item) => item.event_id)).toEqual(["from-b"]);
    expect(result.current.cursor).toBeNull();
    expect(result.current.completeness).toEqual({ status: "exact", reasons: [] });
    expect(result.current.error).toBe("");
    expect(result.current.loading).toBe(false);
  });

  it("drops a finished resume that arrives after refresh replaces the snapshot", async () => {
    const late = deferred<TracePage>();
    let refreshed = false;
    api.getTrace.mockImplementation((_scope: unknown, cursor: string | null) => {
      if (cursor === "cursor-old") return late.promise;
      if (!refreshed) {
        const seen = api.getTrace.mock.calls.filter((call) => call[1] !== "cursor-old").length;
        return Promise.resolve(page([], seen >= TRACE_EMPTY_PAGE_BUDGET ? "cursor-old" : `step-${seen}`));
      }
      return Promise.resolve(page([event("new-snapshot")], null));
    });
    const { result } = renderHook(() => useTraceTelemetry({ kind: "thread", id: "thread-1" }, { pollMs: 60_000 }));
    await waitFor(() => expect(result.current.cursor).toBe("cursor-old"));
    act(() => result.current.loadMore());
    await waitFor(() => expect(api.getTrace.mock.calls.some((call) => call[1] === "cursor-old")).toBe(true));
    refreshed = true;
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toEqual(["new-snapshot"]));
    await act(async () => {
      late.resolve(page([event("old-snapshot")], null));
      await late.promise;
    });
    expect(result.current.events.map((item) => item.event_id)).toEqual(["new-snapshot"]);
    expect(result.current.cursor).toBeNull();
    expect(result.current.error).toBe("");
    expect(result.current.loading).toBe(false);
  });

  it("ignores a resume failure that arrives after the thread changes", async () => {
    const late = deferred<TracePage>();
    api.getTrace.mockImplementation((scope: { id: string }, cursor: string | null) => {
      if (scope.id === "thread-a" && cursor === "cursor-a") return late.promise;
      if (scope.id === "thread-a") {
        const seen = api.getTrace.mock.calls.filter((call) => call[0]?.id === "thread-a").length;
        return Promise.resolve(page([], seen >= TRACE_EMPTY_PAGE_BUDGET ? "cursor-a" : `cursor-a-${seen}`));
      }
      return Promise.resolve(page([event("from-b")], null));
    });
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useTraceTelemetry({ kind: "thread", id }, { pollMs: 60_000 }),
      { initialProps: { id: "thread-a" } },
    );
    await waitFor(() => expect(result.current.cursor).toBe("cursor-a"));
    act(() => result.current.loadMore());
    await waitFor(() => expect(api.getTrace.mock.calls.some((call) => call[1] === "cursor-a")).toBe(true));
    rerender({ id: "thread-b" });
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toEqual(["from-b"]));
    await act(async () => {
      late.reject(new Error("请求失败（500）"));
      await late.promise.catch(() => undefined);
    });
    expect(result.current.events.map((item) => item.event_id)).toEqual(["from-b"]);
    expect(result.current.error).toBe("");
    expect(result.current.loading).toBe(false);
    expect(result.current.cursor).toBeNull();
  });

  it("ignores a resume failure that arrives after refresh", async () => {
    const late = deferred<TracePage>();
    let refreshed = false;
    api.getTrace.mockImplementation((_scope: unknown, cursor: string | null) => {
      if (cursor === "cursor-old") return late.promise;
      if (!refreshed) {
        const seen = api.getTrace.mock.calls.filter((call) => call[1] !== "cursor-old").length;
        return Promise.resolve(page([], seen >= TRACE_EMPTY_PAGE_BUDGET ? "cursor-old" : `step-${seen}`));
      }
      return Promise.resolve(page([event("new-snapshot")], null));
    });
    const { result } = renderHook(() => useTraceTelemetry({ kind: "thread", id: "thread-1" }, { pollMs: 60_000 }));
    await waitFor(() => expect(result.current.cursor).toBe("cursor-old"));
    act(() => result.current.loadMore());
    await waitFor(() => expect(api.getTrace.mock.calls.some((call) => call[1] === "cursor-old")).toBe(true));
    refreshed = true;
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toEqual(["new-snapshot"]));
    await act(async () => {
      late.reject(new Error("请求失败（500）"));
      await late.promise.catch(() => undefined);
    });
    expect(result.current.events.map((item) => item.event_id)).toEqual(["new-snapshot"]);
    expect(result.current.error).toBe("");
    expect(result.current.loading).toBe(false);
  });

  it("keeps reading a thread after an older run finished and a newer run started", async () => {
    let reads = 0;
    api.getTrace.mockImplementation(async () => {
      reads += 1;
      const events = [
        event("old-done", { type: "run.completed", stream_kind: "run", stream_id: "run-old", seq: 1 }),
        event("new-start", { type: "run.started", stream_kind: "run", stream_id: "run-new", seq: 2 }),
      ];
      if (reads > 1) events.push(event("new-step", { type: "tool.called", stream_kind: "run", stream_id: "run-new", seq: 3 }));
      return page(events, null);
    });
    const { result } = renderHook(() => useTraceTelemetry({ kind: "thread", id: "thread-1" }, { pollMs: 20 }));
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toContain("new-step"));
  });

  it("keeps reading a thread while an older run is finished and another run is still open", async () => {
    let reads = 0;
    api.getTrace.mockImplementation(async () => {
      reads += 1;
      const events = [
        event("run-a-done", { type: "run.completed", stream_kind: "run", stream_id: "run-a", seq: 1 }),
        event("run-b-open", { type: "run.started", stream_kind: "run", stream_id: "run-b", seq: 2 }),
      ];
      if (reads > 1) events.push(event("run-b-step", { type: "tool.called", stream_kind: "run", stream_id: "run-b", seq: 3 }));
      return page(events, null);
    });
    const { result } = renderHook(() => useTraceTelemetry({ kind: "thread", id: "thread-1" }, { pollMs: 20 }));
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toContain("run-b-step"));
  });

  it.each(["agent.run.completed", "agent.run.failed", "agent.run.cancelled"])(
    "stops polling when the current expert run reaches %s",
    async (type) => {
      api.getTrace.mockResolvedValue(page([
        event("expert-end", { type, stream_kind: "agent", stream_id: "expert-1", seq: 1 }),
      ], null));
      renderHook(() => useTraceTelemetry({ kind: "task", taskKind: "expert", id: "expert-1" }, { pollMs: 20 }));
      await waitFor(() => expect(api.getTrace).toHaveBeenCalledTimes(1));
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(api.getTrace).toHaveBeenCalledTimes(1);
    },
  );

  it("keeps polling when a terminal event belongs to a different run", async () => {
    let reads = 0;
    api.getTrace.mockImplementation(async () => {
      reads += 1;
      const events = [
        event("other-done", { type: "agent.run.completed", stream_kind: "agent", stream_id: "other-run", seq: 1 }),
      ];
      if (reads > 1) events.push(event("current-step", { type: "agent.task.started", stream_kind: "agent", stream_id: "expert-1", seq: 2 }));
      return page(events, null);
    });
    const { result } = renderHook(() => useTraceTelemetry({ kind: "task", taskKind: "expert", id: "expert-1" }, { pollMs: 20 }));
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toContain("current-step"));
  });

  it("does not apply a response after unmount", async () => {
    let resolvePage: (value: TracePage) => void = () => undefined;
    api.getTrace.mockReturnValue(new Promise<TracePage>((resolve) => { resolvePage = resolve; }));
    const { unmount } = renderHook(() => useTraceTelemetry({ kind: "thread", id: "thread-1" }, { pollMs: 60_000 }));
    unmount();
    await act(async () => { resolvePage(page([event("late")], null)); });
    expect(api.getTrace).toHaveBeenCalledTimes(1);
  });
});
