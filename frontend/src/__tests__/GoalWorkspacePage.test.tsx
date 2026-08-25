import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GoalWorkspacePage from "../pages/GoalWorkspacePage";

const api = vi.hoisted(() => ({ getGoalWorkspace: vi.fn() }));
vi.mock("../api", () => api);
afterEach(cleanup);

describe("GoalWorkspacePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getGoalWorkspace.mockResolvedValue({
      resource_id: "plan-1", thread_id: "thread-1", plan_document_id: "plan-1", phase: "ADJUSTING",
      next_action: { kind: "accept_adjustment", label: "查看并确认计划调整", href: "/today", resource_id: "proposal-1", reason: "复盘提出了计划调整，等待你的确认。" },
      sources: [{ kind: "plan", id: "plan-1", label: "计划 v2" }, { kind: "review", id: "review-1", label: "每日复盘" }],
      plan: { id: "plan-1", title: "两天训练计划", version: 2, file_status: "ready" }, program: null, today: null, review: null, research: [],
      growth: { episode_id: null, episode_count: 0, latest_summary: null },
    });
  });

  it("shows the current phase, one next action, and source lineage", async () => {
    render(<GoalWorkspacePage resourceId="plan-1" />);
    expect(await screen.findByRole("heading", { level: 2, name: "两天训练计划" })).toBeTruthy();
    expect(screen.getByText("等待调整确认")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "查看并确认计划调整" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "现在去做" }).getAttribute("href")).toBe("/today");
    expect(screen.getByText("每日复盘")).toBeTruthy();
  });
});
