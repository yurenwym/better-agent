import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "./renderApp";
import StatsBar from "../components/StatsBar";
import EventStream from "../components/EventStream";
import ApprovalCard from "../components/ApprovalCard";
import WorkspaceSidebar from "../components/WorkspaceSidebar";
import type { Thread } from "../types";

afterEach(() => { cleanup(); window.history.pushState({}, "", "/"); });

// 页面已改为 React.lazy 懒加载：全量并行跑测试时动态 import 可能超过默认 1s 超时，
// 因此所有等待懒加载页面的断言统一使用更长的超时。
const LAZY = { timeout: 8000 };

describe("personal agent workspace", () => {
  it("switches between the four core pages", async () => {
    renderApp();

    fireEvent.click(await screen.findByText("控制台", undefined, LAZY));
    fireEvent.click(screen.getByRole("link", { name: "运行轨迹" }));
    expect((await screen.findAllByRole("heading", { name: "运行轨迹" }, LAZY)).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("link", { name: "计划" }));
    expect(await screen.findByRole("navigation", { name: "计划视图" }, LAZY)).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "记忆" }));
    expect((await screen.findAllByRole("heading", { name: "长期记忆" }, LAZY)).length).toBeGreaterThan(0);
  });

  it("keeps the chat page focused on the conversation surface", async () => {
    renderApp();

    expect(screen.queryByRole("heading", { name: "Better Agent" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "目标对话" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "开始一段新的工作" })).toBeNull();
    expect(await screen.findByRole("region", { name: "当前目标对话" })).toBeTruthy();
  });

  it("uses a wider shell for the trajectory workspace", async () => {
    renderApp();

    fireEvent.click(await screen.findByText("控制台"));
    fireEvent.click(screen.getByRole("link", { name: "运行轨迹" }));

    const main = await screen.findByRole("main");
    expect(main.querySelector(".workspace-topbar h1")?.textContent).toBe("运行轨迹");
    expect(main.querySelector(".workspace-page")?.className).toContain("workspace-page-trajectory");
  });

  it("gives the empty conversation more room on desktop", async () => {
    renderApp();

    await screen.findByRole("main");
    expect(document.querySelector(".chat-workspace-empty")?.className).toContain("chat-workspace-empty-wide");
  });

  it("gives the chat page the same wide shell as the trajectory page", () => {
    renderApp();

    const main = screen.getByRole("main");
    expect(main.className).toContain("workspace-main-viewport");
    expect(main.querySelector(".workspace-page-chat")?.className).toContain("workspace-page-wide");
    expect(main.querySelector(".workspace-page-chat")?.className).toContain("workspace-page-fluid");
  });

  it("removes the redundant research page heading and uses the fluid shell", () => {
    window.history.pushState({}, "", "/research");
    renderApp();

    const main = screen.getByRole("main");
    expect(main.querySelector(".workspace-page-header-research")).toBeNull();
    expect(main.querySelector(".workspace-page-research")?.className).toContain("workspace-page-fluid");
  });

  it("uses the same fluid master-detail shell for today", async () => {
    window.history.pushState({}, "", "/today");
    renderApp();

    const main = await screen.findByRole("main");
    expect(main.querySelector(".workspace-page-header-today")).toBeNull();
    expect(main.querySelector(".workspace-page-today")?.className).toContain("workspace-page-wide");
    expect(main.querySelector(".workspace-page-today")?.className).not.toContain("workspace-page-fluid");
    await waitFor(() => expect(main.querySelector(".today-load-state")).toBeTruthy(), LAZY);
    const goalNav=screen.getByRole("navigation",{name:"工作区导航"});
    expect(goalNav.querySelector('[aria-current="page"]')?.textContent).toBe("计划");
  });

  it("keeps four daily work destinations visible instead of hiding goals",()=>{
    render(<WorkspaceSidebar activePage="chat" bootstrap={null} run={null} onNavigate={()=>undefined} onNewConversation={()=>undefined}/>);
    const goalNav=screen.getByRole("navigation",{name:"工作区导航"});
    expect(goalNav.querySelectorAll("a")).toHaveLength(3);
    expect(goalNav.textContent).toContain("计划");
    expect(goalNav.textContent).not.toContain("今日");
    expect(goalNav.querySelector('[aria-current="page"]')?.textContent).toBe("对话");
  });

  it("restores plan deep links when browser history changes",async()=>{
    window.history.pushState({},"","/plans/plan-first");
    renderApp();
    window.history.pushState({},"","/plans/plan-second");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(window.location.pathname).toBe("/plans/plan-second");
  });

  it("keeps multiple conversation entries and switches the active thread", () => {
    const threads = [
      { id: "thread-2", title: "广西旅行", version: 1, active_turn_id: null, next_event_seq: 2, created_at: "2026-08-24T02:00:00Z", updated_at: "2026-08-24T02:00:00Z" },
      { id: "thread-1", title: "骑行计划", version: 1, active_turn_id: null, next_event_seq: 2, created_at: "2026-08-24T01:00:00Z", updated_at: "2026-08-24T01:00:00Z" },
    ] satisfies Thread[];
    const onSelectThread = vi.fn();
    render(<WorkspaceSidebar activePage="chat" activeThreadId="thread-2" bootstrap={null} run={null} threads={threads} onNavigate={()=>undefined} onNewConversation={()=>undefined} onSelectThread={onSelectThread}/>);

    expect(screen.getByRole("link", { name: /^广西旅行/ }).getAttribute("aria-current")).toBe("page");
    fireEvent.click(screen.getByRole("link", { name: /^骑行计划/ }));
    expect(onSelectThread).toHaveBeenCalledWith("thread-1");
    expect(screen.getByRole("link", { name: /^广西旅行/ })).toBeTruthy();
  });

  it("offers a separate labelled delete action for every conversation", () => {
    const threads = [
      { id: "thread-1", title: "骑行计划", version: 1, active_turn_id: null, next_event_seq: 2, created_at: "2026-08-24T01:00:00Z", updated_at: "2026-08-24T01:00:00Z" },
    ] satisfies Thread[];
    const onDeleteThread = vi.fn();
    render(<WorkspaceSidebar activePage="chat" activeThreadId="thread-1" bootstrap={null} run={null} threads={threads} onNavigate={()=>undefined} onNewConversation={()=>undefined} onSelectThread={()=>undefined} onDeleteThread={onDeleteThread}/>);

    fireEvent.click(screen.getByRole("button", { name: "删除会话：骑行计划" }));

    expect(onDeleteThread).toHaveBeenCalledWith("thread-1");
  });

  it("exposes a labelled goal message form", () => {
    renderApp();

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
