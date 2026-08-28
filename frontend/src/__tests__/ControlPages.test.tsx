import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ModelsPage from "../pages/ModelsPage";
import UsagePage from "../pages/UsagePage";
import EvaluationPage from "../pages/EvaluationPage";
import SkillsPage from "../pages/SkillsPage";

const api = vi.hoisted(() => ({
  listModelProfiles: vi.fn(), listRoutingPolicies: vi.fn(), createModelProfile: vi.fn(), verifyModelVersion: vi.fn(),
  getCostSummary: vi.fn(), getEvaluationRun: vi.fn(), getEvaluationReport: vi.fn(), getEvaluationEvents: vi.fn(), cancelEvaluationRun: vi.fn(),
  listSkillVersions: vi.fn(), getSkills: vi.fn(), listTrustedConnectors: vi.fn(), setSkillVersionEnabled: vi.fn(), uninstallSkill: vi.fn(),
}));
vi.mock("../api", () => api);
afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  api.listModelProfiles.mockResolvedValue({profiles:[{id:"p1",name:"主模型",status:"ACTIVE",created_at:"",updated_at:"",versions:[{id:"pv1",profile_id:"p1",profile_name:"主模型",version:1,provider_protocol:"anthropic",provider_name:"Anthropic",base_url:"https://api.anthropic.com",model_name:"claude",credential_env_ref:"ANTHROPIC_API_KEY",credential_configured:true,capabilities:{text:true,streaming:true,tool_calling:true,json_object:true},context_window:100000,max_output_tokens:4096,timeout_seconds:30,max_attempts:2,config_digest:"digest",status:"ACTIVE",verified_at:null,verification_status:"UNVERIFIED",verification_error_kind:null,created_at:""}]}]});
  api.listRoutingPolicies.mockResolvedValue({policies:[]});
  api.getCostSummary.mockResolvedValue({limit_microusd:100000,reserved_microusd:1000,charged_microusd:2500});
  api.getEvaluationRun.mockResolvedValue({id:"e1",suite_id:"release-v1",baseline_bundle_id:"b",candidate_bundle_id:"c",status:"RUNNING",budget_microusd:10000,attempts:1,created_at:"",updated_at:"",finished_at:null,cancel_requested_at:null});
  api.getEvaluationEvents.mockResolvedValue({events:[{seq:1,type:"evaluation.case.finished",case_id:"case-1",partition:"DEV",domain:"plan",execution_order:"baseline_first"}]});
  api.getEvaluationReport.mockRejectedValue(new Error("尚未完成"));
  api.getSkills.mockResolvedValue({skills:[{skill_id:"s1",name:"travel",title:"旅行计划",description:"生成旅行计划",version_id:"sv1",package_digest:"pkg",enabled:true}]});
  api.listSkillVersions.mockResolvedValue({versions:[{skill_id:"s1",version_id:"sv1",name:"travel",version:"1.0.0",title:"旅行计划",description:"生成旅行计划",content:"",package_digest:"pkg",manifest_digest:"m",requested_tools:["calculator"],granted_tools:["calculator"],connectors:[],phases:["planner"],grant_digest:"g",status:"ENABLED"}]});
  api.listTrustedConnectors.mockResolvedValue({connectors:[]});
});

describe("control plane pages", () => {
  it("shows model versions, protocol, credential and verification state", async () => {
    render(<ModelsPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading", {name:"模型控制台"})).toBeTruthy();
    expect(screen.getByText("Anthropic")).toBeTruthy();
    expect(screen.getByText("凭据已配置")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", {name:"验证连接"}));
    await waitFor(()=>expect(api.verifyModelVersion).toHaveBeenCalledWith("pv1","csrf"));
  });

  it("renders authoritative cost without recomputing unknown values", async () => {
    render(<UsagePage/>);
    expect(await screen.findByRole("heading", {name:"用量与预算"})).toBeTruthy();
    expect(screen.getByText("$0.002500")).toBeTruthy();
    expect(screen.getByText("$0.001000")).toBeTruthy();
  });

  it("shows evaluation progress and a labelled cancel action", async () => {
    render(<EvaluationPage evaluationId="e1" csrfToken="csrf"/>);
    expect(await screen.findByRole("heading", {name:"真实配对评测"})).toBeTruthy();
    expect(screen.getByText("1 / 60")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", {name:"取消评测"}));
    expect(screen.getByRole("dialog", {name:"取消评测？"})).toBeTruthy();
  });

  it("shows installed skills and expands version permissions", async () => {
    render(<SkillsPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading", {name:"Skill 平台"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button", {name:/旅行计划/}));
    expect(await screen.findByText("calculator")).toBeTruthy();
    expect(screen.getByText("已授权工具 1 / 1")).toBeTruthy();
  });
});
