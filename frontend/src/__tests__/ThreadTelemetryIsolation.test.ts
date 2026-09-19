import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useThreadTelemetry } from "../hooks/useThreadTelemetry";

const api = vi.hoisted(() => ({ getThread: vi.fn(), getThreadEvents: vi.fn(), getThreadMessages: vi.fn(), subscribeToThreadEvents: vi.fn() }));
vi.mock("../api", () => api);
afterEach(cleanup);

beforeEach(() => {
  vi.resetAllMocks();
  api.getThread.mockResolvedValue({ id: "thread-a", active_turn_id: "turn-a", turns: [{ id: "turn-a", status: "AWAITING_INPUT" }] });
  api.getThreadMessages.mockResolvedValue({ messages: [{ id: "message-a", thread_id: "thread-a", role: "assistant", content: "A 的旧消息", status: "ready" }] });
  api.getThreadEvents.mockResolvedValue({ events: [{ event_id: "event-a", seq: 1, thread_id: "thread-a", turn_id: "turn-a", type: "ask.requested", data: {
    ask_id: "ask-a", questions: [{ id: "question-a", header: "确认", question: "A 的问题", options: [], multi_select: false, allow_free_text: true }],
  } }] });
  api.subscribeToThreadEvents.mockReturnValue(vi.fn());
});

it("clears the old thread, messages and Ask while the next thread loads and if it fails", async () => {
  const { result, rerender } = renderHook(({ id }) => useThreadTelemetry(id), { initialProps: { id: "thread-a" } });
  await waitFor(() => expect(result.current.pendingAsk?.id).toBe("ask-a"));
  expect(result.current.messages[0].content).toBe("A 的旧消息");
  const staleSubscription = api.subscribeToThreadEvents.mock.calls[0][2];
  let rejectNext: (reason: Error) => void = () => undefined;
  api.getThread.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectNext = reject; }));
  rerender({ id: "thread-b" });
  expect(result.current).toMatchObject({ thread: null, activeTurn: null, messages: [], events: [], pendingAsk: null, loading: true, error: "" });
  await act(async () => {
    staleSubscription({ type: "ask.requested", data: { ask_id: "late-a" }, seq: 2 });
    rejectNext(new Error("B 加载失败"));
  });
  expect(result.current).toMatchObject({ thread: null, activeTurn: null, messages: [], events: [], pendingAsk: null, loading: false, error: "B 加载失败" });
});

it("ignores a pending snapshot from the previous thread after switching", async () => {
  let resolvePrevious: (value: unknown) => void = () => undefined;
  api.getThread.mockReturnValueOnce(new Promise(resolve => { resolvePrevious = resolve; }));
  const { result, rerender } = renderHook(({ id }) => useThreadTelemetry(id), { initialProps: { id: "thread-a" } });
  api.getThread.mockResolvedValue({ id: "thread-b", active_turn_id: null, turns: [] });
  api.getThreadEvents.mockResolvedValue({ events: [] });
  api.getThreadMessages.mockResolvedValue({ messages: [] });
  rerender({ id: "thread-b" });
  await waitFor(() => expect(result.current.thread?.id).toBe("thread-b"));
  await act(async () => { resolvePrevious({ id: "thread-a", active_turn_id: "turn-a", turns: [{ id: "turn-a" }] }); });
  expect(result.current.thread?.id).toBe("thread-b");
  expect(result.current.pendingAsk).toBeNull();
  expect(result.current.messages).toEqual([]);
});
