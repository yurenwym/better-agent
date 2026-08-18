import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import App from "../App";
import StatsBar from "../components/StatsBar";
import EventStream from "../components/EventStream";
import ApprovalCard from "../components/ApprovalCard";

afterEach(cleanup);

describe("personal agent workspace", () => {
  it("switches between the four core pages", () => {
    render(<App />);

    expect(screen.getByRole("heading", { name: "目标对话" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "轨迹" }));
    expect(screen.getAllByRole("heading", { name: "运行轨迹" }).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "计划" }));
    expect(screen.getAllByRole("heading", { name: "计划版本" }).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "记忆" }));
    expect(screen.getAllByRole("heading", { name: "长期记忆" }).length).toBeGreaterThan(0);
  });

  it("uses a wider shell for the trajectory workspace", () => {
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "轨迹" }));

    const main = screen.getByRole("main");
    expect(main.querySelector(".workspace-page-header")?.className).toContain("workspace-page-header-trajectory");
    expect(main.querySelector(".workspace-page")?.className).toContain("workspace-page-trajectory");
  });

  it("gives the empty conversation more room on desktop", () => {
    render(<App />);

    expect(document.querySelector(".chat-workspace-empty")?.className).toContain("chat-workspace-empty-wide");
  });

  it("exposes a labelled goal message form", () => {
    render(<App />);

    expect(screen.getByLabelText("继续推动目标")).toBeTruthy();
    expect(screen.getByRole("button", { name: "发送" })).toBeTruthy();
    expect(screen.getAllByText("等待输入").length).toBeGreaterThan(0);
  });

  it("shows unavailable telemetry instead of inventing zeroes", () => {
    render(<StatsBar stats={{ run_id: "run-1" }} />);

    expect(screen.getAllByText("不可用").length).toBeGreaterThan(0);
    expect(screen.getByText(/TTFT/)).toBeTruthy();
  });

  it("renders trajectory events in their event lanes", () => {
    render(
      <EventStream
        events={[
          {
            schema_version: 1,
            event_id: "evt-1",
            seq: 1,
            run_id: "run-1",
            goal_id: "goal-1",
            type: "state.transitioned",
            occurred_at: "2026-08-18T00:00:00Z",
            actor: "runtime",
            correlation: {},
            data: { target: "PLANNING" },
          },
        ]}
      />,
    );

    expect(screen.getByText("已进入规划阶段")).toBeTruthy();
    expect(screen.getAllByText("状态").length).toBeGreaterThan(0);
  });

  it("supports text search across trajectory events", () => {
    render(
      <EventStream
        events={[
          { schema_version: 1, event_id: "evt-1", seq: 1, run_id: "run-1", goal_id: "goal-1", type: "state.transitioned", occurred_at: "2026-08-18T00:00:00Z", actor: "runtime", correlation: {}, data: {} },
          { schema_version: 1, event_id: "evt-2", seq: 2, run_id: "run-1", goal_id: "goal-1", type: "tool.execution.finished", occurred_at: "2026-08-18T00:00:01Z", actor: "tool", correlation: {}, data: {} },
        ]}
      />,
    );

    fireEvent.change(screen.getByLabelText("搜索轨迹"), { target: { value: "工具" } });

    expect(screen.queryByText("已进入规划阶段")).toBeNull();
    expect(screen.getByText("工具执行完成")).toBeTruthy();
  });

  it("exposes explicit grant and reject controls for a pending approval", () => {
    const grant = () => undefined;
    const reject = () => undefined;
    render(<ApprovalCard approvalId="approval-1" onGrant={grant} onReject={reject} />);

    expect(screen.getByText("approval-1")).toBeTruthy();
    expect(screen.getByRole("button", { name: "批准" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "拒绝" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
  });
});
