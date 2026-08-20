import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlanPage from "../pages/PlanPage";
import { ApiError } from "../api";

const api = vi.hoisted(() => ({
  getPlanDocument: vi.fn(),
  getThreadPlan: vi.fn(),
  getPlans: vi.fn(),
  putPlanDocument: vi.fn(),
  restorePlanDocument: vi.fn(),
  syncPlanFile: vi.fn(),
  retryPlanProjection: vi.fn(),
}));

vi.mock("../api", () => ({ ...api, ApiError: class ApiError extends Error { status = 409; payload: unknown; constructor(message: string, status: number, payload: unknown) { super(message); this.status = status; this.payload = payload; } } }));

afterEach(cleanup);

const current = {
  id: "version-2",
  plan_document_id: "plan-1",
  version: 2,
  base_version_id: "version-1",
  title: "Travel plan",
  markdown: "# Travel plan\n\n## Day 2\nUpdated",
  content_hash: "sha256:v2",
  source_turn_id: null,
  source_message_id: null,
  actor: "user",
  change_summary: "Updated",
  status: "committed",
  created_at: "2026-08-20T00:00:00Z",
  committed_at: "2026-08-20T00:00:00Z",
};

const document = {
  id: "plan-1",
  thread_id: "thread-1",
  title: "Travel plan",
  current_version_id: current.id,
  projected_version_id: current.id,
  file_status: "ready",
  file_path: "plans/plan-1/plan.md",
  created_at: current.created_at,
  updated_at: current.created_at,
  current,
  versions: [
    { ...current, id: "version-1", version: 1, markdown: "# Travel plan\n\n## Day 1\nOriginal", content_hash: "sha256:v1", base_version_id: null },
    current,
  ],
};

describe("PlanPage document editor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getPlanDocument.mockResolvedValue(document);
    api.getThreadPlan.mockResolvedValue({ plan: document });
    api.putPlanDocument.mockResolvedValue(current);
    api.restorePlanDocument.mockResolvedValue(current);
    api.syncPlanFile.mockResolvedValue(current);
    api.retryPlanProjection.mockResolvedValue(document);
  });

  it("loads by stable plan id without requiring a Run and saves exact Markdown with CAS", async () => {
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);

    const editor = await screen.findByRole("textbox", { name: "Markdown editor" });
    expect((editor as HTMLTextAreaElement).value).toBe(current.markdown);
    expect(screen.getByRole("heading", { name: "Day 2" })).toBeTruthy();
    expect(screen.getAllByText("Updated").length).toBeGreaterThan(0);

    fireEvent.change(editor, { target: { value: "# Edited\n" } });
    fireEvent.click(screen.getByRole("button", { name: "Save plan" }));

    await waitFor(() => expect(api.putPlanDocument).toHaveBeenCalledWith(
      "plan-1",
      expect.objectContaining({
        expected_version: 2,
        expected_content_hash: "sha256:v2",
        markdown: "# Edited\n",
      }),
      "csrf",
    ));
  });

  it("keeps the local draft visible when a save hits a 409 conflict", async () => {
    api.putPlanDocument.mockRejectedValue(new ApiError("plan conflict", 409, {
      current: { version: 3, content_hash: "sha256:v3", markdown: "# Server" },
    }));
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);
    const editor = await screen.findByRole("textbox", { name: "Markdown editor" });
    fireEvent.change(editor, { target: { value: "# Local draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Save plan" }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("conflict"));
    expect((editor as HTMLTextAreaElement).value).toBe("# Local draft");
  });

  it("restores a selected history version as a new revision", async () => {
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);
    await screen.findByRole("textbox", { name: "Markdown editor" });
    fireEvent.click(screen.getByRole("button", { name: "Restore version 1" }));

    await waitFor(() => expect(api.restorePlanDocument).toHaveBeenCalledWith(
      "plan-1",
      { version: 1, expected_version: 2, expected_content_hash: "sha256:v2" },
      "csrf",
    ));
  });

  it("shows the fixed document source for an execution projection", async () => {
    api.getPlans.mockResolvedValue({
      current: {
        id: "execution-1",
        run_id: "run-1",
        goal_id: "goal-1",
        version: 1,
        status: "draft",
        summary: "Compiled execution",
        source_document_version_id: "version-1",
        steps: [{ id: "step-1", title: "Track", description: "Track it", status: "pending", position: 0 }],
      },
      history: [],
    });
    api.getPlanDocument.mockResolvedValue(document);
    render(
      <PlanPage
        csrfToken="csrf"
        run={{
          id: "run-1",
          goal_id: "goal-1",
          session_id: "session-1",
          state: "AWAITING_APPROVAL",
          resume_state: null,
          current_plan_version_id: "execution-1",
          current_step_id: null,
          version: 1,
          budget: {},
          pending_approvals: [],
          source_plan_document_id: "plan-1",
          source_plan_document_version_id: "version-1",
          source_plan_content_hash: "sha256:v1",
        }}
        onRun={vi.fn()}
      />,
    );

    expect((await screen.findByText(/执行来源：Travel plan/)).textContent).toContain("文档 v1");
  });
});
