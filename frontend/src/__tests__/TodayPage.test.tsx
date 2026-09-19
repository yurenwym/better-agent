import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TodayPage from "../pages/TodayPage";

const api=vi.hoisted(()=>({getToday:vi.fn(),getGoalAction:vi.fn(),listGoalPrograms:vi.fn(),listPlanDocuments:vi.fn(),deleteGoalProgram:vi.fn(),mutateGoalAction:vi.fn(),transitionGoalProgram:vi.fn(),requestGoalActionHelp:vi.fn(),proposeGoalAdjustment:vi.fn(),decideGoalAdjustment:vi.fn(),syncGoalAdjustment:vi.fn(),retryGoalReview:vi.fn(),closeGoalDay:vi.fn()}));
vi.mock("../api",()=>({...api,ApiError:class ApiError extends Error{status=409;}}));
afterEach(cleanup);

const action={id:"action-1",program_id:"program-1",program_version_id:"pv-1",logical_key:"d1",scheduled_date:"2026-09-01",position:1,title:"完成一道题",description:"数组练习",estimated_minutes:60,completion_criteria:"提交通过",required:true,status:"SCHEDULED",version:0,completed_at:null,skipped_at:null,deferred_at:null,cancelled_at:null,deferred_from_action_id:null,cancel_reason:null};
const response={date:null,programs:[{program:{id:"program-1",objective_title:"一周力扣",objective_summary:"每天一道",status:"ACTIVE",timezone:"Asia/Shanghai",start_date:"2026-09-01",end_date:"2026-09-07",version:2},local_date:"2026-09-01",day_number:1,today:[action],overdue:[],today_estimated_minutes:60,progress:{required_completed:0,required_total:7,completion_rate:0,completion_ready:false,optional_completed:0},review:null}]};
const program={...response.programs[0].program,compile_status:"READY",compile_error_code:null,daily_minutes:60,source_thread_id:"thread-1",source_plan_document_id:"plan-1",source_plan_document_version_id:"plan-version-1",source_plan_content_hash:"sha256:x",current_program_version_id:"pv-1",structure:null,actions:[action],progress:response.programs[0].progress,next_event_seq:4,deleted_at:null,completion_summary:null,completion_episode_id:null};
const pendingPlan={id:"plan-2",thread_id:"thread-2",title:"新手骑行减脂计划",version:1,file_status:"ready",created_at:"2026-09-05T07:41:19Z",updated_at:"2026-09-05T07:41:19Z"};

describe("TodayPage",()=>{
  it("renders a real embedded review view without action DOM or duplicate shell",async()=>{
    const review={id:"review-embed",program_id:program.id,local_date:"2026-09-01",status:"COMPLETED",signals:[],summary:"复盘的唯一正文",encouragement:null,needs_adjustment:false,adjustment_reason:null,proposal:null,error_code:null};
    api.getToday.mockResolvedValue({...response,programs:[{...response.programs[0],review}]});
    const {container}=render(<TodayPage csrfToken="csrf" programId={program.id} embedded contentView="review"/>);
    expect(await screen.findByText("复盘的唯一正文")).toBeTruthy();
    expect(container.querySelector(".daily-action-card,.daily-day-close,.program-day-plan,.today-focus-card,.today-page-toolbar,.today-list")).toBeNull();
    expect(screen.queryByRole("heading",{name:"一周力扣"})).toBeNull();
    expect(screen.queryByRole("button",{name:"完成"})).toBeNull();
    expect(screen.getByRole("button",{name:"暂停"})).toBeTruthy();
  });
  it("embedded actions link to review while retaining adjustment initiation",async()=>{
    const review={id:"review-embed",program_id:program.id,local_date:"2026-09-01",status:"COMPLETED",signals:[],summary:"不要在行动页展示的长复盘",encouragement:null,needs_adjustment:false,adjustment_reason:null,proposal:null,error_code:null};
    api.getToday.mockResolvedValue({...response,programs:[{...response.programs[0],review}]});
    const open=vi.fn();
    render(<TodayPage csrfToken="csrf" programId={program.id} embedded onOpenReview={open}/>);
    fireEvent.click(await screen.findByRole("button",{name:"查看复盘"}));
    expect(open).toHaveBeenCalledOnce();
    expect(screen.queryByText("不要在行动页展示的长复盘")).toBeNull();
    fireEvent.click(screen.getByRole("button",{name:"考虑调整"}));
    expect(screen.getByLabelText("为什么要调整？")).toBeTruthy();
  });
  it("groups action and day close under one plan heading",async()=>{
    const {container}=render(<TodayPage csrfToken="csrf" embedded/>);
    const group=await screen.findByRole("region",{name:"一周力扣"});
    expect(within(group).getByRole("button",{name:"完成"})).toBeTruthy();
    expect(within(group).getByRole("button",{name:"今天先到这里"})).toBeTruthy();
    expect(container.querySelectorAll(".today-plan-group")).toHaveLength(1);
    expect(screen.getAllByRole("heading",{name:"一周力扣"})).toHaveLength(1);
  });
  it("keeps progress separate from explicit day close",async()=>{
    api.getToday.mockResolvedValue({...response,programs:[{...response.programs[0],day_time:{spent_minutes:45,remaining_minutes:15,time_complete:true,unknown_action_ids:[],has_execution_record:true}}]});
    api.closeGoalDay.mockResolvedValue({});
    render(<TodayPage csrfToken="csrf"/>);
    fireEvent.click(await screen.findByRole("button",{name:"记录进度"}));
    fireEvent.change(screen.getByLabelText("待完成或待验收"),{target:{value:"读取CSV"}});
    fireEvent.change(screen.getByLabelText("完成一道题 实际分钟"),{target:{value:"45"}});
    fireEvent.click(screen.getByRole("button",{name:"保存部分进度"}));
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith("action-1","feedback",expect.objectContaining({kind:"partial",actual_date:"2026-09-01",actual_minutes:45}),expect.any(String),"csrf"));
    expect(api.closeGoalDay).not.toHaveBeenCalled();
    await waitFor(()=>expect((screen.getByRole("button",{name:"今天先到这里"}) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button",{name:"今天先到这里"}));
    await waitFor(()=>expect(api.closeGoalDay).toHaveBeenCalledWith("program-1","2026-09-01",expect.any(String),"csrf"));
    expect(screen.getByText("剩余 15 分钟")).toBeTruthy();
  });
  it("does not generate a review without execution evidence or invent remaining time",async()=>{
    api.getToday.mockResolvedValue({...response,programs:[{...response.programs[0],needs_review:true,day_time:{spent_minutes:0,remaining_minutes:null,time_complete:false,unknown_action_ids:[action.id],has_execution_record:false}}]});
    render(<TodayPage csrfToken="csrf"/>);
    expect((await screen.findByRole("button",{name:"今天先到这里"}) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("部分用时未记录")).toBeTruthy();
    expect(screen.queryByText("剩余 60 分钟")).toBeNull();
  });
  it("updates stale review using the existing retry operation",async()=>{
    const review={id:"stale-review",program_id:program.id,local_date:"2026-09-01",status:"COMPLETED",evidence_stale:true,signals:[],summary:"旧记录总结",encouragement:null,needs_adjustment:false,adjustment_reason:null,proposal:null,error_code:null};
    api.getToday.mockResolvedValue({...response,programs:[{...response.programs[0],review}]});
    render(<TodayPage csrfToken="csrf"/>);
    fireEvent.click(await screen.findByRole("button",{name:"更新复盘"}));
    await waitFor(()=>expect(api.retryGoalReview).toHaveBeenCalledWith("stale-review",expect.any(String),"csrf"));
    expect(api.closeGoalDay).not.toHaveBeenCalled();
  });
  it("retains completed actions and permits persisted correction and reopening",async()=>{
    const completed={...action,status:"COMPLETED",version:2,time_entry:{actual_date:"2026-09-01",actual_minutes:60}};
    api.getToday.mockResolvedValue({...response,programs:[{...response.programs[0],today:[],completed:[completed]}]});
    render(<TodayPage csrfToken="csrf"/>);
    fireEvent.click(await screen.findByText("一周力扣 · 已完成 1 项"));
    fireEvent.click(screen.getByRole("button",{name:"修改记录"}));
    fireEvent.change(screen.getByLabelText("完成一道题 实际分钟"),{target:{value:"45"}});
    fireEvent.click(screen.getByRole("button",{name:"保存更正"}));
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith(action.id,"feedback",expect.objectContaining({kind:"correction",actual_minutes:45,actual_date:"2026-09-01",expected_version:2}),expect.any(String),"csrf"));
    await waitFor(()=>expect((screen.getByRole("button",{name:"撤销完成"}) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button",{name:"撤销完成"}));
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith(action.id,"reopen",expect.objectContaining({expected_version:2}),expect.any(String),"csrf"));
  });
  beforeEach(()=>{vi.clearAllMocks();api.getToday.mockResolvedValue(response);api.getGoalAction.mockResolvedValue({action,program:response.programs[0].program});api.listGoalPrograms.mockResolvedValue({programs:[program]});api.listPlanDocuments.mockResolvedValue({plans:[]});api.deleteGoalProgram.mockResolvedValue(undefined);api.mutateGoalAction.mockResolvedValue({});api.transitionGoalProgram.mockResolvedValue({});api.requestGoalActionHelp.mockResolvedValue({thread_id:"thread-1",action_id:"action-1"});api.retryGoalReview.mockResolvedValue({});});
  it("renders grouped actions and refetches after completion",async()=>{
    api.mutateGoalAction.mockResolvedValueOnce({action:{...action,status:"COMPLETED",version:1}});
    render(<TodayPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading",{name:"一周力扣"})).toBeTruthy();
    expect(screen.getByRole("region",{name:"一周力扣"})).toBeTruthy();
    expect(screen.queryByRole("region",{name:"完整执行日程"})).toBeNull();
    expect(screen.getByText("完成标准")).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"完成"}));
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith("action-1","complete",expect.objectContaining({expected_version:0}),expect.any(String),"csrf"));
    await waitFor(()=>expect(api.getToday).toHaveBeenCalledTimes(2));
  });
  it("shows the full day-by-day plan and the next action",async()=>{
    const tomorrow={...action,id:"action-2",logical_key:"d2",scheduled_date:"2026-09-02",position:2,title:"复盘错题",estimated_minutes:30};
    api.listGoalPrograms.mockResolvedValue({programs:[{...program,actions:[action,tomorrow]}]});
    render(<TodayPage csrfToken="csrf" programId="program-1"/>);
    expect(await screen.findByRole("heading",{name:"每天要做什么"})).toBeTruthy();
    const dayPlan=screen.getByRole("region",{name:"完整执行日程"});
    expect(screen.getByText("第 1 天")).toBeTruthy();
    expect(screen.getByText("第 2 天")).toBeTruthy();
    // 每天展开时显示编译产出的具体内容，而不只是标题。
    expect(within(dayPlan).getAllByText("数组练习").length).toBeGreaterThan(0);
    expect(within(dayPlan).getAllByText("完成标准").length).toBeGreaterThan(0);
    expect(screen.getByText("下一步：完成一道题")).toBeTruthy();
  });
  it("completes a scheduled action from the full day-by-day plan",async()=>{
    const tomorrow={...action,id:"action-2",logical_key:"d2",scheduled_date:"2026-09-02",position:2,title:"复盘错题",estimated_minutes:30};
    api.listGoalPrograms.mockResolvedValue({programs:[{...program,actions:[action,tomorrow]}]});
    api.mutateGoalAction.mockResolvedValue({action:{...tomorrow,status:"COMPLETED",version:1}});
    render(<TodayPage csrfToken="csrf" programId="program-1"/>);
    fireEvent.click(await screen.findByText("第 2 天"));
    const checkbox=await screen.findByRole("checkbox",{name:"标记完成：复盘错题"}) as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
    fireEvent.click(checkbox);
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith("action-2","complete",expect.objectContaining({expected_version:0}),expect.any(String),"csrf"));
  });
  it("keeps terminal actions checked or disabled in the full schedule",async()=>{
    const completed={...action,status:"COMPLETED"};
    const skipped={...action,id:"action-2",logical_key:"d2",title:"复盘错题",status:"SKIPPED"};
    api.listGoalPrograms.mockResolvedValue({programs:[{...program,actions:[completed,skipped]}]});
    render(<TodayPage csrfToken="csrf" programId="program-1"/>);
    expect((await screen.findByRole("checkbox",{name:"撤销完成：完成一道题"}) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole("checkbox",{name:"已跳过：复盘错题"}) as HTMLInputElement).disabled).toBe(true);
  });
  it("shows the replacement action after an item is deferred",async()=>{
    const deferred={...action,status:"DEFERRED",version:1};
    const replacement={...action,id:"action-2",logical_key:"d1-defer-2",scheduled_date:"2026-09-02",position:2,title:"完成一道题",deferred_from_action_id:"action-1"};
    api.listGoalPrograms.mockResolvedValue({programs:[{...program,structure:{objective_title:"一周力扣",objective_summary:"每天一道",start_date:"2026-09-01",end_date:"2026-09-07",assumptions:[],milestones:[],actions:[action]},actions:[deferred,replacement]}]});
    render(<TodayPage csrfToken="csrf" programId="program-1"/>);
    fireEvent.click(await screen.findByText("第 2 天"));
    expect(await screen.findByRole("checkbox",{name:"标记完成：完成一道题"})).toBeTruthy();
    expect(screen.getByText("第 2 天")).toBeTruthy();
  });
  it("keeps an unchanged older-version action operable after another adjustment",async()=>{
    const cancelled={...action,id:"a-cancelled",status:"CANCELLED"};
    const retained={...action,id:"b-retained",program_version_id:"pv-2"};
    api.listGoalPrograms.mockResolvedValue({programs:[{...program,current_program_version_id:"pv-3",actions:[cancelled,retained]}]});
    render(<TodayPage csrfToken="csrf" programId="program-1"/>);
    const checkbox=await screen.findByRole("checkbox",{name:"标记完成：完成一道题"});
    fireEvent.click(checkbox);
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith("b-retained","complete",expect.any(Object),expect.any(String),"csrf"));
  });
  it("retains a failed note and its error after reloading server state",async()=>{
    api.mutateGoalAction.mockRejectedValue(new Error("反馈保存失败，请重试"));
    render(<TodayPage csrfToken="csrf"/>);
    fireEvent.click(await screen.findByLabelText("完成一道题 反馈与进度"));
    const note=await screen.findByLabelText("完成一道题 今天的感受");
    fireEvent.change(note,{target:{value:"安装阻塞"}});
    fireEvent.click(screen.getByRole("button",{name:"记录感受"}));
    await waitFor(()=>expect(screen.getByRole("alert").textContent).toContain("反馈保存失败"));
    expect((note as HTMLTextAreaElement).value).toBe("安装阻塞");
  });
  it("saves completion and optional feedback in one command",async()=>{
    api.mutateGoalAction.mockResolvedValueOnce({action:{...action,status:"COMPLETED",version:1}}).mockResolvedValueOnce({});
    render(<TodayPage csrfToken="csrf"/>);
    fireEvent.click(await screen.findByLabelText("完成一道题 反馈与进度"));
    fireEvent.change(await screen.findByLabelText("完成一道题 实际分钟"),{target:{value:"90"}});
    fireEvent.change(screen.getByLabelText("完成一道题 完成难度"),{target:{value:"5"}});
    fireEvent.click(screen.getByRole("button",{name:"完成"}));
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith("action-1","complete",expect.objectContaining({expected_version:0,actual_minutes:90,difficulty:5}),expect.any(String),"csrf"));
    expect(api.mutateGoalAction).toHaveBeenCalledTimes(1);
  });
  it("supports keyboard-visible native controls without horizontal-only interaction",async()=>{
    render(<TodayPage csrfToken="csrf"/>);
    fireEvent.click(await screen.findByLabelText("完成一道题 更多操作"));
    const defer=await screen.findByLabelText("完成一道题 延期日期");
    fireEvent.change(defer,{target:{value:"2026-09-02"}});
    fireEvent.click(screen.getByRole("button",{name:"确认延期"}));
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith("action-1","defer",expect.objectContaining({scheduled_date:"2026-09-02"}),expect.any(String),"csrf"));
  });
  it("requires an explicit proposal and acceptance before changing future actions",async()=>{
    const proposal={id:"proposal-1",program_id:"program-1",base_program_version_id:"pv-1",expected_plan_document_version_id:"planv-1",expected_plan_content_hash:"sha256:x",candidate:{},diff:{added:[],removed:[],changed:[{logical_key:"d2",fields:{estimated_minutes:{before:60,after:30}}}]},reason:"太难",status:"PENDING",version:0,accepted_program_version_id:null,plan_sync_status:null,plan_sync_version_id:null,created_at:"",decided_at:null};
    api.proposeGoalAdjustment.mockResolvedValue(proposal);api.decideGoalAdjustment.mockResolvedValue({proposal:{...proposal,status:"ACCEPTED",version:1},program:{}});
    render(<TodayPage csrfToken="csrf" programId="program-1"/>);
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
    fireEvent.click(await screen.findByLabelText("一周力扣 复盘与调整"));
    expect(await screen.findByRole("heading",{name:"今天的复盘"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"接受调整"}));
    await waitFor(()=>expect(api.decideGoalAdjustment).toHaveBeenCalledWith("proposal-auto","accept",0,expect.any(String),"csrf"));
  });
  it("offers a recovery action when daily review generation fails",async()=>{
    const failed={id:"review-failed",program_id:"program-1",local_date:"2026-09-01",status:"FAILED",signals:["high_difficulty"],summary:null,encouragement:null,needs_adjustment:null,adjustment_reason:null,proposal:null,error_code:"INVALID_MODEL_OUTPUT"};
    api.getToday.mockResolvedValue({date:null,programs:[{...response.programs[0],today:[],today_estimated_minutes:0,review:failed}]});
    render(<TodayPage csrfToken="csrf"/>);
    fireEvent.click(await screen.findByLabelText("一周力扣 复盘与调整"));
    fireEvent.click(await screen.findByRole("button",{name:"重新复盘"}));
    await waitFor(()=>expect(api.retryGoalReview).toHaveBeenCalledWith("review-failed",expect.any(String),"csrf"));
  });
  it("lists a paused program and lets the user resume it",async()=>{
    const paused={...program,status:"PAUSED",version:3};
    api.listGoalPrograms.mockResolvedValue({programs:[paused]});api.getToday.mockResolvedValue({date:null,programs:[]});
    render(<TodayPage csrfToken="csrf" programId="program-1"/>);
    fireEvent.click(await screen.findByRole("button",{name:"恢复执行"}));
    await waitFor(()=>expect(api.transitionGoalProgram).toHaveBeenCalledWith("program-1","resume",3,expect.any(String),"csrf"));
  });
  it("lists an unlinked saved plan as pending and opens its execution setup",async()=>{
    const onOpenPlan=vi.fn();
    api.listGoalPrograms.mockResolvedValue({programs:[]});api.getToday.mockResolvedValue({date:null,programs:[]});api.listPlanDocuments.mockResolvedValue({plans:[pendingPlan]});
    render(<TodayPage csrfToken="csrf" onOpenPlan={onOpenPlan}/>);
    expect(await screen.findByRole("heading",{name:"待决定"})).toBeTruthy();
    expect(await screen.findByRole("heading",{name:"新手骑行减脂计划"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"设置执行并启动"}));
    expect(onOpenPlan).toHaveBeenCalledWith("plan-2");
  });
  it("does not duplicate a plan that already has an execution program",async()=>{
    api.listPlanDocuments.mockResolvedValue({plans:[{...pendingPlan,id:"plan-1",title:"一周力扣"}]});
    render(<TodayPage csrfToken="csrf"/>);
    await screen.findByRole("heading",{name:"一周力扣"});
    expect(screen.queryByRole("heading",{name:"待启动计划"})).toBeNull();
  });
  it("explains that a draft must be activated without showing false progress",async()=>{
    const onOpenPlan=vi.fn();
    const draft={...program,status:"DRAFT",actions:[],progress:{...program.progress,required_total:0,completion_rate:1}};
    api.listGoalPrograms.mockResolvedValue({programs:[draft]});api.getToday.mockResolvedValue({date:null,programs:[]});
    render(<TodayPage csrfToken="csrf" programId="program-1" onOpenPlan={onOpenPlan}/>);
    expect(await screen.findByText("尚未激活")).toBeTruthy();
    expect(screen.getAllByText("尚未生成行动")).toHaveLength(2);
    expect(screen.queryByText("100%")).toBeNull();
    expect(screen.queryByText("必做 0/0")).toBeNull();
    fireEvent.click(screen.getByRole("button",{name:"返回计划并激活"}));
    expect(onOpenPlan).toHaveBeenCalledWith("plan-1");
  });
  it("collapses duplicate failed drafts and falls back to the saved plan title",async()=>{
    const failed={...program,objective_title:"",status:"DRAFT",compile_status:"FAILED",actions:[],progress:{...program.progress,required_total:0,completion_rate:1}};
    api.listGoalPrograms.mockResolvedValue({programs:[failed,{...failed,id:"program-older"}]});api.getToday.mockResolvedValue({date:null,programs:[]});api.listPlanDocuments.mockResolvedValue({plans:[{...pendingPlan,id:"plan-1"}]});
    render(<TodayPage csrfToken="csrf" programId="program-1"/>);
    expect(await screen.findAllByText("新手骑行减脂计划")).toHaveLength(2);
    expect(screen.getAllByRole("button",{name:/新手骑行减脂计划/})).toHaveLength(1);
    expect(screen.getAllByText("执行预览生成失败")).toHaveLength(2);
    expect(screen.queryByText("100%")).toBeNull();
    expect(screen.getByRole("button",{name:"返回计划并重试"})).toBeTruthy();
  });
  it("requires confirmation before completing a ready goal",async()=>{
    const ready={...program,version:4,progress:{...program.progress,required_completed:7,completion_rate:1,completion_ready:true}};
    api.listGoalPrograms.mockResolvedValue({programs:[ready]});api.getToday.mockResolvedValue({date:null,programs:[{...response.programs[0],program:ready,progress:ready.progress,today:[]}]});
    render(<TodayPage csrfToken="csrf" programId="program-1"/>);
    fireEvent.click(await screen.findByRole("button",{name:"完成目标"}));
    expect(api.transitionGoalProgram).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button",{name:"确认完成"}));
    await waitFor(()=>expect(api.transitionGoalProgram).toHaveBeenCalledWith("program-1","complete",4,expect.any(String),"csrf"));
  });
  it("deletes a terminal program after confirmation",async()=>{
    const completed={...program,status:"COMPLETED",version:5,completion_summary:"目标周期已完成。"};
    api.listGoalPrograms.mockResolvedValueOnce({programs:[completed]}).mockResolvedValue({programs:[]});api.getToday.mockResolvedValue({date:null,programs:[]});
    render(<TodayPage csrfToken="csrf" programId="program-1"/>);
    fireEvent.click(await screen.findByRole("button",{name:"删除记录"}));
    fireEvent.click(screen.getByRole("button",{name:"确认删除"}));
    await waitFor(()=>expect(api.deleteGoalProgram).toHaveBeenCalledWith("program-1",5,expect.any(String),"csrf"));
    expect(await screen.findByText("执行记录已删除")).toBeTruthy();
  });
  it("recovers from an initial today load failure",async()=>{
    api.getToday.mockRejectedValueOnce(new Error("今日服务暂时不可用")).mockResolvedValue(response);
    render(<TodayPage csrfToken="csrf"/>);
    expect(await screen.findByRole("alert")).toHaveProperty("textContent",expect.stringContaining("今日服务暂时不可用"));
    expect(screen.queryByText("正在加载今天的行动…")).toBeNull();
    fireEvent.click(screen.getByRole("button",{name:"重试加载"}));
    expect(await screen.findByRole("heading",{name:"完成一道题"})).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
  it("keeps today's actions usable when both supporting requests fail",async()=>{
    api.listGoalPrograms.mockRejectedValue(new Error("项目服务暂时不可用"));
    api.listPlanDocuments.mockRejectedValue(new Error("计划服务暂时不可用"));
    render(<TodayPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading",{name:"完成一道题"})).toBeTruthy();
    expect(await screen.findByRole("button",{name:"重试项目资料"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"完成"}));
    await waitFor(()=>expect(api.mutateGoalAction).toHaveBeenCalledWith("action-1","complete",expect.objectContaining({expected_version:0}),expect.any(String),"csrf"));
    api.listGoalPrograms.mockResolvedValue({programs:[program]});
    api.listPlanDocuments.mockResolvedValue({plans:[]});
    await waitFor(()=>expect((screen.getByRole("button",{name:"重试项目资料"}) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button",{name:"重试项目资料"}));
    await waitFor(()=>expect(screen.queryByText(/项目服务暂时不可用/)).toBeNull());
  });
  it("does not wait for slow project details before rendering the inbox",async()=>{
    api.listGoalPrograms.mockImplementation(()=>new Promise(()=>{}));
    api.listPlanDocuments.mockImplementation(()=>new Promise(()=>{}));
    render(<TodayPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading",{name:"完成一道题"})).toBeTruthy();
    expect(screen.queryByText("正在加载今天的行动…")).toBeNull();
  });
  it("aggregates multiple goals instead of selecting the first project",async()=>{
    const otherAction={...action,id:"action-2",program_id:"program-2",title:"阅读一章",estimated_minutes:20};
    const overdueAction={...action,id:"action-old",title:"补做昨天的题",scheduled_date:"2026-08-31"};
    const otherGroup={...response.programs[0],program:{...response.programs[0].program,id:"program-2",objective_title:"阅读目标"},today:[otherAction],today_estimated_minutes:20};
    api.getToday.mockResolvedValue({...response,programs:[{...response.programs[0],overdue:[overdueAction]},otherGroup]});
    render(<TodayPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading",{name:"完成一道题"})).toBeTruthy();
    expect(screen.getByRole("heading",{name:"阅读一章"})).toBeTruthy();
    expect(screen.getByRole("heading",{name:"补做昨天的题"})).toBeTruthy();
    expect(screen.getByText("预计 80 分钟")).toBeTruthy();
    expect(screen.queryByRole("complementary",{name:"执行项目"})).toBeNull();
    expect(screen.queryByRole("region",{name:"完整执行日程"})).toBeNull();
  });
  it("opens an explicitly selected project and focuses its action",async()=>{
    const targetAction={...action,id:"action-2",program_id:"program-2",title:"阅读一章"};
    const targetProgram={...program,id:"program-2",objective_title:"阅读目标",actions:[targetAction]};
    api.listGoalPrograms.mockResolvedValue({programs:[program,targetProgram]});
    api.getToday.mockResolvedValue({...response,programs:[...response.programs,{...response.programs[0],program:targetProgram,today:[targetAction]}]});
    const onSelectProgram=vi.fn();
    render(<TodayPage csrfToken="csrf" programId="program-2" actionId="action-2" onSelectProgram={onSelectProgram}/>);
    const detail=await screen.findByRole("region",{name:"行动详情"});
    expect(await within(detail).findByRole("heading",{name:"阅读目标"})).toBeTruthy();
    expect(within(detail).queryByRole("heading",{name:"一周力扣"})).toBeNull();
    await waitFor(()=>expect(document.activeElement?.id).toBe("action-action-2"));
    fireEvent.click(screen.getByRole("button",{name:"今日清单"}));
    expect(onSelectProgram).toHaveBeenCalledWith(null);
    expect(screen.getByRole("heading",{name:"完成一道题"})).toBeTruthy();
  });
  it("opens the schedule day for a future deep-linked action",async()=>{
    const future={...action,id:"action-future",scheduled_date:"2026-09-02",logical_key:"d2",title:"复盘错题"};
    api.listGoalPrograms.mockResolvedValue({programs:[{...program,actions:[action,future]}]});
    render(<TodayPage csrfToken="csrf" programId="program-1" actionId="action-future"/>);
    await waitFor(()=>expect(document.activeElement?.id).toBe("schedule-action-action-future"));
    expect((document.activeElement?.closest("details") as HTMLDetailsElement).open).toBe(true);
    expect(screen.getByRole("checkbox",{name:"标记完成：复盘错题"})).toBeTruthy();
  });
  it("keeps a missing program explicit without substituting the first project",async()=>{
    render(<TodayPage csrfToken="csrf" programId="missing-program"/>);
    expect(await screen.findByRole("heading",{name:"这个执行项目暂时不可用"})).toBeTruthy();
    const detail=screen.getByRole("region",{name:"行动详情"});
    expect(within(detail).queryByRole("heading",{name:"一周力扣"})).toBeNull();
  });
  it("keeps thread and action identifiers when asking for help from the inbox",async()=>{
    const onHelp=vi.fn();
    render(<TodayPage csrfToken="csrf" onHelp={onHelp}/>);
    fireEvent.click(await screen.findByRole("button",{name:"遇到困难"}));
    expect(api.requestGoalActionHelp).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("卡在哪里？"),{target:{value:"找不到CSV文件"}});
    fireEvent.click(screen.getByRole("button",{name:"帮我解决"}));
    await waitFor(()=>expect(api.requestGoalActionHelp).toHaveBeenCalledWith("action-1","找不到CSV文件",0,expect.any(String),"csrf"));
    await waitFor(()=>expect(onHelp).toHaveBeenCalledWith("thread-1","action-1"));
  });
  it("does not mistake a saved plan for an unstarted one while programs are unavailable",async()=>{
    api.listGoalPrograms.mockRejectedValue(new Error("项目列表暂时不可用"));
    api.listPlanDocuments.mockResolvedValue({plans:[pendingPlan]});
    render(<TodayPage csrfToken="csrf"/>);
    expect(await screen.findByRole("heading",{name:"完成一道题"})).toBeTruthy();
    expect(screen.queryByRole("button",{name:"设置执行并启动"})).toBeNull();
  });
  it("returns from project details to the project list when the URL is cleared",async()=>{
    const onSelectProgram=vi.fn();
    const {rerender}=render(<TodayPage csrfToken="csrf" programId="program-1" onSelectProgram={onSelectProgram}/>);
    expect(await screen.findByRole("heading",{name:"每天要做什么"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"返回项目列表"}));
    expect(onSelectProgram).toHaveBeenCalledWith(null);
    rerender(<TodayPage csrfToken="csrf" programId={null} onSelectProgram={onSelectProgram}/>);
    expect(screen.getByRole("button",{name:"执行项目"}).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("heading",{name:"选择一个执行项目"})).toBeTruthy();
    expect(screen.queryByRole("region",{name:"完整执行日程"})).toBeNull();
    expect(screen.getByRole("complementary",{name:"执行项目"})).toBeTruthy();
  });
  it("clears an old project selection when the execution projects view is clicked",async()=>{
    render(<TodayPage csrfToken="csrf"/>);
    fireEvent.click(await screen.findByRole("button",{name:"查看计划"}));
    expect(await screen.findByRole("heading",{name:"每天要做什么"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"执行项目"}));
    expect(screen.getByRole("heading",{name:"选择一个执行项目"})).toBeTruthy();
    expect(screen.queryByRole("region",{name:"完整执行日程"})).toBeNull();
  });
  it("resolves an action-only link to its project and focuses the action",async()=>{
    const future={...action,id:"action-future",scheduled_date:"2026-09-02",logical_key:"d2",title:"复盘错题"};
    api.getGoalAction.mockResolvedValue({action:future,program:response.programs[0].program});
    api.listGoalPrograms.mockResolvedValue({programs:[{...program,actions:[action,future]}]});
    render(<TodayPage csrfToken="csrf" actionId="action-future"/>);
    await waitFor(()=>expect(api.getGoalAction).toHaveBeenCalledWith("action-future"));
    await waitFor(()=>expect(document.activeElement?.id).toBe("schedule-action-action-future"));
    expect(screen.getByRole("button",{name:"执行项目"}).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("checkbox",{name:"标记完成：复盘错题"})).toBeTruthy();
  });
  it("offers retry and an inbox exit for an unavailable action-only link",async()=>{
    api.getGoalAction.mockRejectedValueOnce(new Error("行动不存在")).mockResolvedValue({action,program:response.programs[0].program});
    const onSelectProgram=vi.fn();
    render(<TodayPage csrfToken="csrf" actionId="action-1" onSelectProgram={onSelectProgram}/>);
    expect(await screen.findByRole("alert")).toHaveProperty("textContent",expect.stringContaining("无法打开关联行动"));
    expect(screen.getByRole("button",{name:"返回今日清单"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"重试定位行动"}));
    await waitFor(()=>expect(document.activeElement?.id).toBe("action-action-1"));
    await waitFor(()=>expect(screen.queryByRole("button",{name:"重试定位行动"})).toBeNull());
    expect(screen.getByRole("button",{name:"返回项目列表"})).toBeTruthy();
  });
  it("keeps pending review details collapsed while showing today's action",async()=>{
    const failed={id:"review-failed",program_id:"program-1",local_date:"2026-09-01",status:"FAILED",signals:[],summary:null,encouragement:null,needs_adjustment:null,adjustment_reason:null,proposal:null,error_code:"MODEL_UNAVAILABLE"};
    api.getToday.mockResolvedValue({...response,programs:[{...response.programs[0],review:failed}]});
    render(<TodayPage csrfToken="csrf"/>);
    const summary=await screen.findByLabelText("一周力扣 复盘与调整");
    expect((summary.closest("details") as HTMLDetailsElement).open).toBe(false);
    expect(summary.textContent).toContain("复盘待重试");
    expect(summary.textContent).toContain("2026-09-01");
    expect(screen.getByRole("heading",{name:"完成一道题"})).toBeTruthy();
    fireEvent.click(summary);
    fireEvent.click(screen.getByRole("button",{name:"重新复盘"}));
    await waitFor(()=>expect(api.retryGoalReview).toHaveBeenCalledWith("review-failed",expect.any(String),"csrf"));
  });
});
