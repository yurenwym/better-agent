import { useCallback, useEffect, useState } from "react";
import { ApiError, decideGoalAdjustment, deleteGoalProgram, getToday, listGoalPrograms, mutateGoalAction, proposeGoalAdjustment, requestGoalActionHelp, syncGoalAdjustment, transitionGoalProgram } from "../api";
import DailyActionCard from "../components/DailyActionCard";
import AdjustmentProposalCard from "../components/AdjustmentProposalCard";
import DailyReviewCard from "../components/DailyReviewCard";
import ConfirmDialog from "../components/ConfirmDialog";
import AppToast from "../components/AppToast";
import type { GoalAction, GoalAdjustmentProposal, GoalProgram, TodayResponse } from "../types";

interface Props { csrfToken: string; onHelp?: (threadId: string, actionId: string) => void; }

function key(prefix:string){ return `${prefix}-${crypto.randomUUID()}`; }
function message(error:unknown){ return error instanceof ApiError && error.status===409 ? "内容已在别处更新，已刷新最新状态。" : error instanceof Error ? error.message : "操作失败，请重试。"; }

export default function TodayPage({ csrfToken, onHelp }: Props) {
  const [data,setData]=useState<TodayResponse|null>(null); const [busy,setBusy]=useState<string|null>(null); const [error,setError]=useState("");
  const [programs,setPrograms]=useState<GoalProgram[]>([]);
  const [selectedProgramId,setSelectedProgramId]=useState<string|null>(null);
  const [adjusting,setAdjusting]=useState<string|null>(null);const [reason,setReason]=useState("");const [proposal,setProposal]=useState<GoalAdjustmentProposal|null>(null);
  const [confirm,setConfirm]=useState<{program:GoalProgram;operation:"complete"|"cancel"|"delete"}|null>(null);
  const [notice,setNotice]=useState<{message:string;tone:"success"|"error"}|null>(null);
  const load=useCallback(async()=>{try{const [nextToday,nextPrograms]=await Promise.all([getToday(),listGoalPrograms()]);setData(nextToday);setPrograms(nextPrograms.programs);setSelectedProgramId(current=>nextPrograms.programs.some(program=>program.id===current)?current:nextPrograms.programs[0]?.id??null);}catch(reason){setError(message(reason));}},[]);
  useEffect(()=>{void load();},[load]);
  const reviewPending=Boolean(data?.programs.some(group=>group.review&&["QUEUED","RUNNING"].includes(group.review.status)));
  useEffect(()=>{if(!reviewPending)return;const timer=window.setInterval(()=>{void load();},1000);return()=>window.clearInterval(timer);},[reviewPending,load]);
  async function mutate(action:GoalAction,operation:"complete"|"skip"|"defer"|"feedback",payload:Record<string,unknown>={}){
    setBusy(action.id);setError("");try{await mutateGoalAction(action.id,operation,{expected_version:action.version,...payload},key(operation),csrfToken);}catch(reason){setError(message(reason));}finally{await load();setBusy(null);}
  }
  async function complete(action:GoalAction,feedback:{actual_minutes?:number;difficulty?:number}){setBusy(action.id);setError("");try{const result=await mutateGoalAction(action.id,"complete",{expected_version:action.version},key("complete"),csrfToken) as {action:GoalAction};if(Object.keys(feedback).length){try{await mutateGoalAction(action.id,"feedback",{expected_version:result.action.version,kind:"completion",...feedback},key("feedback"),csrfToken);}catch(reason){setError(`行动已完成，但反馈保存失败：${message(reason)}`);}}}catch(reason){setError(message(reason));}finally{await load();setBusy(null);}}
  async function pause(programId:string,version:number){setBusy(programId);setError("");try{await transitionGoalProgram(programId,"pause",version,key("pause"),csrfToken);}catch(reason){setError(message(reason));}finally{await load();setBusy(null);}}
  async function transition(program:GoalProgram,operation:"resume"|"complete"|"cancel"){setBusy(program.id);setError("");try{await transitionGoalProgram(program.id,operation,program.version,key(operation),csrfToken);setNotice({message:operation==="resume"?"目标已恢复":operation==="complete"?"目标已完成，执行总结已保存到记忆":"目标已取消",tone:"success"});setConfirm(null);}catch(reason){setNotice({message:message(reason),tone:"error"});}finally{await load();setBusy(null);}}
  async function remove(program:GoalProgram){setBusy(program.id);try{await deleteGoalProgram(program.id,program.version,key("delete"),csrfToken);setNotice({message:"执行记录已删除",tone:"success"});setConfirm(null);}catch(reason){setNotice({message:message(reason),tone:"error"});}finally{await load();setBusy(null);}}
  async function help(action:GoalAction){setBusy(action.id);setError("");try{const result=await requestGoalActionHelp(action.id,"请根据当前行动说明，帮我拆解下一步并回答我的问题。",action.version,key("help"),csrfToken);onHelp?.(result.thread_id,action.id);}catch(reason){setError(message(reason));}finally{setBusy(null);}}
  async function propose(programId:string,version:number){setBusy(programId);setError("");try{setProposal(await proposeGoalAdjustment(programId,reason,version,key("adjust"),csrfToken));}catch(value){setError(message(value));}finally{setBusy(null);}}
  async function decide(decision:"accept"|"reject",target=proposal){if(!target)return;setBusy(target.id);setError("");try{const result=await decideGoalAdjustment(target.id,decision,target.version,key(decision),csrfToken);setProposal("proposal" in result?result.proposal:result);await load();}catch(value){setError(message(value));await load();}finally{setBusy(null);}}
  async function sync(rebaseToCurrent=false,target=proposal){if(!target)return;setBusy(target.id);setError("");try{const result=await syncGoalAdjustment(target.id,target.version,key("sync"),csrfToken,rebaseToCurrent);setProposal(result.proposal);}catch(value){setError(message(value));setProposal({...target,plan_sync_status:"CONFLICT"});}finally{setBusy(null);}}
  if(!data)return <div className="today-page" aria-busy="true"><aside className="today-list"><p role="status">正在加载今天的行动…</p></aside><section className="today-detail" aria-label="行动详情"/></div>;
  const selectedProgram=programs.find(program=>program.id===selectedProgramId)??null;
  const selected=data.programs.find(group=>group.program.id===selectedProgramId)??null;
  const statusLabel:Record<string,string>={DRAFT:"准备中",ACTIVE:"执行中",PAUSED:"已暂停",COMPLETED:"已完成",CANCELLED:"已取消"};
  return <div className="today-page">
    <aside className="today-list" aria-label="执行项目">
      <header className="today-list-head"><div><span className="eyebrow">GOAL PROGRAMS</span><h2>执行项目</h2><p>{new Intl.DateTimeFormat("zh-CN",{dateStyle:"long"}).format(new Date())}</p></div><span className="today-count">{programs.length}</span></header>
      {error&&<p className="research-page-error" role="alert">{error}</p>}
      <div className="today-list-scroll">{programs.length===0?<div className="today-list-empty"><strong>还没有执行项目</strong><p>从计划页点击“开始执行”，确认后会显示在这里。</p></div>:programs.map(program=><button aria-pressed={selectedProgramId===program.id} className={`today-list-item${selectedProgramId===program.id?" active":""}`} key={program.id} type="button" onClick={()=>setSelectedProgramId(program.id)}><span className={`program-status status-${program.status.toLowerCase()}`}>{statusLabel[program.status]}</span><strong>{program.objective_title}</strong><span>{program.start_date} — {program.end_date}</span><small>必做 {program.progress.required_completed}/{program.progress.required_total}</small></button>)}</div>
    </aside>
    <section className="today-detail" aria-label="行动详情">{selectedProgram?<section className="today-program" key={selectedProgram.id}>
      <header className="today-program-header"><div><span className="eyebrow">{statusLabel[selectedProgram.status]} · {selectedProgram.timezone}</span><h3>{selectedProgram.objective_title}</h3><p>{selected?.today_estimated_minutes??0} 分钟 · 必做 {selectedProgram.progress.required_completed}/{selectedProgram.progress.required_total}</p></div><div className="button-row">{selectedProgram.status==="ACTIVE"&&<><button className="button button-quiet" disabled={busy!==null} type="button" onClick={()=>{setAdjusting(selectedProgram.id);setProposal(null)}}>考虑调整</button><button className="button button-secondary" disabled={busy!==null} type="button" onClick={()=>void pause(selectedProgram.id,selectedProgram.version)}>暂停</button>{selectedProgram.progress.completion_ready&&<button className="button button-primary" disabled={busy!==null} type="button" onClick={()=>setConfirm({program:selectedProgram,operation:"complete"})}>完成目标</button>}<button className="button button-quiet" disabled={busy!==null} type="button" onClick={()=>setConfirm({program:selectedProgram,operation:"cancel"})}>取消目标</button></>}{selectedProgram.status==="PAUSED"&&<><button className="button button-primary" disabled={busy!==null} type="button" onClick={()=>void transition(selectedProgram,"resume")}>恢复执行</button><button className="button button-quiet" disabled={busy!==null} type="button" onClick={()=>setConfirm({program:selectedProgram,operation:"cancel"})}>取消目标</button></>}{selectedProgram.status==="DRAFT"&&<button className="button button-quiet" disabled={busy!==null} type="button" onClick={()=>setConfirm({program:selectedProgram,operation:"cancel"})}>取消草稿</button>}{["COMPLETED","CANCELLED"].includes(selectedProgram.status)&&<button className="button button-danger" disabled={busy!==null} type="button" onClick={()=>setConfirm({program:selectedProgram,operation:"delete"})}>删除记录</button>}</div></header>
      <progress aria-label={`${selectedProgram.objective_title} 完成进度`} max="1" value={selectedProgram.progress.completion_rate}/>
      {selectedProgram.completion_summary&&<section className="goal-completion-summary"><span className="eyebrow">GOAL SUMMARY</span><h4>目标周期总结</h4><p>{selectedProgram.completion_summary}</p><small>已保存到长期记忆</small></section>}
      {selectedProgram.status==="PAUSED"&&<section className="goal-lifecycle-note"><strong>执行已暂停</strong><p>行动和历史记录都已保留，恢复后会重新出现在 Today。</p></section>}
      {selectedProgram.status==="CANCELLED"&&<section className="goal-lifecycle-note"><strong>目标已取消</strong><p>历史行动保留，仅从日常执行中移除。</p></section>}
      {selected&&(()=>{const group=selected;return <>
      {group.overdue.length>0&&<div className="today-action-section"><h4>此前逾期</h4>{group.overdue.map(action=><DailyActionCard action={action} busy={busy===action.id} overdue key={action.id} onComplete={feedback=>void complete(action,feedback)} onSkip={()=>void mutate(action,"skip")} onDefer={scheduled_date=>void mutate(action,"defer",{scheduled_date})} onFeedback={difficulty=>void mutate(action,"feedback",{kind:"difficulty",difficulty})} onHelp={onHelp?()=>void help(action):undefined}/>)}</div>}
      <div className="today-action-section"><h4>今天</h4>{group.today.map(action=><DailyActionCard action={action} busy={busy===action.id} key={action.id} onComplete={feedback=>void complete(action,feedback)} onSkip={()=>void mutate(action,"skip")} onDefer={scheduled_date=>void mutate(action,"defer",{scheduled_date})} onFeedback={difficulty=>void mutate(action,"feedback",{kind:"difficulty",difficulty})} onHelp={onHelp?()=>void help(action):undefined}/>)}</div>
      {group.review&&<DailyReviewCard review={group.review}/>}
      {adjusting===group.program.id&&!proposal&&<form className="adjustment-form" onSubmit={event=>{event.preventDefault();void propose(group.program.id,group.program.version)}}><label>为什么要调整？<textarea required maxLength={2000} value={reason} onChange={event=>setReason(event.target.value)}/></label><button className="button button-primary" disabled={busy!==null||!reason.trim()} type="submit">生成调整预览</button></form>}
      {(proposal?.program_id===group.program.id||group.review?.proposal)&&(()=>{const target=proposal?.program_id===group.program.id?proposal:group.review!.proposal!;return <AdjustmentProposalCard proposal={target} busy={busy===target.id} onAccept={()=>void decide("accept",target)} onReject={()=>void decide("reject",target)} onSync={()=>void sync(false,target)} onRebaseSync={()=>void sync(true,target)}/>})()}
      </>})()}
    </section>:<div className="today-empty-detail"><span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M6 4h12v16H6z"/><path d="M9 9h6M9 13h6"/></svg></span><h3>选择一个目标</h3><p>从左侧打开目标，查看今天的行动、进度和每日复盘。</p></div>}
    </section>
    <ConfirmDialog open={Boolean(confirm)} title={confirm?.operation==="complete"?"确认完成目标？":confirm?.operation==="cancel"?"取消这个目标？":"删除执行记录？"} description={confirm?.operation==="complete"?"确认后目标将进入已完成状态，并生成一条有限的执行总结保存到记忆。":confirm?.operation==="cancel"?"未来未执行行动将取消，已发生的行动历史会保留。":"删除后将从执行项目列表隐藏，计划文档不会被删除。"} confirmLabel={confirm?.operation==="complete"?"确认完成":confirm?.operation==="cancel"?"确认取消":"确认删除"} busy={busy!==null} onCancel={()=>setConfirm(null)} onConfirm={()=>{if(!confirm)return;if(confirm.operation==="delete")void remove(confirm.program);else void transition(confirm.program,confirm.operation)}}/>
    {notice&&<AppToast message={notice.message} tone={notice.tone} onDismiss={()=>setNotice(null)}/>}
  </div>;
}
