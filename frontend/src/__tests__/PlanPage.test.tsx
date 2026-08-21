import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlanPage from "../pages/PlanPage";
import { ApiError } from "../api";

const api = vi.hoisted(() => ({
  getPlanDocument: vi.fn(),
  getPlanVersion: vi.fn(),
  getThreadPlan: vi.fn(),
  getPlans: vi.fn(),
  putPlanDocument: vi.fn(),
  deletePlanDocument: vi.fn(),
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
    api.deletePlanDocument.mockResolvedValue(undefined);
    api.restorePlanDocument.mockResolvedValue(current);
    api.syncPlanFile.mockResolvedValue(current);
    api.retryPlanProjection.mockResolvedValue(document);
    api.getPlanVersion.mockResolvedValue({ ...current, version: 1, id: "version-1", markdown: "# Travel plan\n\n## Day 1\nOriginal" });
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

  it("deletes the current plan with its CAS head and leaves the workspace", async () => {
    const onDeleted = vi.fn();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} onDeleted={onDeleted} />);

    await screen.findByRole("textbox", { name: "Markdown editor" });
    fireEvent.click(screen.getByRole("button", { name: "Delete plan" }));

    await waitFor(() => expect(api.deletePlanDocument).toHaveBeenCalledWith(
      "plan-1",
      { expected_version: 2, expected_content_hash: "sha256:v2" },
      "csrf",
    ));
    expect(onDeleted).toHaveBeenCalledOnce();
  });

  it("marks the document retryable when file projection returns a recoverable error", async () => {
    api.putPlanDocument.mockRejectedValue(new ApiError("projection failed", 503, {
      retry: true,
      current: { file_status: "failed" },
    }));
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);
    const editor = await screen.findByRole("textbox", { name: "Markdown editor" });
    fireEvent.change(editor, { target: { value: "# Local draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Save plan" }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("写入失败"));
    expect(screen.getByRole("button", { name: "Retry file write" })).toBeTruthy();
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

  it("does not offer restore for a prepared history candidate", async () => {
    api.getPlanDocument.mockResolvedValue({
      ...document,
      versions: [...document.versions, { ...current, id: "prepared-3", version: 3, status: "prepared", markdown: "# Prepared" }],
    });
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);
    await screen.findByRole("textbox", { name: "Markdown editor" });

    expect(screen.queryByRole("button", { name: "Restore version 3" })).toBeNull();
  });

  it("loads Markdown on demand when the history list contains metadata only", async () => {
    const metadataOnly = {
      ...document,
      versions: document.versions.map(({ markdown: _markdown, ...version }) => version),
    };
    api.getPlanDocument.mockResolvedValue(metadataOnly);
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);
    await screen.findByRole("textbox", { name: "Markdown editor" });

    fireEvent.click(screen.getByRole("button", { name: "v1" }));

    await waitFor(() => expect(api.getPlanVersion).toHaveBeenCalledWith("plan-1", 1));
    expect((screen.getByRole("textbox", { name: "Markdown editor" }) as HTMLTextAreaElement).value).toContain("Day 1");
  });

  it("keeps a pending document addressable and exposes a projection retry", async () => {
    const pending = { ...document, current: null, current_version_id: null, projected_version_id: null, file_status: "failed" };
    api.getPlanDocument.mockResolvedValue(pending);
    api.retryPlanProjection.mockResolvedValue(pending);

    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);

    expect(await screen.findByText("计划文档还没有可用版本")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试写入计划文件" }));

    await waitFor(() => expect(api.retryPlanProjection).toHaveBeenCalledWith("plan-1", "csrf"));
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

  it("keeps execution approval and revision controls visible beside the document editor", async () => {
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

    render(
      <PlanPage
        csrfToken="csrf"
        planId="plan-1"
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

    await screen.findByRole("textbox", { name: "Markdown editor" });
    expect(await screen.findByRole("button", { name: "批准计划" })).toBeTruthy();
    expect(screen.getByText("调整未完成步骤")).toBeTruthy();
  });
});
