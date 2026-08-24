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
      evaluation: { status: "PASSED", deterministic_pass: true, score_delta: 0.12, regressions: [] },
      permission_diff: { added: ["memory:read"], removed: [], unchanged: ["conversation:read"] },
      canary: null, created_at: "2026-08-24T00:00:00Z", updated_at: "2026-08-24T00:00:01Z",
    }] });
    api.approveEvolutionCandidate.mockResolvedValue({ id: "candidate-1", status: "APPROVED", version: 4 });
  });

  it("shows candidate evidence, evaluation and permission changes", async () => {
    render(<GrowthPage csrfToken="csrf" />);

    expect(await screen.findByRole("heading", { name: "受控成长" })).toBeTruthy();
    expect(screen.getByText("减少不必要追问")).toBeTruthy();
    expect(screen.getByText("评测已通过")).toBeTruthy();
    expect(screen.getByText("新增权限 1 项")).toBeTruthy();
    expect(screen.getByText("24 条证据")).toBeTruthy();
  });

  it("approves with the expected candidate version", async () => {
    render(<GrowthPage csrfToken="csrf" />);
    fireEvent.click(await screen.findByRole("button", { name: "批准候选" }));

    await waitFor(() => expect(api.approveEvolutionCandidate).toHaveBeenCalledWith("candidate-1", 3, "csrf"));
  });
});
