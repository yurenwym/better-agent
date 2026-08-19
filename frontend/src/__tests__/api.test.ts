import { describe, expect, it, vi } from "vitest";
import { answerAsk, createGoal, getBootstrap, getPendingAsk, getSkills, sendMessage, subscribeToEvents, subscribeToThreadEvents } from "../api";
import type { EventRecord } from "../types";

describe("REST client", () => {
  it("sends JSON and CSRF headers for goal creation", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: "g1" }) });

    await createGoal({ title: "Ship", description: "A proposal" }, "csrf", fetcher);

    expect(fetcher).toHaveBeenCalledWith(
      "/api/goals",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf" },
      }),
    );
  });

  it("reads bootstrap data without exposing an API key", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ csrf_token: "csrf" }) });

    const result = await getBootstrap(fetcher);

    expect(result.csrf_token).toBe("csrf");
    expect(fetcher).toHaveBeenCalledWith("/api/bootstrap");
  });

  it("sends the selected skills only with the current message", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ state: "RECEIVED" }) });

    await sendMessage("goal-1", "Continue", "csrf", ["reflection"], fetcher);

    expect(fetcher).toHaveBeenCalledWith(
      "/api/goals/goal-1/messages",
      expect.objectContaining({ body: JSON.stringify({ content: "Continue", skill_names: ["reflection"] }) }),
    );
  });

  it("loads installed skills from the local runtime", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ skills: [] }) });

    await getSkills(fetcher);

    expect(fetcher).toHaveBeenCalledWith("/api/skills");
  });

  it("submits a structured ask answer with version and CSRF protection", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ask_id: "ask-1", turn: { id: "turn-2" } }) });

    await answerAsk("turn-1", {
      expected_version: 2,
      idempotency_key: "answer-1",
      answers: [{ question_id: "level", selected_options: ["新手"], free_text: "" }],
    }, "csrf", fetcher);

    expect(fetcher).toHaveBeenCalledWith(
      "/api/turns/turn-1/ask/answer",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf" },
        body: JSON.stringify({
          expected_version: 2,
          idempotency_key: "answer-1",
          answers: [{ question_id: "level", selected_options: ["新手"], free_text: "" }],
        }),
      }),
    );
  });

  it("loads a pending ask without sending mutation headers", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: "ask-1" }) });

    await getPendingAsk("turn-1", fetcher);

    expect(fetcher).toHaveBeenCalledWith("/api/turns/turn-1/ask");
  });

  it("keeps the event stream in follow mode and stops reconnecting after a terminal event", () => {
    let handler: ((event: Event) => void) | undefined;
    const close = vi.fn();
    class FakeEventSource {
      static instance: FakeEventSource;
      url: string;
      constructor(url: string) {
        this.url = url;
        FakeEventSource.instance = this;
      }
      addEventListener(_type: string, callback: (event: Event) => void) {
        handler = callback;
      }
      close = close;
    }
    vi.stubGlobal("EventSource", FakeEventSource);

    subscribeToEvents("run-1", 4, () => undefined);

    expect(FakeEventSource.instance.url).toContain("follow=1");
    const event: EventRecord = {
      schema_version: 1,
      event_id: "evt-5",
      seq: 5,
      run_id: "run-1",
      goal_id: "goal-1",
      type: "run.completed",
      occurred_at: "2026-08-18T00:00:00Z",
      actor: "runtime",
      correlation: {},
      data: {},
    };
    handler?.({ data: JSON.stringify(event), lastEventId: "5" } as MessageEvent<string>);

    expect(close).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("keeps the thread stream open after a completed parent turn", () => {
    let handler: ((event: Event) => void) | undefined;
    const close = vi.fn();
    class FakeEventSource {
      static instance: FakeEventSource;
      constructor(public url: string) {
        FakeEventSource.instance = this;
      }
      addEventListener(_type: string, callback: (event: Event) => void) {
        handler = callback;
      }
      close = close;
    }
    vi.stubGlobal("EventSource", FakeEventSource);

    subscribeToThreadEvents("thread-1", 4, () => undefined);
    const completed: EventRecord = {
      schema_version: 1,
      event_id: "evt-5",
      seq: 5,
      run_id: "thread-1",
      goal_id: "",
      type: "turn.completed",
      occurred_at: "2026-08-18T00:00:00Z",
      actor: "user",
      correlation: {},
      data: { continuation_turn_id: "turn-2" },
    };
    handler?.({ data: JSON.stringify(completed), lastEventId: "5" } as MessageEvent<string>);

    expect(FakeEventSource.instance.url).toContain("follow=1");
    expect(close).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("closes the thread stream after a final completed turn", () => {
    let handler: ((event: Event) => void) | undefined;
    const close = vi.fn();
    class FakeEventSource {
      static instance: FakeEventSource;
      constructor(public url: string) {
        FakeEventSource.instance = this;
      }
      addEventListener(_type: string, callback: (event: Event) => void) {
        handler = callback;
      }
      close = close;
    }
    vi.stubGlobal("EventSource", FakeEventSource);

    subscribeToThreadEvents("thread-1", 4, () => undefined);
    const completed: EventRecord = {
      schema_version: 1,
      event_id: "evt-5-final",
      seq: 5,
      run_id: "thread-1",
      goal_id: "",
      type: "turn.completed",
      occurred_at: "2026-08-18T00:00:00Z",
      actor: "worker",
      correlation: {},
      data: {},
    };
    handler?.({ data: JSON.stringify(completed), lastEventId: "5" } as MessageEvent<string>);

    expect(close).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
