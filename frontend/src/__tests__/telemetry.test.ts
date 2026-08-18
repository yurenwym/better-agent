import { describe, expect, it } from "vitest";
import { appendTelemetryEvent, applyModelEvent, hasTerminalEvent, hydrateMessages } from "../hooks/useRunTelemetry";
import type { EventRecord, MessageRecord } from "../types";

function event(type: string, data: Record<string, unknown>, seq: number): EventRecord {
  return {
    schema_version: 1,
    event_id: `event-${seq}`,
    seq,
    run_id: "run-1",
    goal_id: "goal-1",
    type,
    occurred_at: "2026-08-18T00:00:00Z",
    actor: "model",
    correlation: {},
    data,
  };
}

describe("live model telemetry", () => {
  it("merges deltas into one assistant message and replaces it with the final response", () => {
    let messages: MessageRecord[] = [];
    messages = applyModelEvent(messages, event("model.response.delta", {
      message_id: "message-1",
      interaction_id: "interaction-1",
      kind: "planning",
      delta: '{"summary":"',
    }, 1));
    messages = applyModelEvent(messages, event("model.response.delta", {
      message_id: "message-1",
      interaction_id: "interaction-1",
      kind: "planning",
      delta: "Ship" + '"}',
    }, 2));

    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe('{"summary":"Ship"}');
    expect(messages[0].streaming).toBe(true);

    messages = applyModelEvent(messages, event("model.response", {
      message_id: "message-1",
      interaction_id: "interaction-1",
      kind: "planning",
      content: '{"summary":"Ship"}',
    }, 3));

    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe('{"summary":"Ship"}');
    expect(messages[0].streaming).toBe(false);
  });

  it("does not append a delta already included by the REST snapshot", () => {
    const messages: MessageRecord[] = [{
      id: "message-1",
      run_id: "run-1",
      interaction_id: "interaction-1",
      role: "assistant",
      content: "hello world",
      created_at: "2026-08-18T00:00:00Z",
    }];

    const merged = applyModelEvent(messages, event("model.response.delta", {
      message_id: "message-1",
      delta: " world",
      content_length: 11,
    }, 2));

    expect(merged[0].content).toBe("hello world");
  });

  it("clears an abandoned provider attempt before the replacement stream", () => {
    const messages: MessageRecord[] = [{
      id: "message-1",
      run_id: "run-1",
      interaction_id: "interaction-1",
      role: "assistant",
      content: "old attempt",
      created_at: "2026-08-18T00:00:00Z",
    }];

    const reset = applyModelEvent(messages, event("model.response.reset", { message_id: "message-1" }, 2));

    expect(reset[0].content).toBe("");
    expect(reset[0].streaming).toBe(true);
  });

  it("keeps only the latest delta row for one model response", () => {
    const first = event("model.response.delta", { message_id: "message-1", delta: "a" }, 1);
    const second = event("model.response.delta", { message_id: "message-1", delta: "b" }, 2);

    const rows = appendTelemetryEvent(appendTelemetryEvent([], first), second);

    expect(rows.map((row) => row.seq)).toEqual([2]);
  });

  it("marks a partially hydrated assistant message as streaming", () => {
    const messages: MessageRecord[] = [{
      id: "message-1",
      run_id: "run-1",
      interaction_id: "interaction-1",
      role: "assistant",
      content: '{"summary":"partial',
      created_at: "2026-08-18T00:00:00Z",
    }];

    const hydrated = hydrateMessages(messages, [event("model.response.delta", {
      message_id: "message-1",
      delta: '{"summary":"partial',
    }, 1)]);

    expect(hydrated[0].streaming).toBe(true);
  });

  it("does not reconnect to a run whose initial snapshot is already terminal", () => {
    expect(hasTerminalEvent([event("run.completed", {}, 3)])).toBe(true);
    expect(hasTerminalEvent([event("model.invocation_started", {}, 3)])).toBe(false);
  });
});
