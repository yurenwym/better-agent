import { useCallback, useEffect, useState } from "react";
import { ApiError, decideGoalAdjustment, getToday, mutateGoalAction, proposeGoalAdjustment, requestGoalActionHelp, syncGoalAdjustment, transitionGoalProgram } from "../api";
import DailyActionCard from "../components/DailyActionCard";
import AdjustmentProposalCard from "../components/AdjustmentProposalCard";
import DailyReviewCard from "../components/DailyReviewCard";
import type { GoalAction, GoalAdjustmentProposal, TodayResponse } from "../types";

interface Props { csrfToken: string; onHelp?: (threadId: string, actionId: string) => void; }

function key(prefix:string){ return `${prefix}-${crypto.randomUUID()}`; }
function message(error:unknown){ return error instanceof ApiError && error.status===409 ? "内容已在别处更新，已刷新最新状态。" : error instanceof Error ? error.message : "操作失败，请重试。"; }

export default function TodayPage({ csrfToken, onHelp }: Props) {
  const [data,setData]=useState<TodayResponse|null>(null); const [busy,setBusy]=useState<string|null>(null); const [error,setError]=useState("");
  const [selectedProgramId,setSelectedProgramId]=useState<string|null>(null);
  const [adjusting,setAdjusting]=useState<string|null>(null);const [reason,setReason]=useState("");const [proposal,setProposal]=useState<GoalAdjustmentProposal|null>(null);
  const load=useCallback(async()=>{try{const next=await getToday();setData(next);setSelectedProgramId(current=>next.programs.some(group=>group.program.id===current)?current:next.programs[0]?.program.id??null);}catch(reason){setError(message(reason));}},[]);
  useEffect(()=>{void load();},[load]);
  const reviewPending=Boolean(data?.programs.some(group=>group.review&&["QUEUED","RUNNING"].includes(group.review.status)));
  useEffect(()=>{if(!reviewPending)return;const timer=window.setInterval(()=>{void load();},1000);return()=>window.clearInterval(timer);},[reviewPending,load]);
  async function mutate(action:GoalAction,operation:"complete"|"skip"|"defer"|"feedback",payload:Record<string,unknown>={}){
    setBusy(action.id);setError("");try{await mutateGoalAction(action.id,operation,{expected_version:action.version,...payload},key(operation),csrfToken);}catch(reason){setError(message(reason));}finally{await load();setBusy(null);}
  }
  async function complete(action:GoalAction,feedback:{actual_minutes?:number;difficulty?:number}){setBusy(action.id);setError("");try{const result=await mutateGoalAction(action.id,"complete",{expected_version:action.version},key("complete"),csrfToken) as {action:GoalAction};if(Object.keys(feedback).length){try{await mutateGoalAction(action.id,"feedback",{expected_version:result.action.version,kind:"completion",...feedback},key("feedback"),csrfToken);}catch(reason){setError(`行动已完成，但反馈保存失败：${message(reason)}`);}}}catch(reason){setError(message(reason));}finally{await load();setBusy(null);}}
  async function pause(programId:string,version:number){setBusy(programId);setError("");try{await transitionGoalProgram(programId,"pause",version,key("pause"),csrfToken);}catch(reason){setError(message(reason));}finally{await load();setBusy(null);}}
  async function help(action:GoalAction){setBusy(action.id);setError("");try{const result=await requestGoalActionHelp(action.id,"请根据当前行动说明，帮我拆解下一步并回答我的问题。",action.version,key("help"),csrfToken);onHelp?.(result.thread_id,action.id);}catch(reason){setError(message(reason));}finally{setBusy(null);}}
  async function propose(programId:string,version:number){setBusy(programId);setError("");try{setProposal(await proposeGoalAdjustment(programId,reason,version,key("adjust"),csrfToken));}catch(value){setError(message(value));}finally{setBusy(null);}}
  async function decide(decision:"accept"|"reject",target=proposal){if(!target)return;setBusy(target.id);setError("");try{const result=await decideGoalAdjustment(target.id,decision,target.version,key(decision),csrfToken);setProposal("proposal" in result?result.proposal:result);await load();}catch(value){setError(message(value));await load();}finally{setBusy(null);}}
  async function sync(rebaseToCurrent=false,target=proposal){if(!target)return;setBusy(target.id);setError("");try{const result=await syncGoalAdjustment(target.id,target.version,key("sync"),csrfToken,rebaseToCurrent);setProposal(result.proposal);}catch(value){setError(message(value));setProposal({...target,plan_sync_status:"CONFLICT"});}finally{setBusy(null);}}
  if(!data)return <div className="today-page" aria-busy="true"><aside className="today-list"><p role="status">正在加载今天的行动…</p></aside><section className="today-detail" aria-label="行动详情"/></div>;
  const selected=data.programs.find(group=>group.program.id===selectedProgramId)??null;
  return <div className="today-page">
    <aside className="today-list" aria-label="今日目标">
      <header className="today-list-head"><div><span className="eyebrow">TODAY</span><h2>今天的行动</h2><p>{new Intl.DateTimeFormat("zh-CN",{dateStyle:"long"}).format(new Date())}</p></div><span className="today-count">{data.programs.length}</span></header>
      {error&&<p className="research-page-error" role="alert">{error}</p>}
      <div className="today-list-scroll">{data.programs.length===0?<div className="today-list-empty"><strong>今天没有待推进的行动</strong><p>暂停的执行项目和已结束行动不会出现在这里。</p></div>:data.programs.map(group=><button aria-pressed={selectedProgramId===group.program.id} className={`today-list-item${selectedProgramId===group.program.id?" active":""}`} key={group.program.id} type="button" onClick={()=>setSelectedProgramId(group.program.id)}><strong>{group.program.objective_title}</strong><span>第 {group.day_number} 天 · {group.today_estimated_minutes} 分钟</span><small>必做 {group.progress.required_completed}/{group.progress.required_total}</small></button>)}</div>
    </aside>
    <section className="today-detail" aria-label="行动详情">{selected?<section className="today-program" key={selected.program.id}>
      {(()=>{const group=selected;return <>
      <header className="today-program-header"><div><span className="eyebrow">DAY {group.day_number} · {group.program.timezone}</span><h3>{group.program.objective_title}</h3><p>{group.today_estimated_minutes} 分钟 · 必做 {group.progress.required_completed}/{group.progress.required_total}</p></div><div className="button-row"><button className="button button-quiet" disabled={busy!==null} type="button" onClick={()=>{setAdjusting(group.program.id);setProposal(null)}}>考虑调整</button><button className="button button-secondary" disabled={busy!==null} type="button" onClick={()=>void pause(group.program.id,group.program.version)}>暂停</button></div></header>
      <progress aria-label={`${group.program.objective_title} 完成进度`} max="1" value={group.progress.completion_rate}/>
      {group.overdue.length>0&&<div className="today-action-section"><h4>此前逾期</h4>{group.overdue.map(action=><DailyActionCard action={action} busy={busy===action.id} overdue key={action.id} onComplete={feedback=>void complete(action,feedback)} onSkip={()=>void mutate(action,"skip")} onDefer={scheduled_date=>void mutate(action,"defer",{scheduled_date})} onFeedback={difficulty=>void mutate(action,"feedback",{kind:"difficulty",difficulty})} onHelp={onHelp?()=>void help(action):undefined}/>)}</div>}
      <div className="today-action-section"><h4>今天</h4>{group.today.map(action=><DailyActionCard action={action} busy={busy===action.id} key={action.id} onComplete={feedback=>void complete(action,feedback)} onSkip={()=>void mutate(action,"skip")} onDefer={scheduled_date=>void mutate(action,"defer",{scheduled_date})} onFeedback={difficulty=>void mutate(action,"feedback",{kind:"difficulty",difficulty})} onHelp={onHelp?()=>void help(action):undefined}/>)}</div>
      {group.review&&<DailyReviewCard review={group.review}/>}
      {adjusting===group.program.id&&!proposal&&<form className="adjustment-form" onSubmit={event=>{event.preventDefault();void propose(group.program.id,group.program.version)}}><label>为什么要调整？<textarea required maxLength={2000} value={reason} onChange={event=>setReason(event.target.value)}/></label><button className="button button-primary" disabled={busy!==null||!reason.trim()} type="submit">生成调整预览</button></form>}
      {(proposal?.program_id===group.program.id||group.review?.proposal)&&(()=>{const target=proposal?.program_id===group.program.id?proposal:group.review!.proposal!;return <AdjustmentProposalCard proposal={target} busy={busy===target.id} onAccept={()=>void decide("accept",target)} onReject={()=>void decide("reject",target)} onSync={()=>void sync(false,target)} onRebaseSync={()=>void sync(true,target)}/>})()}
      </>})()}
    </section>:<div className="today-empty-detail"><span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M6 4h12v16H6z"/><path d="M9 9h6M9 13h6"/></svg></span><h3>选择一个目标</h3><p>从左侧打开目标，查看今天的行动、进度和每日复盘。</p></div>}
    </section>
  </div>;
}
