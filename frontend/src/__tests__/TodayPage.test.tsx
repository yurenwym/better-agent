import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TodayPage from "../pages/TodayPage";

const api=vi.hoisted(()=>({getToday:vi.fn(),mutateGoalAction:vi.fn(),transitionGoalProgram:vi.fn(),requestGoalActionHelp:vi.fn(),proposeGoalAdjustment:vi.fn(),decideGoalAdjustment:vi.fn(),syncGoalAdjustment:vi.fn()}));
vi.mock("../api",()=>({...api,ApiError:class ApiError extends Error{status=409;}}));
afterEach(cleanup);

const action={id:"action-1",program_id:"program-1",program_version_id:"pv-1",logical_key:"d1",scheduled_date:"2026-09-01",position:1,title:"完成一道题",description:"数组练习",estimated_minutes:60,completion_criteria:"提交通过",required:true,status:"SCHEDULED",version:0,completed_at:null,skipped_at:null,deferred_at:null,cancelled_at:null,deferred_from_action_id:null,cancel_reason:null};
const response={date:null,programs:[{program:{id:"program-1",objective_title:"一周力扣",objective_summary:"每天一道",status:"ACTIVE",timezone:"Asia/Shanghai",start_date:"2026-09-01",end_date:"2026-09-07",version:2},local_date:"2026-09-01",day_number:1,today:[action],overdue:[],today_estimated_minutes:60,progress:{required_completed:0,required_total:7,completion_rate:0,completion_ready:false,optional_completed:0},review:null}]};

describe("TodayPage",()=>{
  beforeEach(()=>{vi.clearAllMocks();api.getToday.mockResolvedValue(response);api.mutateGoalAction.mockResolvedValue({});api.transitionGoalProgram.mockResolvedValue({});api.requestGoalActionHelp.mockResolvedValue({thread_id:"thread-1",action_id:"action-1"});});
  it("renders grouped actions and refetches after completion",async()=>{
    api.mutateGoalAction.mockResolvedValueOnce({action:{...action,status:"COMPLETED",version:1}});
    render(<TodayPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading",{name:"一周力扣"})).toBeTruthy();
    expect(screen.getByText("完成标准")).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"完成"}));
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith("action-1","complete",expect.objectContaining({expected_version:0}),expect.any(String),"csrf"));
    await waitFor(()=>expect(api.getToday).toHaveBeenCalledTimes(2));
  });
  it("saves optional completion feedback after the completed action version",async()=>{
    api.mutateGoalAction.mockResolvedValueOnce({action:{...action,status:"COMPLETED",version:1}}).mockResolvedValueOnce({});
    render(<TodayPage csrfToken="csrf"/>);
    fireEvent.change(await screen.findByLabelText("完成一道题 实际分钟"),{target:{value:"90"}});
    fireEvent.change(screen.getByLabelText("完成一道题 完成难度"),{target:{value:"5"}});
    fireEvent.click(screen.getByRole("button",{name:"完成"}));
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenNthCalledWith(2,"action-1","feedback",expect.objectContaining({expected_version:1,actual_minutes:90,difficulty:5}),expect.any(String),"csrf"));
  });
  it("supports keyboard-visible native controls without horizontal-only interaction",async()=>{
    render(<TodayPage csrfToken="csrf"/>);
    const defer=await screen.findByLabelText("完成一道题 延期日期");
    fireEvent.change(defer,{target:{value:"2026-09-02"}});
    fireEvent.click(screen.getByRole("button",{name:"确认延期"}));
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith("action-1","defer",expect.objectContaining({scheduled_date:"2026-09-02"}),expect.any(String),"csrf"));
  });
  it("requires an explicit proposal and acceptance before changing future actions",async()=>{
    const proposal={id:"proposal-1",program_id:"program-1",base_program_version_id:"pv-1",expected_plan_document_version_id:"planv-1",expected_plan_content_hash:"sha256:x",candidate:{},diff:{added:[],removed:[],changed:[{logical_key:"d2",fields:{estimated_minutes:{before:60,after:30}}}]},reason:"太难",status:"PENDING",version:0,accepted_program_version_id:null,plan_sync_status:null,plan_sync_version_id:null,created_at:"",decided_at:null};
    api.proposeGoalAdjustment.mockResolvedValue(proposal);api.decideGoalAdjustment.mockResolvedValue({proposal:{...proposal,status:"ACCEPTED",version:1},program:{}});
    render(<TodayPage csrfToken="csrf"/>);
    fireEvent.click(await screen.findByRole("button",{name:"考虑调整"}));
    fireEvent.change(screen.getByRole("textbox",{name:"为什么要调整？"}),{target:{value:"太难"}});
    fireEvent.click(screen.getByRole("button",{name:"生成调整预览"}));
    expect(await screen.findByText("共 1 处变化：新增 0，移除 0，修改 1。")).toBeTruthy();
    expect(api.decideGoalAdjustment).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button",{name:"接受调整"}));
    await waitFor(()=>expect(api.decideGoalAdjustment).toHaveBeenCalled());
  });
  it("shows an automatic daily review and accepts its proposal on the first click",async()=>{
    const proposal={id:"proposal-auto",program_id:"program-1",base_program_version_id:"pv-1",expected_plan_document_version_id:"planv-1",expected_plan_content_hash:"sha256:x",candidate:{},diff:{added:[],removed:[],changed:[{logical_key:"d2",fields:{estimated_minutes:{before:60,after:30}}}]},reason:"今天太难",status:"PENDING",version:0,accepted_program_version_id:null,plan_sync_status:null,plan_sync_version_id:null,created_at:"",decided_at:null};
    api.getToday.mockResolvedValue({date:null,programs:[{...response.programs[0],today:[],today_estimated_minutes:0,review:{id:"review-1",program_id:"program-1",local_date:"2026-09-01",status:"COMPLETED",signals:["high_difficulty"],summary:"今天完成了复盘。",encouragement:"保持真实节奏。",needs_adjustment:true,adjustment_reason:"建议降低强度。",proposal,error_code:null}}]});
    api.decideGoalAdjustment.mockResolvedValue({proposal:{...proposal,status:"ACCEPTED",version:1},program:{}});
    render(<TodayPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading",{name:"今天的复盘"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"接受调整"}));
    await waitFor(()=>expect(api.decideGoalAdjustment).toHaveBeenCalledWith("proposal-auto","accept",0,expect.any(String),"csrf"));
  });
});
