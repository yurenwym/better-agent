import { describe, expect, it } from "vitest";
import type { EventRecord, ThreadEvent } from "../types";
import { describeEvent, describeThreadEvent, groupEvents } from "../trajectory";

function event(type: string, data: Record<string, unknown> = {}, seq = 1): EventRecord {
  return {
    schema_version: 1,
    event_id: `evt-${seq}`,
    seq,
    run_id: "run-1",
    goal_id: "goal-1",
    type,
    occurred_at: "2026-08-18T00:00:00Z",
    actor: "runtime",
    correlation: {},
    data,
  };
}

function threadEvent(type: string, data: Record<string, unknown> = {}, seq = 1): ThreadEvent {
  return {
    schema_version: 1,
    event_id: `thread-evt-${seq}`,
    seq,
    thread_id: "thread-1",
    turn_id: "turn-1",
    type,
    occurred_at: "2026-08-18T00:00:00Z",
    actor: "worker",
    data,
  };
}

describe("trajectory view model", () => {
  it("maps state events to a readable label and stage", () => {
    const item = describeEvent(event("state.transitioned", { from: "RECEIVED", to: "PLANNING" }));

    expect(item.stage).toBe("state");
    expect(item.title).toContain("进入规划");
  });

  it("groups events by interaction and keeps sequence order", () => {
    const groups = groupEvents([
      event("interaction.started", { interaction_id: "interaction-1" }, 1),
      event("model.invocation_started", { kind: "clarification" }, 2),
      event("interaction.ended", { interaction_id: "interaction-1" }, 3),
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0].events.map((item) => item.seq)).toEqual([1, 2, 3]);
  });

  it("summarizes a model failure without exposing raw provider payloads", () => {
    const item = describeEvent(event("model.attempt_finished", { error_kind: "request", status: "failed" }));

    expect(item.title).toContain("模型请求失败");
    expect(item.detail).not.toContain("api_key");
  });

  it("makes a model response visible as a next-step activity", () => {
    const item = describeEvent(event("model.response", { kind: "planning", content: "{...}" }));

    expect(item.title).toContain("计划回复已到达");
    expect(item.detail).toContain("加入对话");
  });

  it("summarizes model deltas without putting partial JSON in the trajectory copy", () => {
    const item = describeEvent(event("model.response.delta", {
      kind: "planning",
      delta: '{"summary":"内部片段"}',
    }));

    expect(item.title).toContain("正在接收模型回答");
    expect(item.detail).not.toContain("内部片段");
  });

  it("translates thread completion events into readable activity", () => {
    const item = describeThreadEvent(threadEvent("turn.completed"));

    expect(item.title).toBe("本轮对话完成");
    expect(item.detail).toContain("加入对话");
  });
});
