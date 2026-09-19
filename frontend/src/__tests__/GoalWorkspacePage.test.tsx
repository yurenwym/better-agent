import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GoalWorkspacePage from "../pages/GoalWorkspacePage";

const api = vi.hoisted(() => ({ getGoalWorkspace: vi.fn(),listPlanDocuments:vi.fn() }));
vi.mock("../api", () => api);
afterEach(cleanup);

describe("GoalWorkspacePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/workspace");
    api.listPlanDocuments.mockResolvedValue({plans:[]});
    api.getGoalWorkspace.mockResolvedValue({
      resource_id: "plan-1", thread_id: "thread-1", plan_document_id: "plan-1", phase: "ADJUSTING",
      next_action: { kind: "accept_adjustment", label: "查看并确认计划调整", href: "/today", resource_id: "proposal-1", reason: "复盘提出了计划调整，等待你的确认。" },
      sources: [{ kind: "plan", id: "plan-1", label: "计划 v2" }, { kind: "review", id: "review-1", label: "每日复盘" }],
      plan: { id: "plan-1", title: "两天训练计划", version: 2, file_status: "ready" },
      program: { id: "program-1", objective_title: "两天训练计划", status: "ACTIVE", start_date: "2026-09-12", end_date: "2026-09-13", version: 1 },
      today: null, review: { id: "review-1", local_date: "2026-09-12", status: "COMPLETED", summary: "今天完成了基础训练。", proposal: null }, research: [],
      growth: { episode_id: null, episode_count: 0, latest_summary: null },
    });
  });

  it("shows saved goals when no resource is selected",async()=>{
    api.listPlanDocuments.mockResolvedValue({plans:[{id:"plan-2",thread_id:"thread-2",title:"骑行减脂计划",version:1,file_status:"ready",created_at:"",updated_at:""}]});
    render(<GoalWorkspacePage/>);
    expect(await screen.findByRole("heading",{name:"全部计划"})).toBeTruthy();
    expect((await screen.findByRole("link",{name:/骑行减脂计划/})).getAttribute("href")).toBe("/workspace/plan-2");
    expect(api.getGoalWorkspace).not.toHaveBeenCalled();
  });

  it("recovers from a missing resource with plan choices",async()=>{
    api.getGoalWorkspace.mockRejectedValue(new Error("请求失败（404）"));
    api.listPlanDocuments.mockResolvedValue({plans:[{id:"plan-2",thread_id:"thread-2",title:"骑行减脂计划",version:1,file_status:"ready",created_at:"",updated_at:""}]});
    render(<GoalWorkspacePage resourceId="missing-thread"/>);
    expect(await screen.findByRole("heading",{name:"这个目标暂时无法打开"})).toBeTruthy();
    expect(screen.queryByText("请求失败（404）")).toBeNull();
    expect(screen.getByRole("link",{name:/骑行减脂计划/})).toBeTruthy();
  });

  it("shows the current phase, one next action, and source lineage", async () => {
    render(<GoalWorkspacePage resourceId="plan-1" />);
    expect(await screen.findByRole("heading", { level: 2, name: "两天训练计划" })).toBeTruthy();
    expect(screen.getByText("等待调整确认")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "查看并确认计划调整" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "现在去做" }).getAttribute("href")).toBe("/today?program=program-1");
    expect(screen.getByRole("link", { name: "每日复盘" }).getAttribute("href")).toBe("#goal-review");
    expect(screen.getByText("今天完成了基础训练。")).toBeTruthy();
    const navigation = screen.getByRole("navigation", { name: "目标页面" });
    expect(within(navigation).getByRole("link", { name: "概览" }).getAttribute("aria-current")).toBe("page");
    expect(within(navigation).getByRole("link", { name: "计划" }).getAttribute("href")).toBe("/plans/plan-1");
    expect(within(navigation).getByRole("link", { name: "执行" }).getAttribute("href")).toBe("/today?program=program-1");
    expect(within(navigation).getByRole("link", { name: "继续对话" }).getAttribute("href")).toBe("/threads/thread-1");
  });

  it("filters saved facts locally and can recover from no search results", async () => {
    api.listPlanDocuments.mockResolvedValue({ plans: [
      { id: "plan-1", title: "两天训练计划", version: 2, file_status: "ready", updated_at: "2026-09-12T00:00:00Z" },
      { id: "plan-2", title: "Travel plan", version: null, file_status: "failed", updated_at: "" },
    ] });
    render(<GoalWorkspacePage />);
    await screen.findByRole("link", { name: /Travel plan/ });
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索计划" }), { target: { value: " TRAVEL " } });
    expect(screen.queryByRole("link", { name: /两天训练计划/ })).toBeNull();
    expect(screen.getByText("保存失败")).toBeTruthy();
    expect(screen.getByText("暂无可用版本")).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "不存在的目标" } });
    expect(screen.getByRole("heading", { name: "没有匹配的目标" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "清除搜索" }));
    expect(screen.getByRole("link", { name: /两天训练计划/ })).toBeTruthy();
    expect(api.listPlanDocuments).toHaveBeenCalledTimes(1);
  });

  it("distinguishes loading, failed and empty goal lists and retries", async () => {
    api.listPlanDocuments.mockRejectedValueOnce(new Error("offline"));
    render(<GoalWorkspacePage />);
    expect(screen.getByRole("status").textContent).toContain("正在加载目标");
    expect(await screen.findByRole("heading", { name: "目标列表加载失败" })).toBeTruthy();
    expect(screen.queryByText("还没有保存的目标")).toBeNull();
    api.listPlanDocuments.mockResolvedValue({ plans: [] });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("heading", { name: "还没有保存的目标" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "新建对话" }).getAttribute("href")).toBe("/");
  });

  it("targets the exact action and makes supported sources navigable", async () => {
    const workspace = await api.getGoalWorkspace();
    api.getGoalWorkspace.mockResolvedValue({ ...workspace, phase: "EXECUTING",
      next_action: { kind: "complete_action", label: "完成下一项行动", href: "/today", resource_id: "action-2", reason: "有待完成行动" },
      sources: [
        { kind: "program", id: "program-1", label: "执行计划" },
        { kind: "research", id: "research-1", label: "研究结果" },
        { kind: "memory", id: "episode-1", label: "成长经历" },
        { kind: "unknown", id: "unknown-1", label: "未知记录" },
      ],
    });
    render(<GoalWorkspacePage resourceId="plan-1" />);
    expect((await screen.findByRole("link", { name: "现在去做" })).getAttribute("href")).toBe("/today?program=program-1&action=action-2");
    expect(screen.getByRole("link", { name: "执行计划" }).getAttribute("href")).toBe("/today?program=program-1");
    expect(screen.getByRole("link", { name: "研究结果" }).getAttribute("href")).toBe("/research?job=research-1");
    expect(screen.getByRole("link", { name: "成长经历" }).getAttribute("href")).toBe("/memory");
    expect(screen.queryByRole("link", { name: "未知记录" })).toBeNull();
  });

  it("normalizes a legacy thread resource to its real plan ID without adding history", async () => {
    window.history.replaceState({}, "", "/workspace/thread-1#goal-review");
    const historyLength = window.history.length;
    render(<GoalWorkspacePage resourceId="thread-1" />);
    await screen.findByRole("heading", { name: "两天训练计划" });
    expect(api.getGoalWorkspace).toHaveBeenCalledWith("thread-1");
    expect(window.location.pathname).toBe("/workspace/plan-1");
    expect(window.location.hash).toBe("#goal-review");
    expect(window.history.length).toBe(historyLength);
    expect(window.document.activeElement).toBe(screen.getByRole("region", { name: "目标复盘" }));
  });

  it("keeps current goal visible when its fallback library fails", async () => {
    api.listPlanDocuments.mockRejectedValue(new Error("offline"));
    render(<GoalWorkspacePage resourceId="plan-1" />);
    await screen.findByRole("heading", { name: "两天训练计划" });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("link", { name: "现在去做" })).toBeTruthy();
  });

  it("clears stale goal details when switching to another resource", async () => {
    const workspace = await api.getGoalWorkspace();
    const { rerender } = render(<GoalWorkspacePage resourceId="plan-1" />);
    await screen.findByRole("heading", { name: "两天训练计划" });
    let resolveNext: (value: unknown) => void = () => undefined;
    api.getGoalWorkspace.mockReturnValueOnce(new Promise(resolve => { resolveNext = resolve; }));
    rerender(<GoalWorkspacePage resourceId="plan-2" />);
    expect(screen.queryByRole("heading", { name: "两天训练计划" })).toBeNull();
    resolveNext({ ...workspace, plan_document_id: "plan-2", plan: { ...workspace.plan, id: "plan-2", title: "阅读计划" } });
    await waitFor(() => expect(screen.getByRole("heading", { name: "阅读计划" })).toBeTruthy());
  });
});
