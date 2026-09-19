import { describe, expect, it } from "vitest";
import type { EventRecord, ThreadEvent } from "../types";
import { describeEvent, describeThreadEvent, formatDuration, groupEvents, keyTrajectoryEvents, latestArchiveStatus } from "../trajectory";

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

  it("renders stable runtime reason codes as Chinese instead of exposing internal English", () => {
    const item = describeEvent(event("run.blocked", {
      reason_code: "REACT_ITERATION_BUDGET_EXHAUSTED",
      message: "本步骤的执行轮次已用完",
      reason: "react iteration budget exhausted",
    }));

    expect(item.detail).toBe("本步骤的执行轮次已用完");
    expect(item.detail).not.toContain("react iteration");
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

  it("summarizes memory retrieval without exposing raw query or vectors", () => {
    const item = describeThreadEvent(threadEvent("memory.retrieval.completed", {
      retrieval_mode: "lexical_fallback",
      fallback_reason: "embedding_not_configured",
      query: "private query",
      embedding: [1, 0, 0],
    }));

    expect(item.stage).toBe("context");
    expect(item.title).toBe("已使用关键词记忆检索");
    expect(item.detail).toContain("向量模型未配置");
    expect(item.detail).not.toContain("private query");
  });

  it("describes plan document lifecycle events in user-facing language", () => {
    expect(describeThreadEvent(threadEvent("plan.document_prepared", {
      plan_document_id: "plan_123",
      version: 1,
    })).title).toBe("正在准备保存计划");
    expect(describeThreadEvent(threadEvent("plan.document_version_created", {
      plan_document_id: "plan_123",
      version: 1,
    })).detail).toContain("v1");
    expect(describeThreadEvent(threadEvent("plan.document_ready", {
      plan_document_id: "plan_123",
      version: 1,
    })).title).toBe("计划已保存");
    expect(describeThreadEvent(threadEvent("plan.document_failed", {
      reason: "projection unavailable",
    })).detail).toBe("计划文件写入未完成，可以重试");
  });

  it("describes plan context and execution projection events", () => {
    expect(describeThreadEvent(threadEvent("plan.context_loaded", {
      version: 2,
      cropped: false,
    })).title).toBe("已加载计划 v2");
    expect(describeThreadEvent(threadEvent("plan.execution_projection_created", {
      version: 2,
    })).stage).toBe("plan");
    expect(describeThreadEvent(threadEvent("plan.execution_projection_failed", {
      reason: "编译失败",
    })).tone).toBe("danger");
  });

  it("keeps a concise conversation spine plus failures in summary mode", () => {
    const events = [
      threadEvent("turn.accepted", {}, 1),
      threadEvent("turn.started", {}, 2),
      threadEvent("memory.retrieval.completed", { retrieval_mode: "semantic" }, 3),
      threadEvent("turn.policy_decided", {}, 4),
      threadEvent("message.started", {}, 5),
      threadEvent("message.delta", { delta: "partial" }, 6),
      threadEvent("message.completed", {}, 7),
      threadEvent("turn.completed", {}, 8),
      threadEvent("turn.metrics.updated", { total_ms: 1200 }, 9),
      threadEvent("model.attempt.failed", { error_kind: "timeout" }, 10),
    ];

    expect(keyTrajectoryEvents(events, "thread").map((item) => item.type)).toEqual([
      "turn.started",
      "memory.retrieval.completed",
      "message.completed",
      "model.attempt.failed",
    ]);
  });

  it("formats latency values in milliseconds and seconds", () => {
    expect(formatDuration(320)).toBe("0.32s");
    expect(formatDuration(1234)).toBe("1.23s");
    expect(formatDuration(null)).toBe("--");
  });

  // R2-04: the waiting state must read as a *context* status, never as model
  // output, so that "正在整理历史" can never be mistaken for a first token.
  it("renders the archival wait as a context status, not model output", () => {
    for (const state of ["waiting", "ready", "cancelled", "timeout"]) {
      const item = describeThreadEvent(
        threadEvent("context.archiving", { state, waited_ms: 1200, deadline_ms: 2000 }),
      );
      expect(item.stage).toBe("context");
      expect(item.stage).not.toBe("model");
      expect(item.title).toContain("历史");
    }
  });

  it("distinguishes a timed-out wait from a dropped-history answer", () => {
    const timeout = describeThreadEvent(
      threadEvent("context.archive_wait_timeout", { message: "你的输入已保存，稍后可以直接继续。" }),
    );
    expect(timeout.stage).toBe("context");
    expect(timeout.tone).toBe("warning");
    expect(timeout.detail).toContain("稍后");

    const incomplete = describeThreadEvent(
      threadEvent("context.incomplete", { message: "历史整理不可用" }),
    );
    expect(incomplete.stage).toBe("context");
    expect(incomplete.tone).toBe("warning");
    expect(incomplete.title).toContain("完整历史");
  });

  it("surfaces the wait status and the incomplete-context warning in summary mode", () => {
    const events = [
      threadEvent("turn.accepted", {}, 1),
      threadEvent("context.archiving", { state: "waiting" }, 2),
      threadEvent("context.counted", { counting_ms: 12 }, 3),
      threadEvent("message.completed", {}, 4),
      threadEvent("context.archive_wait_timeout", {}, 5),
    ];

    expect(keyTrajectoryEvents(events, "thread").map((item) => item.type)).toEqual([
      "context.archiving",
      "message.completed",
      "context.archive_wait_timeout",
    ]);
  });
});

describe("latestArchiveStatus", () => {
  it("reports nothing when no archival wait has happened", () => {
    expect(latestArchiveStatus([])).toBeNull();
    expect(latestArchiveStatus([threadEvent("message.completed")])).toBeNull();
  });

  it("shows the waiting hint while the wait is in progress", () => {
    const hint = latestArchiveStatus([threadEvent("context.archiving", { state: "waiting" })]);
    expect(hint?.state).toBe("waiting");
    expect(hint?.title).toContain("整理历史");
  });

  it("clears the hint once the wait succeeds", () => {
    const hint = latestArchiveStatus([
      threadEvent("context.archiving", { state: "waiting" }, 1),
      threadEvent("context.archiving", { state: "ready" }, 2),
    ]);
    expect(hint).toBeNull();
  });

  it("reports a timeout as a warning that does not claim success", () => {
    const hint = latestArchiveStatus([
      threadEvent("context.archiving", { state: "timeout" }, 1),
      threadEvent("context.archive_wait_timeout", { message: "稍后可以直接继续" }, 2),
    ]);
    expect(hint?.state).toBe("timeout");
    expect(hint?.tone).toBe("warning");
    expect(hint?.detail).toContain("稍后");
  });

  it("reports a dropped-history answer separately from a timeout", () => {
    const hint = latestArchiveStatus([threadEvent("context.incomplete", { message: "历史整理不可用" })]);
    expect(hint?.state).toBe("incomplete");
    expect(hint?.title).toContain("完整历史");
  });

  it("lets a newer wait supersede an older terminal state", () => {
    const hint = latestArchiveStatus([
      threadEvent("context.archive_wait_timeout", {}, 1),
      threadEvent("context.archiving", { state: "waiting" }, 2),
    ]);
    expect(hint?.state).toBe("waiting");
  });
});
