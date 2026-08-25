import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlanPage from "../pages/PlanPage";
import { ApiError } from "../api";

const api = vi.hoisted(() => ({
  listPlanDocuments: vi.fn(),
  getPlanDocument: vi.fn(),
  getPlanVersion: vi.fn(),
  getThreadPlan: vi.fn(),
  getPlans: vi.fn(),
  putPlanDocument: vi.fn(),
  deletePlanDocument: vi.fn(),
  restorePlanDocument: vi.fn(),
  syncPlanFile: vi.fn(),
  retryPlanProjection: vi.fn(),
  previewGoalProgram: vi.fn(),
  activateGoalProgram: vi.fn(),
  listGoalPrograms: vi.fn(),
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

async function enterEditMode() {
  fireEvent.click(await screen.findByRole("button", { name: "编辑计划" }));
  return screen.findByRole("region", { name: "可视化计划编辑器" });
}

describe("PlanPage document editor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listPlanDocuments.mockResolvedValue({ plans: [
      { id: "plan-1", thread_id: "thread-1", title: "Travel plan", version: 2, file_status: "ready", created_at: current.created_at, updated_at: current.created_at },
      { id: "plan-2", thread_id: "thread-2", title: "Training plan", version: 1, file_status: "ready", created_at: current.created_at, updated_at: current.created_at },
    ] });
    api.getPlanDocument.mockResolvedValue(document);
    api.getThreadPlan.mockResolvedValue({ plan: document });
    api.putPlanDocument.mockResolvedValue(current);
    api.deletePlanDocument.mockResolvedValue(undefined);
    api.restorePlanDocument.mockResolvedValue(current);
    api.syncPlanFile.mockResolvedValue(current);
    api.retryPlanProjection.mockResolvedValue(document);
    api.previewGoalProgram.mockResolvedValue(null);
    api.activateGoalProgram.mockResolvedValue(null);
    api.listGoalPrograms.mockResolvedValue({ programs: [] });
    api.getPlanVersion.mockResolvedValue({ ...current, version: 1, id: "version-1", markdown: "# Travel plan\n\n## Day 1\nOriginal" });
  });

  it("lists saved plan titles and selects a plan by stable id", async () => {
    const onSelectPlan = vi.fn();
    render(<PlanPage csrfToken="csrf" planId={null} threadId={null} run={null} onRun={vi.fn()} onSelectPlan={onSelectPlan} />);

    const list = await screen.findByRole("navigation", { name: "Saved plans" });
    expect(list.textContent).toContain("Travel plan");
    expect(list.textContent).toContain("Training plan");
    fireEvent.click(screen.getByRole("button", { name: /Training plan/ }));

    expect(onSelectPlan).toHaveBeenCalledWith("plan-2");
  });

  it("keeps the detail pane empty until the user selects a saved plan", async () => {
    render(<PlanPage csrfToken="csrf" planId={null} threadId={null} run={null} onRun={vi.fn()} />);

    await screen.findByRole("button", { name: /Training plan/ });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(api.getPlanDocument).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "选择一个计划" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "Markdown editor" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Training plan/ }));

    await waitFor(() => expect(api.getPlanDocument).toHaveBeenCalledWith("plan-2"));
    expect((await screen.findAllByRole("heading", { name: "Travel plan" })).length).toBeGreaterThan(0);
  });

  it("lets the user choose an explicit execution end date", async () => {
    api.previewGoalProgram.mockResolvedValue({ id: "program-1", status: "DRAFT", version: 1, structure: { assumptions: [], actions: [] } });
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "开始执行" }));
    fireEvent.change(screen.getByLabelText("执行开始日期"), { target: { value: "2026-09-01" } });
    fireEvent.change(screen.getByLabelText("执行结束日期"), { target: { value: "2026-09-02" } });
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));

    await waitFor(() => expect(api.previewGoalProgram).toHaveBeenCalledWith(
      "plan-1",
      expect.objectContaining({ start_date: "2026-09-01", requested_end_date: "2026-09-02" }),
      expect.any(String),
      "csrf",
    ));
  });

  it("shows the linked execution instead of offering a duplicate start", async () => {
    api.listGoalPrograms.mockResolvedValue({programs:[{id:"program-1",source_plan_document_id:"plan-1",objective_title:"Travel plan",status:"PAUSED",version:3,progress:{required_completed:2,required_total:7,completion_rate:2/7,completion_ready:false}}]});
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);

    expect(await screen.findByText("执行已暂停")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "开始执行" })).toBeNull();
  });

  it("starts in rendered mode and saves edits made in the visual plan", async () => {
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "编辑计划" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "Markdown editor" })).toBeNull();
    const editor = await enterEditMode();
    const heading = within(editor).getByRole("heading", { name: "Day 2" });
    heading.textContent = "Updated day";
    fireEvent.input(heading);
    fireEvent.click(screen.getByRole("button", { name: "保存计划" }));

    await waitFor(() => expect(api.putPlanDocument).toHaveBeenCalledWith(
      "plan-1",
      expect.objectContaining({ markdown: expect.stringContaining("## Updated day") }),
      "csrf",
    ));
  });

  it("cancels visual editing without changing the saved view", async () => {
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);

    const editor = await enterEditMode();
    const heading = within(editor).getByRole("heading", { name: "Day 2" });
    heading.textContent = "Local draft";
    fireEvent.input(heading);
    fireEvent.click(screen.getByRole("button", { name: "取消编辑" }));

    expect(screen.queryByRole("region", { name: "可视化计划编辑器" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Markdown editor" })).toBeNull();
    expect(screen.getAllByRole("heading", { name: "Day 2" }).length).toBeGreaterThan(0);
  });

  it("loads by stable plan id without requiring a Run and saves exact Markdown with CAS", async () => {
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);

    const editor = await enterEditMode();
    expect(within(editor).getByRole("heading", { name: "Travel plan" })).toBeTruthy();
    expect(within(editor).getByRole("heading", { name: "Day 2" })).toBeTruthy();
    expect(screen.getAllByText("Updated").length).toBeGreaterThan(0);

    const heading = within(editor).getByRole("heading", { name: "Travel plan" });
    heading.textContent = "Edited";
    fireEvent.input(heading);
    fireEvent.click(screen.getByRole("button", { name: "保存计划" }));

    await waitFor(() => expect(api.putPlanDocument).toHaveBeenCalledWith(
      "plan-1",
      expect.objectContaining({
        expected_version: 2,
        expected_content_hash: "sha256:v2",
        markdown: expect.stringContaining("# Edited"),
      }),
      "csrf",
    ));
  });

  it("keeps the local draft visible when a save hits a 409 conflict", async () => {
    api.putPlanDocument.mockRejectedValue(new ApiError("plan conflict", 409, {
      current: { version: 3, content_hash: "sha256:v3", markdown: "# Server" },
    }));
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);
    const editor = await enterEditMode();
    const heading = within(editor).getByRole("heading", { name: "Day 2" });
    heading.textContent = "Local draft";
    fireEvent.input(heading);
    fireEvent.click(screen.getByRole("button", { name: "保存计划" }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("conflict"));
    expect(within(editor).getByRole("heading", { name: "Local draft" })).toBeTruthy();
  });

  it("deletes the current plan with its CAS head and returns to the plan library", async () => {
    const onDeleted = vi.fn();
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} onDeleted={onDeleted} />);

    await screen.findByRole("button", { name: "删除计划" });
    fireEvent.click(screen.getByRole("button", { name: "删除计划" }));
    expect(api.deletePlanDocument).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "删除计划？" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(api.deletePlanDocument).toHaveBeenCalledWith(
      "plan-1",
      { expected_version: 2, expected_content_hash: "sha256:v2" },
      "csrf",
    ));
    expect(onDeleted).toHaveBeenCalledOnce();
    expect(screen.getByRole("heading", { name: "选择一个计划" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Travel plan/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Training plan/ })).toBeTruthy();
    expect(screen.getByText("计划已删除")).toBeTruthy();
  });

  it("shows a deletion failure notice and keeps the plan", async () => {
    api.deletePlanDocument.mockRejectedValueOnce(new Error("busy"));
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "删除计划" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    expect(await screen.findByText("计划删除失败，请稍后重试")).toBeTruthy();
    expect(screen.getAllByRole("heading", { name: "Travel plan" }).length).toBeGreaterThan(0);
  });

  it("marks the document retryable when file projection returns a recoverable error", async () => {
    api.putPlanDocument.mockRejectedValue(new ApiError("projection failed", 503, {
      retry: true,
      current: { file_status: "failed" },
    }));
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);
    const editor = await enterEditMode();
    const heading = within(editor).getByRole("heading", { name: "Day 2" });
    heading.textContent = "Local draft";
    fireEvent.input(heading);
    fireEvent.click(screen.getByRole("button", { name: "保存计划" }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("写入失败"));
    expect(screen.getByRole("button", { name: "Retry file write" })).toBeTruthy();
    expect(within(editor).getByRole("heading", { name: "Local draft" })).toBeTruthy();
  });

  it("restores a selected history version as a new revision", async () => {
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);
    await enterEditMode();
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
    await enterEditMode();

    expect(screen.queryByRole("button", { name: "Restore version 3" })).toBeNull();
  });

  it("loads Markdown on demand when the history list contains metadata only", async () => {
    const metadataOnly = {
      ...document,
      versions: document.versions.map(({ markdown: _markdown, ...version }) => version),
    };
    api.getPlanDocument.mockResolvedValue(metadataOnly);
    render(<PlanPage csrfToken="csrf" planId="plan-1" run={null} onRun={vi.fn()} />);
    const editor = await enterEditMode();

    fireEvent.click(screen.getByRole("button", { name: "v1" }));

    await waitFor(() => expect(api.getPlanVersion).toHaveBeenCalledWith("plan-1", 1));
    await waitFor(() => expect(within(editor).getByRole("heading", { name: "Day 1" })).toBeTruthy());
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

    await screen.findByRole("button", { name: "编辑计划" });
    expect(await screen.findByRole("button", { name: "批准计划" })).toBeTruthy();
    expect(screen.getByText("调整未完成步骤")).toBeTruthy();
  });
});
