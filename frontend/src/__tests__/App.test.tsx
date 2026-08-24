import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import App from "../App";
import StatsBar from "../components/StatsBar";
import EventStream from "../components/EventStream";
import ApprovalCard from "../components/ApprovalCard";

afterEach(() => { cleanup(); window.history.pushState({}, "", "/"); });

describe("personal agent workspace", () => {
  it("switches between the four core pages", () => {
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "轨迹" }));
    expect(screen.getAllByRole("heading", { name: "运行轨迹" }).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "计划" }));
    expect(screen.getAllByRole("heading", { name: "已保存计划" }).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "记忆" }));
    expect(screen.getAllByRole("heading", { name: "长期记忆" }).length).toBeGreaterThan(0);
  });

  it("keeps the chat page focused on the conversation surface", () => {
    render(<App />);

    expect(screen.queryByRole("heading", { name: "Better Agent" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "目标对话" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "开始一段新的工作" })).toBeNull();
    expect(screen.getByRole("region", { name: "当前目标对话" })).toBeTruthy();
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

  it("gives the chat page the same wide shell as the trajectory page", () => {
    render(<App />);

    const main = screen.getByRole("main");
    expect(main.className).toContain("workspace-main-viewport");
    expect(main.querySelector(".workspace-page-chat")?.className).toContain("workspace-page-wide");
    expect(main.querySelector(".workspace-page-chat")?.className).toContain("workspace-page-fluid");
  });

  it("removes the redundant research page heading and uses the fluid shell", () => {
    window.history.pushState({}, "", "/research");
    render(<App />);

    const main = screen.getByRole("main");
    expect(main.querySelector(".workspace-page-header-research")).toBeNull();
    expect(main.querySelector(".workspace-page-research")?.className).toContain("workspace-page-fluid");
  });

  it("uses the same fluid master-detail shell for today", () => {
    window.history.pushState({}, "", "/today");
    render(<App />);

    const main = screen.getByRole("main");
    expect(main.querySelector(".workspace-page-header-today")).toBeNull();
    expect(main.querySelector(".workspace-page-today")?.className).toContain("workspace-page-fluid");
    expect(main.querySelector(".today-list")).toBeTruthy();
    expect(main.querySelector(".today-detail")).toBeTruthy();
  });

  it("exposes a labelled goal message form", () => {
    render(<App />);

    expect(screen.getByLabelText("输入消息")).toBeTruthy();
    expect(screen.getByRole("button", { name: "发送" })).toBeTruthy();
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

  it("gives an empty trajectory a useful, structured next step", () => {
    render(<EventStream events={[]} />);

    const timeline = screen.getByRole("region", { name: "运行时间线" });
    expect(timeline.querySelector(".timeline-empty")).toBeTruthy();
    expect(screen.getByText("等待第一条轨迹事件")).toBeTruthy();
    expect(screen.getByText("发送消息后，交互、模型和计划进展会按顺序出现在这里。")).toBeTruthy();
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
