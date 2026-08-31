import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ModelsPage from "../pages/ModelsPage";
import UsagePage from "../pages/UsagePage";
import EvaluationPage from "../pages/EvaluationPage";
import SkillsPage from "../pages/SkillsPage";

const api = vi.hoisted(() => ({
  listModelProfiles: vi.fn(), listRoutingPolicies: vi.fn(), createModelProfile: vi.fn(), createRoutingPolicy: vi.fn(), verifyModelVersion: vi.fn(),
  getCostSummary: vi.fn(), getUsageSummary: vi.fn(), setCostBudget: vi.fn(), listEvaluationSuites: vi.fn(), createEvaluationRun: vi.fn(), getEvaluationRun: vi.fn(), getEvaluationReport: vi.fn(), getEvaluationEvents: vi.fn(), subscribeToEvaluationEvents: vi.fn(), cancelEvaluationRun: vi.fn(), resumeEvaluationRun: vi.fn(),
  listSkillVersions: vi.fn(), getSkills: vi.fn(), listInstalledSkills: vi.fn(), listTrustedConnectors: vi.fn(), setSkillVersionEnabled: vi.fn(), uninstallSkill: vi.fn(), previewSkillInstall: vi.fn(), confirmSkillInstall: vi.fn(),
}));
vi.mock("../api", () => api);
afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  api.listModelProfiles.mockResolvedValue({profiles:[{id:"p1",name:"主模型",status:"ACTIVE",created_at:"",updated_at:"",versions:[{id:"pv1",profile_id:"p1",profile_name:"主模型",version:1,provider_protocol:"anthropic",provider_name:"Anthropic",base_url:"https://api.anthropic.com",model_name:"claude",credential_env_ref:"ANTHROPIC_API_KEY",credential_configured:true,capabilities:{text:true,streaming:true,tool_calling:true,json_object:true},context_window:100000,max_output_tokens:4096,timeout_seconds:30,max_attempts:2,config_digest:"digest",status:"ACTIVE",verified_at:null,verification_status:"UNVERIFIED",verification_error_kind:null,created_at:""}]}]});
  api.listRoutingPolicies.mockResolvedValue({policies:[]});
  api.createModelProfile.mockResolvedValue({id:"p2",name:"规划模型",status:"ACTIVE",created_at:"",updated_at:"",versions:[]});
  api.createRoutingPolicy.mockResolvedValue({id:"route",name:"默认策略",version:1,roles:{},policy_digest:"digest",created_at:""});
  api.getCostSummary.mockResolvedValue({limit_microusd:100000,reserved_microusd:1000,charged_microusd:2500});
  api.getUsageSummary.mockResolvedValue({groups:[{role:"planner",provider:"供应商",profile_version_id:"pv1",attempts:2,succeeded:2,fallbacks:1,cost_microusd:2500,unknown_cost_attempts:1,success_rate:1,fallback_rate:.5,ttft_seconds:.2,tps:30,p95_latency_seconds:1.2}]});
  api.setCostBudget.mockResolvedValue({limit_microusd:200000,reserved_microusd:1000,charged_microusd:2500});
  api.listEvaluationSuites.mockResolvedValue({suites:[{id:"release-v1",digest:"suite-digest",kind:"release",case_count:60}]});
  api.createEvaluationRun.mockResolvedValue({id:"e2",suite_id:"release-v1",baseline_bundle_id:"stable",candidate_bundle_id:"candidate",status:"QUEUED",budget_microusd:10000,attempts:0,created_at:"",updated_at:"",finished_at:null,cancel_requested_at:null});
  api.getEvaluationRun.mockResolvedValue({id:"e1",suite_id:"release-v1",baseline_bundle_id:"b",candidate_bundle_id:"c",status:"RUNNING",budget_microusd:10000,attempts:1,created_at:"",updated_at:"",finished_at:null,cancel_requested_at:null});
  api.getEvaluationEvents.mockResolvedValue({events:[{seq:1,type:"evaluation.case.finished",case_id:"case-1",partition:"DEV",domain:"plan",execution_order:"baseline_first"}]});
  api.subscribeToEvaluationEvents.mockReturnValue(()=>undefined);
  api.getEvaluationReport.mockRejectedValue(new Error("尚未完成"));
  api.resumeEvaluationRun.mockResolvedValue({id:"e1",suite_id:"release-v1",baseline_bundle_id:"b",candidate_bundle_id:"c",status:"QUEUED",budget_microusd:20000,attempts:2,created_at:"",updated_at:"",finished_at:null,cancel_requested_at:null});
  api.getSkills.mockResolvedValue({skills:[{skill_id:"s1",name:"travel",title:"旅行计划",description:"生成旅行计划",version_id:"sv1",package_digest:"pkg",enabled:true}]});
  api.listInstalledSkills.mockResolvedValue({skills:[{skill_id:"s1",name:"travel",title:"旅行计划",description:"生成旅行计划",version_id:"sv1",package_digest:"pkg",enabled:true}]});
  api.listSkillVersions.mockResolvedValue({versions:[{skill_id:"s1",version_id:"sv1",name:"travel",version:"1.0.0",title:"旅行计划",description:"生成旅行计划",content:"",package_digest:"pkg",manifest_digest:"m",requested_tools:["calculator"],granted_tools:["calculator"],connectors:[],phases:["planner"],grant_digest:"g",status:"ENABLED"}]});
  api.listTrustedConnectors.mockResolvedValue({connectors:[]});
  api.previewSkillInstall.mockResolvedValue({install_token:"token",name:"travel",version:"1.1.0",title:"旅行计划",description:"生成旅行计划",requested_tools:["calculator","web_search"],connectors:[],phases:["planner"],manifest_digest:"manifest",package_digest:"package"});
  api.confirmSkillInstall.mockResolvedValue({skill_id:"s1",version_id:"sv2",name:"travel",version:"1.1.0",title:"旅行计划",description:"生成旅行计划",content:"",package_digest:"package",manifest_digest:"manifest",requested_tools:["calculator","web_search"],granted_tools:["calculator"],connectors:[],phases:["planner"],grant_digest:"grant",status:"ENABLED"});
});

describe("control plane pages", () => {
  it("shows model versions, protocol, credential and verification state", async () => {
    render(<ModelsPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading", {name:"模型控制台"})).toBeTruthy();
    expect(screen.getAllByText("Anthropic").length).toBeGreaterThan(0);
    expect(screen.getByText("凭据已配置")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", {name:"验证连接"}));
    await waitFor(()=>expect(api.verifyModelVersion).toHaveBeenCalledWith("pv1","csrf"));
  });

  it("registers a model profile without collecting a secret", async () => {
    render(<ModelsPage csrfToken="csrf"/>);
    await screen.findByRole("heading", {name:"模型控制台"});
    fireEvent.change(screen.getByLabelText("档案名称"), {target:{value:"规划模型"}});
    fireEvent.change(screen.getByLabelText("供应商名称"), {target:{value:"本地网关"}});
    fireEvent.change(screen.getByLabelText("接口地址"), {target:{value:"https://api.example.com/v1"}});
    fireEvent.change(screen.getByLabelText("模型名称"), {target:{value:"planner-v1"}});
    fireEvent.change(screen.getByLabelText("凭据环境变量"), {target:{value:"PLANNER_API_KEY"}});
    fireEvent.click(screen.getByLabelText("工具调用"));
    fireEvent.click(screen.getByRole("button", {name:"注册模型档案"}));
    await waitFor(()=>expect(api.createModelProfile).toHaveBeenCalledWith(expect.objectContaining({capabilities:{text:true,streaming:true,tool_calling:true,json_object:false}}),"csrf"));
    expect(screen.queryByLabelText(/API Key/)).toBeNull();
  });

  it("creates a complete immutable role routing policy", async () => {
    render(<ModelsPage csrfToken="csrf"/>);
    await screen.findByRole("heading", {name:"模型控制台"});
    expect(document.querySelector(".route-policy-form")).toBeTruthy();
    expect(document.querySelectorAll(".route-field")).toHaveLength(10);
    fireEvent.change(screen.getByLabelText("策略名称"), {target:{value:"默认策略"}});
    for(const role of ["对话","询问","规划","执行","复盘","研究","专家协作","协调","质量评审","安全评审"]){
      fireEvent.change(screen.getByLabelText(`${role} 主模型`), {target:{value:"pv1"}});
    }
    fireEvent.click(screen.getByRole("button", {name:"保存路由策略"}));
    await waitFor(()=>expect(api.createRoutingPolicy).toHaveBeenCalledWith(expect.objectContaining({name:"默认策略",roles:expect.objectContaining({conversation:{primary:"pv1",fallback:[]},judge_safety:{primary:"pv1",fallback:[]}})}),"csrf"));
  });

  it("renders authoritative cost without recomputing unknown values", async () => {
    render(<UsagePage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading", {name:"用量与预算"})).toBeTruthy();
    expect(document.querySelectorAll(".budget-layer-grid .inline-control-form")).toHaveLength(3);
    expect(screen.getByRole("table").classList.contains("usage-table")).toBe(true);
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getAllByText("$0.002500").length).toBeGreaterThan(0);
    expect(screen.getByText("$0.001000")).toBeTruthy();
    expect(screen.getByText("规划")).toBeTruthy();
    expect(screen.getByText("1 次不可用")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("每日上限（microusd）"), {target:{value:"200000"}});
    fireEvent.click(screen.getAllByRole("button", {name:"保存预算"})[1]);
    await waitFor(()=>expect(api.setCostBudget).toHaveBeenCalledWith(200000,"csrf","DAILY",expect.any(String)));
  });

  it("starts a frozen release evaluation from the console", async () => {
    api.listModelProfiles.mockResolvedValue({profiles:[{id:"p",name:"模型",status:"ACTIVE",created_at:"",updated_at:"",versions:["a","b","q","s"].map((id,index)=>({id,profile_id:"p",profile_name:"模型",version:index+1,provider_protocol:"openai_compatible",provider_name:"供应商",base_url:"https://api.example.com/v1",model_name:id,credential_env_ref:`KEY_${id}`,credential_configured:true,capabilities:{text:true,json_object:true,streaming:true,tool_calling:true},context_window:32000,max_output_tokens:4096,timeout_seconds:30,max_attempts:2,config_digest:id,status:"ACTIVE",verified_at:"",verification_status:"VERIFIED",verification_error_kind:null,created_at:""}))}]});
    render(<EvaluationPage evaluationId={null} csrfToken="csrf"/>);
    await screen.findByRole("heading", {name:"真实配对评测"});
    for (const [label,value] of [["基线配置包","stable"],["候选配置包","candidate"]]) fireEvent.change(screen.getByLabelText(label), {target:{value}});
    fireEvent.change(screen.getByLabelText("基线模型"), {target:{value:"a"}});
    fireEvent.change(screen.getByLabelText("候选模型"), {target:{value:"b"}});
    fireEvent.change(screen.getByLabelText("质量评审模型"), {target:{value:"q"}});
    fireEvent.change(screen.getByLabelText("安全评审模型"), {target:{value:"s"}});
    fireEvent.click(screen.getByRole("button", {name:"启动真实评测"}));
    await waitFor(()=>expect(api.createEvaluationRun).toHaveBeenCalled());
  });

  it("shows evaluation progress and a labelled cancel action", async () => {
    render(<EvaluationPage evaluationId="e1" csrfToken="csrf"/>);
    expect(await screen.findByRole("heading", {name:"真实配对评测"})).toBeTruthy();
    expect(screen.getByText("1 / 60")).toBeTruthy();
    expect(screen.getByText("执行中")).toBeTruthy();
    expect(screen.getByText("计划")).toBeTruthy();
    expect(screen.queryByText("RUNNING")).toBeNull();
    fireEvent.click(screen.getByRole("button", {name:"取消评测"}));
    expect(screen.getByRole("dialog", {name:"取消评测？"})).toBeTruthy();
  });

  it("resumes a budget blocked evaluation from an inline form", async () => {
    api.getEvaluationRun.mockResolvedValueOnce({id:"e1",suite_id:"release-v1",baseline_bundle_id:"b",candidate_bundle_id:"c",status:"BUDGET_BLOCKED",budget_microusd:10000,attempts:1,created_at:"",updated_at:"",finished_at:null,cancel_requested_at:null});
    render(<EvaluationPage evaluationId="e1" csrfToken="csrf"/>);
    const input=await screen.findByLabelText(/新预算/);
    fireEvent.change(input,{target:{value:"20000"}});
    fireEvent.click(screen.getByRole("button",{name:"追加预算并恢复"}));
    await waitFor(()=>expect(api.resumeEvaluationRun).toHaveBeenCalledWith("e1",20000,"csrf"));
  });

  it("shows installed skills and expands version permissions", async () => {
    render(<SkillsPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading", {name:"Skill 平台"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button", {name:/旅行计划/}));
    expect(await screen.findByText("calculator")).toBeTruthy();
    expect(screen.getByText("已授权工具 1 / 1")).toBeTruthy();
  });

  it("previews requested permissions before installing a skill zip", async () => {
    render(<SkillsPage csrfToken="csrf"/>);
    await screen.findByRole("heading", {name:"Skill 平台"});
    const file=new File(["zip"],"travel.zip",{type:"application/zip"});
    fireEvent.change(screen.getByLabelText("选择 Skill ZIP"), {target:{files:[file]}});
    await waitFor(()=>expect(api.previewSkillInstall).toHaveBeenCalledWith(file,"csrf"));
    expect(await screen.findByText("请求的工具权限")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("calculator"));
    fireEvent.click(screen.getByRole("button", {name:"确认安装"}));
    await waitFor(()=>expect(api.confirmSkillInstall).toHaveBeenCalledWith("token",["calculator"],"csrf"));
  });
});
