import { describe, expect, it } from "vitest";
import { applyThreadEvent, hydrateThreadMessages, needsEventRecovery, needsMessageSnapshot } from "../hooks/useThreadTelemetry";
import type { MessageRecord, ThreadEvent, ThreadMessage } from "../types";

function message(content: string, generation = 1): MessageRecord {
  return {
    id: "m1",
    run_id: "thread-1",
    interaction_id: null,
    role: "assistant",
    content,
    created_at: "2026-08-19T00:00:00Z",
    streaming: true,
    generation,
  };
}

function event(data: Record<string, unknown>): ThreadEvent {
  return {
    schema_version: 1,
    event_id: "e1",
    seq: 1,
    thread_id: "thread-1",
    turn_id: "turn-1",
    type: "message.delta",
    occurred_at: "2026-08-19T00:00:01Z",
    actor: "model",
    data,
  };
}

describe("thread telemetry reconciliation", () => {
  it("appends a delta only when generation and offset are exact", () => {
    const current = [message("abc")];
    const next = applyThreadEvent(current, event({ message_id: "m1", generation: 1, offset: 3, delta: "你好" }));

    expect(next[0].content).toBe("abc你好");
    expect(needsMessageSnapshot(current, event({ message_id: "m1", generation: 1, offset: 5, delta: "x" }))).toBe(true);
  });

  it("does not append a duplicate or a stale generation", () => {
    const current = [message("abc")];
    const duplicate = applyThreadEvent(current, event({ message_id: "m1", generation: 1, offset: 2, delta: "x" }));
    const stale = applyThreadEvent(current, event({ message_id: "m1", generation: 2, offset: 3, delta: "x" }));

    expect(duplicate).toEqual(current);
    expect(stale).toEqual(current);
    expect(needsMessageSnapshot(current, event({ message_id: "m1", generation: 2, offset: 3, delta: "x" }))).toBe(true);
  });

  it("removes an interrupted generation when the gateway starts a retry", () => {
    const current = [message("old answer")];
    const retry = {
      ...event({ message_id: "m1", generation: 1, finish_reason: "retry" }),
      type: "message.completed",
    };

    expect(applyThreadEvent(current, retry)).toEqual([]);
  });

  it("does not hydrate interrupted generations into the visible chat", () => {
    const interrupted: ThreadMessage = {
      id: "m1",
      thread_id: "thread-1",
      turn_id: "turn-1",
      role: "assistant",
      content: "old answer",
      status: "interrupted",
      generation: 1,
      content_length: 10,
      created_at: "2026-08-19T00:00:00Z",
      completed_at: "2026-08-19T00:00:01Z",
    };

    expect(hydrateThreadMessages([interrupted])).toEqual([]);
  });

  it("detects a missing event sequence for REST recovery", () => {
    const current = [event({ message_id: "m1", generation: 1, offset: 0, delta: "a" })];
    expect(needsEventRecovery(current, { ...event({}), seq: 3, event_id: "e3" })).toBe(true);
  });
});
