import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ActivityRail from "../components/ActivityRail";
import type { EventRecord, Run, Stats } from "../types";

afterEach(cleanup);

const run: Run = {
  id: "run-1",
  goal_id: "goal-1",
  session_id: "session-1",
  state: "PLANNING",
  resume_state: null,
  current_plan_version_id: "plan-1",
  current_step_id: null,
  version: 1,
  budget: { react_iterations_remaining: 4, react_iteration: 3 },
  pending_approvals: [],
};

const stats: Stats = {
  run_id: "run-1",
  state: "PLANNING",
  interactions: 1,
  plan_completed: 0,
  plan_total: 3,
};

function event(type: string, data: Record<string, unknown>, seq: number): EventRecord {
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

describe("activity rail", () => {
  it("shows readable progress without exposing raw interaction ids", () => {
    render(
      <ActivityRail
        run={run}
        stats={stats}
        events={[
          event("interaction.started", { interaction_id: "interaction_a4c59119227447cf96d2d558bbbfb1a9" }, 1),
          event("model.invocation_started", { kind: "clarification" }, 2),
          event("model.invocation_started", { kind: "planning" }, 3),
          event("state.transitioned", { from: "RECEIVED", to: "PLANNING" }, 4),
        ]}
        onOpenTrajectory={() => undefined}
      />,
    );

    expect(screen.getByText("正在理解目标")).toBeTruthy();
    expect(screen.getAllByText("正在生成计划").length).toBeGreaterThan(0);
    expect(screen.getByText("已进入规划阶段")).toBeTruthy();
    expect(screen.getByText("已执行循环")).toBeTruthy();
    expect(screen.getByText("3 次")).toBeTruthy();
    expect(screen.queryByText("剩余预算")).toBeNull();
    expect(screen.queryByText("interaction_a4c59119227447cf96d2d558bbbfb1a9")).toBeNull();
  });

  it("keeps the current phase and recent activity visually separated", () => {
    render(
      <ActivityRail
        run={run}
        stats={stats}
        events={[event("state.transitioned", { from: "RECEIVED", to: "PLANNING" }, 1)]}
        onOpenTrajectory={() => undefined}
      />,
    );

    expect(screen.getByText("当前阶段")).toBeTruthy();
    expect(screen.getByText("最近活动")).toBeTruthy();
    expect(screen.getByText("1 条")).toBeTruthy();
    expect(screen.getByRole("complementary", { name: "当前运行进度" }).querySelector(".activity-current")).toBeTruthy();
    expect(screen.getByRole("complementary", { name: "当前运行进度" }).querySelector(".activity-list-heading")).toBeTruthy();
  });
});
