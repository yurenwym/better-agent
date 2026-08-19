import { describe, expect, it } from "vitest";
import { describeThreadEvent } from "../trajectory";
import type { ThreadEvent } from "../types";

function event(type: string, data: Record<string, unknown>): ThreadEvent {
  return {
    schema_version: 1,
    event_id: "ask-event-1",
    seq: 1,
    thread_id: "thread-1",
    turn_id: "turn-1",
    type,
    occurred_at: "2026-08-19T00:00:00Z",
    actor: "worker",
    data,
  };
}

describe("ask trajectory", () => {
  it("describes waiting for a user answer without raw tool data", () => {
    const item = describeThreadEvent(event("turn.awaiting_input", {
      ask_id: "ask-1",
      questions: [{ id: "secret", question: "内部问题" }],
      raw_arguments: "{\"api_key\":\"secret\"}",
    }));

    expect(item.title).toContain("等待你的回答");
    expect(item.detail).not.toContain("api_key");
    expect(item.detail).not.toContain("内部问题");
  });
});
