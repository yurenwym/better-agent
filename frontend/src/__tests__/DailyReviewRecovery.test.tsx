import {cleanup,fireEvent,render,screen,waitFor} from "@testing-library/react";
import {afterEach,expect,it,vi} from "vitest";
import DailyReviewCard from "../components/DailyReviewCard";
import DailyActionCard from "../components/DailyActionCard";
import type {GoalDailyReview,GoalAction} from "../types";

afterEach(cleanup);
const review:GoalDailyReview={id:"r",program_id:"p",local_date:"2026-09-14",status:"COMPLETED",signals:["unfinished_actions"],summary:"安装已完成，代码验证待补做。",encouragement:"按剩余时间继续。",needs_adjustment:true,adjustment_reason:"先补验证。",proposal:null,error_code:null,adjustment_status:"FAILED",adjustment_error_code:"MODEL_UNAVAILABLE"};

it("keeps saved review visible and retries only the adjustment",()=>{
  const retry=vi.fn();render(<DailyReviewCard review={review} onRetry={retry}/>);
  expect(screen.getByText(review.summary!)).toBeTruthy();
  expect(screen.queryByText("复盘生成失败，不影响计划和完成进度。你可以直接重新复盘。")).toBeNull();
  fireEvent.click(screen.getByRole("button",{name:"重试调整建议"}));expect(retry).toHaveBeenCalledOnce();
});

it("treats no adjustment as a valid outcome",()=>{
  render(<DailyReviewCard review={{...review,adjustment_status:"NO_CHANGE"}}/>);
  expect(screen.getByText("当前无需调整后续安排。")).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull();
});

it("does not present inherited progress as new time spent",()=>{
  const action={id:"a",title:"验证pandas",required:true,estimated_minutes:30,completion_criteria:"打印版本",scheduled_date:"2026-09-15",progress:{state:"CARRIED_OVER",source_action_id:"yesterday",completed_work:"已安装",remaining_work:"运行代码"}} as GoalAction;
  render(<DailyActionCard action={action} busy={false} onComplete={()=>{}} onSkip={()=>{}} onDefer={()=>{}} onFeedback={()=>{}}/>);
  expect(screen.getByRole("status").textContent).toBe("接续上次进度 · 待完成：运行代码");
  expect(screen.queryByText(/部分完成.*分钟/)).toBeNull();
});

it("keeps optional feedback and secondary actions collapsed by default",()=>{
  const action={id:"a",title:"验证pandas",required:true,estimated_minutes:30,completion_criteria:"打印版本",scheduled_date:"2026-09-15"} as GoalAction;
  const feedback=vi.fn();
  render(<DailyActionCard action={action} busy={false} onComplete={()=>{}} onSkip={()=>{}} onDefer={()=>{}} onFeedback={feedback}/>);
  expect(screen.getByRole("button",{name:"完成"})).toBeTruthy();
  expect((screen.getByLabelText("验证pandas 更多操作").closest("details") as HTMLDetailsElement).open).toBe(false);
  expect((screen.getByLabelText("验证pandas 反馈与进度").closest("details") as HTMLDetailsElement).open).toBe(false);
  expect(screen.queryByRole("button",{name:"难度 1"})).toBeNull();
  fireEvent.click(screen.getByLabelText("验证pandas 反馈与进度"));
  fireEvent.change(screen.getByLabelText("验证pandas 完成难度"),{target:{value:"4"}});
  fireEvent.click(screen.getByRole("button",{name:"记录难度"}));
  expect(feedback).toHaveBeenCalledWith(4);
});

it("keeps failed partial progress and prevents submitting invalid minutes",async()=>{
  const action={id:"a",title:"验证pandas",required:true,estimated_minutes:30,completion_criteria:"打印版本",scheduled_date:"2026-09-15"} as GoalAction;
  const progress=vi.fn().mockResolvedValue(false);
  render(<DailyActionCard action={action} busy={false} onComplete={()=>{}} onSkip={()=>{}} onDefer={()=>{}} onFeedback={()=>{}} onProgress={progress}/>);
  fireEvent.click(screen.getByRole("button",{name:"记录进度"}));
  fireEvent.change(screen.getByLabelText("待完成或待验收"),{target:{value:"运行验证"}});
  fireEvent.change(screen.getByLabelText("验证pandas 实际分钟"),{target:{value:"-2"}});
  expect((screen.getByRole("button",{name:"完成"}) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button",{name:"保存部分进度"}) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole("alert").textContent).toContain("0 到 1440");
  fireEvent.change(screen.getByLabelText("验证pandas 实际分钟"),{target:{value:"15"}});
  fireEvent.click(screen.getByRole("button",{name:"保存部分进度"}));
  await waitFor(()=>expect(progress).toHaveBeenCalledWith(expect.objectContaining({kind:"partial",actual_minutes:15,remaining_work:"运行验证"})));
  expect((screen.getByLabelText("待完成或待验收") as HTMLTextAreaElement).value).toBe("运行验证");
});

it("identifies the review date and does not retry an already queued stale review",()=>{
  render(<DailyReviewCard review={{...review,status:"QUEUED",evidence_stale:true}} onRetry={vi.fn()}/>);
  expect(screen.getByText("2026-09-14").tagName).toBe("TIME");
  expect(screen.queryByRole("button",{name:"重新复盘"})).toBeNull();
});

it("clears mistaken time only through an explicit correction choice",async()=>{
  const action={id:"a",title:"验证pandas",status:"COMPLETED",required:true,estimated_minutes:30,completion_criteria:"打印版本",scheduled_date:"2026-09-15",time_entry:{actual_date:"2026-09-15",actual_minutes:45}} as GoalAction;
  const correction=vi.fn().mockResolvedValue(true);
  render(<DailyActionCard action={action} busy={false} onComplete={()=>{}} onSkip={()=>{}} onDefer={()=>{}} onFeedback={()=>{}} onCorrection={correction}/>);
  fireEvent.click(screen.getByRole("button",{name:"修改记录"}));
  fireEvent.click(screen.getByLabelText("清除该日用时"));
  fireEvent.click(screen.getByRole("button",{name:"保存更正"}));
  await waitFor(()=>expect(correction).toHaveBeenCalledWith(expect.objectContaining({kind:"correction",actual_date:"2026-09-15",cleared_fields:["actual_minutes"]})));
  expect(correction.mock.calls[0][0]).not.toHaveProperty("actual_minutes");
});

it("keeps a readable source reference without exposing raw citation markup",()=>{
  render(<DailyReviewCard review={{...review,summary:"已经完成安装 [[source:feedback_123]]"}}/>);
  expect(screen.getByText("依据：执行记录")).toBeTruthy();
  expect(screen.queryByText(/\[\[source:/)).toBeNull();
  fireEvent.click(screen.getByText("依据：执行记录"));
  expect(screen.getByText("feedback_123")).toBeTruthy();
});

it("does not attach a historical correction draft to today's completion",()=>{
  const action={id:"a",title:"验证pandas",status:"SCHEDULED",required:true,estimated_minutes:30,completion_criteria:"打印版本",scheduled_date:"2026-09-14"} as GoalAction;
  const complete=vi.fn();
  render(<DailyActionCard action={action} localDate="2026-09-15" busy={false} onComplete={complete} onSkip={()=>{}} onDefer={()=>{}} onFeedback={()=>{}} onCorrection={async()=>true}/>);
  fireEvent.click(screen.getByLabelText("验证pandas 更多操作"));
  fireEvent.click(screen.getByRole("button",{name:"修改记录"}));
  fireEvent.change(screen.getByLabelText("记录日期"),{target:{value:"2026-09-14"}});
  fireEvent.change(screen.getByLabelText("验证pandas 实际分钟"),{target:{value:"45"}});
  fireEvent.click(screen.getByRole("button",{name:"完成"}));
  expect(complete).toHaveBeenCalledWith({actual_date:"2026-09-15"});
});
