import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlanPage from "../pages/PlanPage";
import SkillsPage from "../pages/SkillsPage";

const api = vi.hoisted(() => ({
  ApiError: class ApiError extends Error { status: number; payload: unknown; constructor(message: string, status: number, payload: unknown) { super(message); this.status = status; this.payload = payload; } },
  confirmSkillInstall: vi.fn(), listInstalledSkills: vi.fn(), listSkillVersions: vi.fn(), listTrustedConnectors: vi.fn(), previewSkillInstall: vi.fn(), setSkillVersionEnabled: vi.fn(), uninstallSkill: vi.fn(),
  approvePlan: vi.fn(), cancelStep: vi.fn(), deletePlanDocument: vi.fn(), getPlanDocument: vi.fn(), getPlanVersion: vi.fn(), getPlans: vi.fn(), getThreadPlan: vi.fn(), listGoalPrograms: vi.fn(), listPlanDocuments: vi.fn(), putPlanDocument: vi.fn(), restorePlanDocument: vi.fn(), retryGoalProgramCompile: vi.fn(), retryPlanProjection: vi.fn(), revisePlan: vi.fn(), syncPlanFile: vi.fn(), previewGoalProgram: vi.fn(), activateGoalProgram: vi.fn(),
}));
vi.mock("../api", () => api);
afterEach(cleanup);

const skillVersion = {
  skill_id: "s1", version_id: "sv1", name: "travel", version: "1.0.0", title: "旅行计划", description: "生成旅行计划", content: "",
  package_digest: "pkg", manifest_digest: "m", requested_tools: ["calculator"], granted_tools: ["calculator"], connectors: [], phases: ["planner"], grant_digest: "g", status: "ENABLED",
};

const current = {
  id: "version-2", plan_document_id: "plan-1", version: 2, base_version_id: "version-1", title: "Travel plan",
  markdown: "# Travel plan\n\n## Day 2\nUpdated", content_hash: "sha256:v2", source_turn_id: null, source_message_id: null,
  actor: "user", change_summary: "Updated", status: "committed", created_at: "2026-08-20T00:00:00Z", committed_at: "2026-08-20T00:00:00Z",
};
const document = {
  id: "plan-1", thread_id: "thread-1", title: "Travel plan", current_version_id: current.id, projected_version_id: current.id,
  file_status: "ready", file_path: "plans/plan-1/plan.md", created_at: current.created_at, updated_at: current.created_at,
  current, versions: [current],
};
const run = {
  id: "run-1", goal_id: "goal-1", session_id: "session-1", state: "AWAITING_APPROVAL" as const, resume_state: null,
  current_plan_version_id: "execution-1", current_step_id: null, version: 1, budget: {}, pending_approvals: [] as never[],
  source_plan_document_id: "plan-1", source_plan_document_version_id: "version-1", source_plan_content_hash: "sha256:v1",
};

beforeEach(() => {
  vi.clearAllMocks();
  api.listInstalledSkills.mockResolvedValue({ skills: [] });
  api.listTrustedConnectors.mockResolvedValue({ connectors: [] });
  api.listSkillVersions.mockResolvedValue({ versions: [skillVersion] });
  api.listPlanDocuments.mockResolvedValue({ plans: [] });
  api.getPlanDocument.mockResolvedValue(document);
  api.listGoalPrograms.mockResolvedValue({ programs: [] });
  api.getPlans.mockResolvedValue({
    current: {
      id: "execution-1", run_id: "run-1", goal_id: "goal-1", version: 1, status: "draft", summary: "Compiled execution",
      source_document_version_id: "version-1",
      steps: [{ id: "step-1", title: "Track", description: "Track it", status: "pending", position: 0 }],
    },
    history: [],
  });
});

describe("console loading and failure states", () => {
  it("hides the empty Skill state until the first load finishes", async () => {
    let resolveSkills: (value: { skills: unknown[] }) => void = () => undefined;
    api.listInstalledSkills.mockReturnValue(new Promise((resolve) => { resolveSkills = resolve; }));
    render(<SkillsPage csrfToken="csrf" />);

    expect(screen.queryByText("暂无 Skill")).toBeNull();
    expect(screen.queryByText("暂无连接器")).toBeNull();
    expect(screen.getAllByRole("status").length).toBeGreaterThan(0);

    resolveSkills({ skills: [] });
    expect(await screen.findByText("暂无 Skill")).toBeTruthy();
  });

  it("surfaces a visible error when approving the plan fails", async () => {
    api.approvePlan.mockRejectedValueOnce(new Error("批准请求被拒绝"));
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={run} onRun={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "批准计划" }));

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("批准请求被拒绝");
    expect((screen.getByRole("button", { name: "批准计划" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
