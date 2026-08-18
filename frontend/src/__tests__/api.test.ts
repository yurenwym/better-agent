import { describe, expect, it, vi } from "vitest";
import { createGoal, getBootstrap, subscribeToEvents } from "../api";
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
});
