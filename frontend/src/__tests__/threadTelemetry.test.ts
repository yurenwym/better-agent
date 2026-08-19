import { describe, expect, it } from "vitest";
import { applyThreadEvent, needsMessageSnapshot } from "../hooks/useThreadTelemetry";
import type { MessageRecord, ThreadEvent } from "../types";

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
});
