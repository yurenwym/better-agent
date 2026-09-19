import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ExpertRunCard from "../components/ExpertRunCard";
import type { AgentArtifact, AgentRun, AgentTask } from "../types";

afterEach(cleanup);

describe("ExpertRunCard", () => {
  it("shows readable expert progress and safe result fields without chain of thought", () => {
    const run = {
      id: "agent-run-1", objective: "比较三个方案", mode: "expert", status: "RUNNING",
      thread_id: "thread-1", runtime_bundle_id: "bundle-1", budget_units: 8,
      reserved_budget_units: 3, version: 1, created_at: "2026-08-24T00:00:00Z",
      updated_at: "2026-08-24T00:00:01Z", finished_at: null, cancel_requested_at: null,
    } satisfies AgentRun;
    const tasks = [{
      id: "task-1", agent_run_id: run.id, root_task_id: "task-1", parent_task_id: null,
      child_key: "research", role: "researcher", objective: "核对依据", output_schema: "brief.v1",
      status: "SUCCEEDED", priority: 0, join_policy: null, attempts: 1, max_attempts: 2,
      lease_epoch: 1, budget_units: 2, result_artifact_id: "artifact-1", error_code: null,
      cancel_requested_at: null, cancel_reason: null, version: 1, created_at: run.created_at,
      updated_at: run.updated_at, finished_at: run.updated_at,
    }] satisfies AgentTask[];
    const artifacts = [{
      id: "artifact-1", task_id: "task-1", artifact_type: "brief", created_at: run.updated_at,
      source_refs: ["source-1", "source-2"],
      content: {
        summary: "## 方案 A 风险最低\n\n- 第一项安排",
        findings: ["成本可控"],
        risks: ["样本仍少"],
        open_questions: ["是否扩大范围"],
        chain_of_thought: "绝不能显示的内部推理",
      },
    }] satisfies AgentArtifact[];

    render(<ExpertRunCard run={run} tasks={tasks} artifacts={artifacts} onCancel={vi.fn()} />);

    expect(screen.getByRole("region", { name: "专家协同任务" }).textContent).toContain("专家正在协作");
    expect(screen.getByText("研究专家")).toBeTruthy();
    expect(screen.getByText("方案 A 风险最低")).toBeTruthy();
    expect(screen.getByRole("heading", {name:"方案 A 风险最低"})).toBeTruthy();
    expect(screen.queryByText("核对依据")).toBeNull();
    expect(screen.getByText("2 条证据")).toBeTruthy();
    expect(screen.queryByText(/绝不能显示的内部推理/)).toBeNull();
    expect(screen.queryByText(/chain_of_thought/)).toBeNull();
  });

  it("offers cancellation while work is active", () => {
    const onCancel = vi.fn();
    render(<ExpertRunCard run={{ id: "agent-run-1", objective: "分析", mode: "expert", status: "WAITING", thread_id: "thread-1", runtime_bundle_id: "bundle-1", budget_units: 8, reserved_budget_units: 2, version: 1, created_at: "2026-08-24T00:00:00Z", updated_at: "2026-08-24T00:00:01Z", finished_at: null, cancel_requested_at: null }} tasks={[]} artifacts={[]} onCancel={onCancel} />);

    fireEvent.click(screen.getByRole("button", { name: "取消专家任务" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("shows a readable model configuration failure", () => {
    const onOpenModels = vi.fn();
    const run = { id: "agent-run-2", objective: "制定骑行计划", mode: "expert", status: "FAILED", thread_id: "thread-1", runtime_bundle_id: "bundle-1", budget_units: 8, reserved_budget_units: 0, version: 2, created_at: "2026-08-24T00:00:00Z", updated_at: "2026-08-24T00:00:01Z", finished_at: "2026-08-24T00:00:01Z", cancel_requested_at: null } satisfies AgentRun;
    const tasks = [{ id: "task-2", agent_run_id: run.id, root_task_id: "task-2", parent_task_id: null, child_key: null, role: "coordinator", objective: run.objective, output_schema: "brief.v1", status: "FAILED", priority: 0, join_policy: null, attempts: 1, max_attempts: 1, lease_epoch: 1, budget_units: 1, result_artifact_id: null, error_code: "MODEL_NOT_CONFIGURED", cancel_requested_at: null, cancel_reason: null, version: 2, created_at: run.created_at, updated_at: run.updated_at, finished_at: run.finished_at }] satisfies AgentTask[];

    render(<ExpertRunCard run={run} tasks={tasks} artifacts={[]} onOpenModels={onOpenModels} />);

    expect(screen.getByRole("alert").textContent).toContain("尚未配置可用模型");
    fireEvent.click(screen.getByRole("button", { name: "前往模型配置" }));
    expect(onOpenModels).toHaveBeenCalledTimes(1);
  });
});
