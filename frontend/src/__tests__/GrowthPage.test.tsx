import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GrowthPage from "../pages/GrowthPage";

const api = vi.hoisted(() => ({
  listEvolutionCandidates: vi.fn(),
  evaluateEvolutionCandidate: vi.fn(),
  approveEvolutionCandidate: vi.fn(),
  rejectEvolutionCandidate: vi.fn(),
  startEvolutionCanary: vi.fn(),
  promoteEvolutionCandidate: vi.fn(),
  rollbackEvolutionCandidate: vi.fn(),
}));

vi.mock("../api", () => api);
afterEach(cleanup);

describe("GrowthPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listEvolutionCandidates.mockResolvedValue({ candidates: [{
      id: "candidate-1", kind: "policy", title: "减少不必要追问", summary: "仅在关键信息缺失时询问。",
      status: "PENDING_APPROVAL", version: 3, risk_level: "medium", evidence_count: 24,
      reason: "多次研究都扩大了用户没有要求的范围。",
      proposed_content: { prompts: "research-scope-bounded" },
      evaluation: { status: "PASSED", deterministic_pass: true, score_delta: 0.12, regressions: [], passed: 12, total: 12 },
      permission_diff: { added: ["memory:read"], removed: [], unchanged: ["conversation:read"] },
      canary: null, created_at: "2026-08-24T00:00:00Z", updated_at: "2026-08-24T00:00:01Z",
    }] });
    api.approveEvolutionCandidate.mockResolvedValue({ id: "candidate-1", status: "APPROVED", version: 4 });
  });

  it("shows candidate evidence, evaluation and permission changes", async () => {
    render(<GrowthPage csrfToken="csrf" />);

    expect(await screen.findByRole("heading", { name: "Agent 正在怎样变得更好" })).toBeTruthy();
    expect(screen.getByText("减少不必要追问")).toBeTruthy();
    expect(screen.getByText("评测已通过")).toBeTruthy();
    expect(screen.getByText("新增权限 1 项")).toBeTruthy();
    expect(screen.getByText("24 条证据")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "发现的问题" })).toBeTruthy();
    expect(screen.getByText("多次研究都扩大了用户没有要求的范围。")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "准备怎样改变" })).toBeTruthy();
    expect(screen.getByText("限制研究范围，只生成用户明确要求的内容")).toBeTruthy();
    expect(screen.getByText("12 / 12 项检查通过")).toBeTruthy();
  });

  it("approves with the expected candidate version", async () => {
    render(<GrowthPage csrfToken="csrf" />);
    fireEvent.click(await screen.findByRole("button", { name: "批准候选" }));

    await waitFor(() => expect(api.approveEvolutionCandidate).toHaveBeenCalledWith("candidate-1", 3, "csrf"));
  });

  it("does not expose an unreadable stored summary", async () => {
    api.listEvolutionCandidates.mockResolvedValue({ candidates: [{
      id:"candidate-broken",kind:"prompt",title:"提示词候选",summary:"????????",reason:"????????",proposed_content:{prompts:"live-model-v1-research-scope-bounded"},
      status:"CANARY",version:3,risk_level:"medium",evidence_count:3,evaluation:{status:"COMPLETED",deterministic_pass:true,regressions:[],passed:12,total:12},permission_diff:{added:[],removed:[]},canary:{sample_size:0},created_at:"",updated_at:"",
    }] });
    render(<GrowthPage csrfToken="csrf"/>);
    expect((await screen.findAllByText("研究任务曾多次扩大用户没有要求的范围。")).length).toBeGreaterThan(0);
    expect(screen.queryByText("????????")).toBeNull();
  });
});
