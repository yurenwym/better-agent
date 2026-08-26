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
  getGrowthProfile: vi.fn(),
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
      evaluation: { status: "PASSED", deterministic_pass: true, score_delta: 0.12, regressions: [], passed: 12, total: 12, baseline_correct: 8, candidate_correct: 9, quality_delta: .1, safety_violations: 0 },
      permission_diff: { added: ["memory:read"], removed: [], unchanged: ["conversation:read"] },
      canary: null, created_at: "2026-08-24T00:00:00Z", updated_at: "2026-08-24T00:00:01Z",
    }] });
    api.approveEvolutionCandidate.mockResolvedValue({ id: "candidate-1", status: "APPROVED", version: 4 });
    api.getGrowthProfile.mockResolvedValue({ owner_id: "local-user", metrics: {
      total_programs: 2, completed_programs: 1, completed_actions: 4, skipped_actions: 1,
      deferred_actions: 0, average_difficulty: 3.5, average_actual_minutes: 42, accepted_adjustments: 1,
    }, programs: [{
      id: "program-1", objective_title: "完成第一次骑行", objective_summary: "建立稳定习惯", status: "COMPLETED",
      start_date: "2026-08-01", end_date: "2026-08-07", version: 2,
      progress: { required_completed: 4, required_total: 4, optional_completed: 0, optional_total: 0 },
      completion_summary: "完成了计划", completion_episode_id: "episode-1", source_plan_document_id: "plan-1",
    }] });
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
    expect(screen.getByText("确定性检查 12 / 12 通过")).toBeTruthy();
    expect(screen.getByRole("list")).toBeTruthy();
    expect(screen.getByText("基线正确")).toBeTruthy();
    expect(screen.getByText("候选正确")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "我的成长档案" })).toBeTruthy();
    expect(screen.getByText("完成行动")).toBeTruthy();
    expect(screen.getByText("4/4 必做行动")).toBeTruthy();
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

  it("explains that canary promotion needs both cohorts", async () => {
    api.listEvolutionCandidates.mockResolvedValue({ candidates: [{
      id:"candidate-canary",kind:"prompt",title:"提示词候选",summary:"优化范围",status:"CANARY",version:3,risk_level:"medium",evidence_count:3,
      evaluation:{status:"COMPLETED",deterministic_pass:true,regressions:[],passed:12,total:12},permission_diff:{added:[],removed:[]},
      canary:{sample_size:4,challenger_sample_size:4,champion_sample_size:7,required_samples:20,safety_failures:0,success_failures:0,promotable:false},created_at:"",updated_at:"",
    }] });
    render(<GrowthPage csrfToken="csrf"/>);
    expect(await screen.findByText("挑战组 4/20 · 对照组 7/20")).toBeTruthy();
    expect(screen.getByText("还需 16 个挑战组样本、13 个对照组样本")).toBeTruthy();
  });

  it("labels a manual rollback and old evaluation metrics without implying a safety failure", async () => {
    api.listEvolutionCandidates.mockResolvedValue({ candidates: [{
      id:"candidate-rollback",kind:"prompt",title:"提示词候选",summary:"限制研究范围",status:"ROLLED_BACK",version:4,risk_level:"medium",evidence_count:3,
      evaluation:{status:"COMPLETED",deterministic_pass:true,regressions:[],passed:12,total:12,baseline_correct:null,candidate_correct:null,quality_delta:null,safety_violations:null},
      permission_diff:{added:[],removed:[]},canary:{status:"ROLLED_BACK",sample_size:0},
      rollback:{kind:"manual",actor:"user",reason:"user rollback",occurred_at:"2026-08-25T07:36:55Z"},record_origin:"demo",created_at:"",updated_at:"2026-08-25T07:36:55Z",
    }] });

    render(<GrowthPage csrfToken="csrf"/>);

    expect(await screen.findByText("用户手动回滚")).toBeTruthy();
    expect(screen.getByText("这是一条演示数据，用于验证进化流程，不代表 Agent 从真实任务中自动学习的结果。")).toBeTruthy();
    expect(screen.getByText("旧版评测未记录真实行为指标")).toBeTruthy();
    expect(screen.getByText("确定性检查 12 / 12 通过")).toBeTruthy();
    expect(screen.getByText("回滚分支")).toBeTruthy();
    expect(screen.queryByText("安全失败")).toBeNull();
  });

  it("explains an unavailable behavior evaluation and offers retry instead of exposing internal checks", async () => {
    api.listEvolutionCandidates.mockResolvedValue({ candidates: [{
      id:"candidate-unconfigured",kind:"prompt",title:"提示词候选",summary:"Repeated turn_failed observed in 9 independent conversation experiences.",status:"EVALUATED",version:1,risk_level:"medium",evidence_count:9,record_origin:"observed",
      proposed_content:{prompts:{base:"live-model-v1",improvement:"Address repeated observed failure without changing permissions or core policy."}},
      evaluation:{status:"COMPLETED",deterministic_pass:false,regressions:["behavior_evaluation_configured","real_baseline_bound","real_evaluation_pass","real_safety_pass"],passed:12,total:12,baseline_correct:0,candidate_correct:0,quality_delta:0,safety_violations:null},
      permission_diff:{added:[],removed:[]},canary:null,created_at:"",updated_at:"",
    }] });

    render(<GrowthPage csrfToken="csrf"/>);

    expect(await screen.findByText("真实行为评测未运行")).toBeTruthy();
    expect(screen.getByText("当前未配置评测模型。基础检查已通过，但候选质量和安全性尚未验证，因此暂不可批准。")).toBeTruthy();
    expect(screen.getByRole("button", {name:"重新评测"})).toBeTruthy();
    expect(screen.queryByText(/behavior_evaluation_configured/)).toBeNull();
    expect(screen.getAllByText("对话任务连续出现 9 条独立失败记录。").length).toBeGreaterThan(0);
    expect(screen.getByText("针对重复失败改进提示词，不新增权限，也不改变核心策略。")).toBeTruthy();
  });
});
