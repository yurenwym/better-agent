import { useCallback, useEffect, useRef, useState } from "react";
import { closeGoalDay } from "../api";
import "../daily-execution.css";
import { decideGoalAdjustment, deleteGoalProgram, getGoalAction, getToday, listGoalPrograms, listPlanDocuments, mutateGoalAction, proposeGoalAdjustment, requestGoalActionHelp, retryGoalReview, syncGoalAdjustment, transitionGoalProgram } from "../api";
import DailyActionCard from "../components/DailyActionCard";
import AdjustmentProposalCard from "../components/AdjustmentProposalCard";
import DailyReviewCard from "../components/DailyReviewCard";
import ConfirmDialog from "../components/ConfirmDialog";
import AppToast from "../components/AppToast";
import { navigateTo } from "../navigation";
import type { GoalAction, GoalAdjustmentProposal, GoalProgram, PlanDocumentSummary, TodayProgramGroup, TodayResponse } from "../types";

interface Props {
  csrfToken: string;
  programId?: string | null;
  actionId?: string | null;
  onSelectProgram?: (programId: string | null) => void;
  onHelp?: (threadId: string, actionId: string) => void;
  onOpenPlan?: (planId: string) => void;
  embedded?: boolean;
  contentView?: "actions" | "review";
  onOpenReview?: () => void;
}

function key(prefix:string){ return `${prefix}-${crypto.randomUUID()}`; }
function message(error:unknown){ return error instanceof Error ? error.message : "操作失败，请重试。"; }
function compactDate(value:string){return new Intl.DateTimeFormat("zh-CN",{month:"numeric",day:"numeric",weekday:"short"}).format(new Date(`${value}T00:00:00`));}
function planDays(program:GoalProgram){
  const currentActions=program.actions.filter(action=>action.status!=="CANCELLED");
  const actionByKey=new Map<string,GoalAction>();
  for(const action of program.actions){
    const current=actionByKey.get(action.logical_key);
    if(!current||(current.status==="CANCELLED"&&action.status!=="CANCELLED")||action.program_version_id===program.current_program_version_id)actionByKey.set(action.logical_key,action);
  }
  const source=program.structure?.actions
    ? [...program.structure.actions,...currentActions.filter(action=>action.deferred_from_action_id&&!program.structure!.actions.some(item=>item.logical_key===action.logical_key))]
    : currentActions;
  const dates=[...new Set(source.map(action=>action.scheduled_date))].sort();
  return dates.map(scheduledDate=>({
    scheduledDate,dayNumber:Math.round((Date.parse(`${scheduledDate}T00:00:00Z`)-Date.parse(`${program.start_date}T00:00:00Z`))/86400000)+1,
    actions:source.filter(action=>action.scheduled_date===scheduledDate).sort((left,right)=>left.position-right.position).map(action=>{
      const liveAction=actionByKey.get(action.logical_key);
      return {...action,status:liveAction?.status??"SCHEDULED",liveAction};
    }),
  }));
}
const actionStatusLabel:Record<string,string>={SCHEDULED:"待完成",COMPLETED:"已完成",SKIPPED:"已跳过",DEFERRED:"已延期",CANCELLED:"已取消"};
// 编译产出的 description 用「。」分句（热身/主要训练/放松…），按句拆成要点更接近计划文档里的表格。
function actionDetailLines(description:string):string[]{return String(description??"").split("。").map(line=>line.trim()).filter(Boolean);}
function ProgramDayPlan({program,localDate,busyId,onComplete,onReopen}:{program:GoalProgram;localDate?:string;busyId:string|null;onComplete:(action:GoalAction)=>void;onReopen:(action:GoalAction)=>void}){
  const days=planDays(program);
  if(!days.length)return null;
  return <section className="program-day-plan" aria-label="完整执行日程">
    <header><div><span className="eyebrow">完整日程</span><h4>每天要做什么</h4></div><strong>{days.length} 天</strong></header>
    <div className="program-day-list">{days.map(day=><details key={day.scheduledDate} open={day.scheduledDate===localDate}>
      <summary><span><strong>第 {day.dayNumber} 天</strong><small>{compactDate(day.scheduledDate)}</small></span><span>{day.actions.length} 项 · {day.actions.reduce((total,action)=>total+action.estimated_minutes,0)} 分钟</span></summary>
      <ol>{day.actions.map(action=>{
        const label=actionStatusLabel[action.status]??"状态已更新";
        const canComplete=program.status==="ACTIVE"&&action.status==="SCHEDULED"&&Boolean(action.liveAction);
        const canReopen=program.status==="ACTIVE"&&action.status==="COMPLETED"&&Boolean(action.liveAction);
        return <li id={action.liveAction?`schedule-action-${action.liveAction.id}`:undefined} tabIndex={-1} aria-busy={busyId===action.liveAction?.id} className={`plan-action-${String(action.status).toLowerCase()}`} key={action.logical_key}>
          <label className="program-action-toggle"><input aria-label={canComplete?`标记完成：${action.title}`:canReopen?`撤销完成：${action.title}`:`${label}：${action.title}`} checked={action.status==="COMPLETED"} className="program-action-check" disabled={(!canComplete&&!canReopen)||busyId!==null} type="checkbox" onChange={()=>{if(action.liveAction){if(canComplete)onComplete(action.liveAction);else if(canReopen)onReopen(action.liveAction)}}}/></label>
          <div><strong>{action.title}</strong><small>{action.required?"必做":"可选"} · {action.estimated_minutes} 分钟</small>
            {actionDetailLines(action.description).length>0&&<ul className="program-action-detail">{actionDetailLines(action.description).map((line,index)=><li key={index}>{line}</li>)}</ul>}
            {action.completion_criteria&&<p className="program-action-criteria"><strong>完成标准</strong>{action.completion_criteria}</p>}
          </div><em>{busyId===action.liveAction?.id?"更新中…":label}</em>
        </li>;
      })}</ol>
    </details>)}</div>
  </section>;
}

export default function TodayPage({ csrfToken, programId = null, actionId = null, onSelectProgram, onHelp, onOpenPlan, embedded = false, contentView = "actions", onOpenReview }: Props) {
  const [data,setData]=useState<TodayResponse|null>(null); const [busy,setBusy]=useState<string|null>(null); const [error,setError]=useState("");
  const [loading,setLoading]=useState(true);
  const [loadError,setLoadError]=useState("");
  const [detailErrors,setDetailErrors]=useState({programs:"",plans:""});
  const [detailsLoading,setDetailsLoading]=useState(true);
  const [programsLoaded,setProgramsLoaded]=useState(false);
  const [programs,setPrograms]=useState<GoalProgram[]>([]);
  const [plans,setPlans]=useState<PlanDocumentSummary[]>([]);
  const [view,setView]=useState<"inbox"|"projects">(programId?"projects":"inbox");
  const [selectedItem,setSelectedItem]=useState<string|null>(programId?`program:${programId}`:null);
  const loadRequest=useRef(0);
  const detailRequest=useRef(0);
  const actionRequest=useRef(0);
  const returningToProjects=useRef(false);
  const focusedAction=useRef<HTMLElement|null>(null);
  const [actionLinkLoading,setActionLinkLoading]=useState(false);
  const [actionLinkError,setActionLinkError]=useState("");
  const [actionLinkRetry,setActionLinkRetry]=useState(0);
  const [adjusting,setAdjusting]=useState<string|null>(null);const [reason,setReason]=useState("");const [proposal,setProposal]=useState<GoalAdjustmentProposal|null>(null);
  const [confirm,setConfirm]=useState<{program:GoalProgram;operation:"complete"|"cancel"|"delete"}|null>(null);
  const [notice,setNotice]=useState<{message:string;tone:"success"|"error"}|null>(null);
  const loadDetails=useCallback(async()=>{
    const request=++detailRequest.current;
    setDetailsLoading(true);
    await Promise.all([
      listGoalPrograms().then(result=>{
        if(request!==detailRequest.current)return;
        const seenDraftPlans=new Set<string>();
        setPrograms(result.programs.filter(program=>{
          if(program.status!=="DRAFT")return true;
          if(seenDraftPlans.has(program.source_plan_document_id))return false;
          seenDraftPlans.add(program.source_plan_document_id);return true;
        }));
        setProgramsLoaded(true);
        setDetailErrors(current=>({...current,programs:""}));
      }).catch(reason=>{if(request===detailRequest.current)setDetailErrors(current=>({...current,programs:message(reason)}));}),
      listPlanDocuments().then(result=>{
        if(request!==detailRequest.current)return;
        setPlans(result.plans);
        setDetailErrors(current=>({...current,plans:""}));
      }).catch(reason=>{if(request===detailRequest.current)setDetailErrors(current=>({...current,plans:message(reason)}));}),
    ]);
    if(request===detailRequest.current)setDetailsLoading(false);
  },[]);
  const load=useCallback(async()=>{
    const request=++loadRequest.current;
    setLoading(true);
    void loadDetails();
    try{
      const nextToday=await getToday();
      if(request===loadRequest.current){setData(nextToday);setLoadError("");}
    }catch(reason){if(request===loadRequest.current)setLoadError(message(reason));}
    finally{if(request===loadRequest.current)setLoading(false);}
  },[loadDetails]);
  useEffect(()=>{void load();return()=>{loadRequest.current++;detailRequest.current++;};},[load]);
  useEffect(()=>{
    setSelectedItem(programId?`program:${programId}`:null);
    setView(programId||(!actionId&&returningToProjects.current)?"projects":"inbox");
    returningToProjects.current=false;
    focusedAction.current=null;
  },[programId,actionId]);
  useEffect(()=>{
    const request=++actionRequest.current;
    setActionLinkError("");
    setActionLinkLoading(Boolean(actionId&&!programId));
    if(!actionId||programId)return;
    void getGoalAction(actionId).then(result=>{
      if(request!==actionRequest.current)return;
      setSelectedItem(`program:${result.program.id}`);
      setView("projects");
    }).catch(reason=>{if(request===actionRequest.current)setActionLinkError(message(reason));})
      .finally(()=>{if(request===actionRequest.current)setActionLinkLoading(false);});
    return()=>{actionRequest.current++;};
  },[actionId,programId,actionLinkRetry]);
  useEffect(()=>{
    if(!actionId)return;
    const element=document.getElementById(`action-${actionId}`)??document.getElementById(`schedule-action-${actionId}`);
    if(!element||focusedAction.current===element)return;
    for(let parent=element.parentElement;parent;parent=parent.parentElement)if(parent instanceof HTMLDetailsElement)parent.open=true;
    element.focus({preventScroll:true});
    element.scrollIntoView?.({block:"center",behavior:"instant"});
    focusedAction.current=element;
  },[actionId,data,programs,view,selectedItem]);
  function selectProgram(id:string|null){
    actionRequest.current++;
    setActionLinkLoading(false);
    setActionLinkError("");
    setSelectedItem(id?`program:${id}`:null);
    setView(id?"projects":"inbox");
    onSelectProgram?.(id);
  }
  function openProjectList(){
    actionRequest.current++;
    setActionLinkLoading(false);
    setActionLinkError("");
    setSelectedItem(null);
    setView("projects");
    if(programId||actionId){
      returningToProjects.current=true;
      onSelectProgram?.(null);
    }
  }
  const reviewPending=Boolean(data?.programs.some(group=>group.review&&(["QUEUED","RUNNING"].includes(group.review.status)||["PENDING","RUNNING"].includes(group.review.adjustment_status??""))));
  useEffect(()=>{if(!reviewPending)return;const timer=window.setInterval(()=>{void load();},1000);return()=>window.clearInterval(timer);},[reviewPending,load]);
  function actionDate(action:GoalAction){return data?.programs.find(group=>group.program.id===action.program_id)?.local_date??new Intl.DateTimeFormat("en-CA",{timeZone:programs.find(program=>program.id===action.program_id)?.timezone??"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());}
  async function mutate(action:GoalAction,operation:"complete"|"skip"|"defer"|"feedback"|"reopen",payload:Record<string,unknown>={}){
    setBusy(action.id);setError("");try{await mutateGoalAction(action.id,operation,{expected_version:action.version,...payload},key(operation),csrfToken);return true;}catch(reason){setError(message(reason));return false;}finally{await load();setBusy(null);}
  }
  async function complete(action:GoalAction,feedback:{actual_minutes?:number;difficulty?:number;note?:string;actual_date?:string}){return mutate(action,"complete",{actual_date:actionDate(action),...feedback});}
  async function pause(programId:string,version:number){setBusy(programId);setError("");try{await transitionGoalProgram(programId,"pause",version,key("pause"),csrfToken);}catch(reason){setError(message(reason));}finally{await load();setBusy(null);}}
  async function transition(program:GoalProgram,operation:"resume"|"complete"|"cancel"){setBusy(program.id);setError("");try{await transitionGoalProgram(program.id,operation,program.version,key(operation),csrfToken);setNotice({message:operation==="resume"?"目标已恢复":operation==="complete"?"目标已完成，执行总结已保存到记忆":"目标已取消",tone:"success"});setConfirm(null);}catch(reason){setNotice({message:message(reason),tone:"error"});}finally{await load();setBusy(null);}}
  async function remove(program:GoalProgram){setBusy(program.id);try{await deleteGoalProgram(program.id,program.version,key("delete"),csrfToken);setNotice({message:"执行记录已删除",tone:"success"});setConfirm(null);}catch(reason){setNotice({message:message(reason),tone:"error"});}finally{await load();setBusy(null);}}
  async function help(action:GoalAction,content:string){setBusy(action.id);setError("");try{const result=await requestGoalActionHelp(action.id,content,action.version,key("help"),csrfToken);onHelp?.(result.thread_id,action.id);return true;}catch(reason){setError(message(reason));return false;}finally{setBusy(null);}}
  async function closeDay(group:TodayProgramGroup){setBusy(`day:${group.program.id}`);setError("");try{if(group.review&&(group.review.evidence_stale||group.review.status==="FAILED"))await retryGoalReview(group.review.id,key("review-retry"),csrfToken);else await closeGoalDay(group.program.id,group.local_date,key("close-day"),csrfToken);}catch(reason){setError(message(reason));}finally{await load();setBusy(null);}}
  async function propose(programId:string,version:number){setBusy(programId);setError("");try{setProposal(await proposeGoalAdjustment(programId,reason,version,key("adjust"),csrfToken));}catch(value){setError(message(value));}finally{setBusy(null);}}
  async function decide(decision:"accept"|"reject",target=proposal){if(!target)return;setBusy(target.id);setError("");try{const result=await decideGoalAdjustment(target.id,decision,target.version,key(decision),csrfToken);setProposal("proposal" in result?result.proposal:result);await load();}catch(value){setError(message(value));await load();}finally{setBusy(null);}}
  async function sync(rebaseToCurrent=false,target=proposal){if(!target)return;setBusy(target.id);setError("");try{const result=await syncGoalAdjustment(target.id,target.version,key("sync"),csrfToken,rebaseToCurrent);setProposal(result.proposal);}catch(value){setError(message(value));setProposal({...target,plan_sync_status:"CONFLICT"});}finally{setBusy(null);}}
  async function retryReview(reviewId:string){setBusy(reviewId);setError("");try{await retryGoalReview(reviewId,key("review-retry"),csrfToken);}catch(value){setError(message(value));}finally{await load();setBusy(null);}}
  function renderAction(action:GoalAction,overdue=false){return <DailyActionCard action={action} localDate={actionDate(action)} busy={busy!==null} overdue={overdue} key={action.id} onComplete={feedback=>void complete(action,feedback)} onSkip={()=>void mutate(action,"skip")} onDefer={scheduled_date=>void mutate(action,"defer",{scheduled_date})} onFeedback={difficulty=>void mutate(action,"feedback",{kind:"difficulty",difficulty,actual_date:actionDate(action)})} onProgress={payload=>mutate(action,"feedback",payload)} onNote={note=>mutate(action,"feedback",{kind:"note",note,actual_date:actionDate(action)})} onHelp={onHelp?content=>help(action,content):undefined} onReopen={()=>void mutate(action,"reopen")} onCorrection={payload=>mutate(action,"feedback",payload)}/>;}
  function openReview(group:TodayProgramGroup){
    if(onOpenReview&&group.program.id===programId){onOpenReview();return;}
    const plan=programs.find(item=>item.id===group.program.id)?.source_plan_document_id;
    if(embedded&&plan){navigateTo(`/workspace/${encodeURIComponent(plan)}?tab=review&from=today`);return;}
    const target=document.getElementById(`daily-review-${group.review?.id}`);
    for(let parent=target?.parentElement;parent;parent=parent.parentElement)if(parent instanceof HTMLDetailsElement)parent.open=true;
    target?.scrollIntoView?.({block:"center"});target?.focus();
  }
  function renderReviewLink(group:TodayProgramGroup){
    const review=group.review;
    if(!review)return null;
    const status=review.evidence_stale?"复盘待更新":review.status==="FAILED"?"复盘失败":review.adjustment_status==="FAILED"?"调整建议待重试":review.proposal?.status==="PENDING"?"调整待确认":["QUEUED","RUNNING"].includes(review.status)?"正在复盘":"复盘已就绪";
    return <div className="today-review-link"><span>{status}</span><button type="button" className="button button-quiet" onClick={()=>openReview(group)}>查看复盘</button></div>;
  }
  function renderDayClose(group:TodayProgramGroup){
    const review=group.review;
    const generating=Boolean(review&&["QUEUED","RUNNING"].includes(review.status));
    const label=generating?"正在复盘…":review?.evidence_stale?"更新复盘":review?.status==="FAILED"?"重试复盘":review?"查看复盘":"今天先到这里";
    const canClose=Boolean(group.has_execution_record||group.day_time?.has_execution_record||review);
    return <div className="daily-day-close"><div className="daily-time-summary">{group.day_time?<><span>今日已记录 {group.day_time.spent_minutes} 分钟</span>{group.day_time.time_complete&&group.day_time.remaining_minutes!==null?<span>剩余 {group.day_time.remaining_minutes} 分钟</span>:<span>部分用时未记录</span>}</>:<span>今日用时尚未记录</span>}</div>{(!embedded||!review||review.evidence_stale||review.status==="FAILED")&&<button type="button" className="button button-secondary" disabled={busy!==null||generating||!canClose} onClick={()=>{if(review&&!review.evidence_stale&&review.status!=="FAILED"){openReview(group);}else void closeDay(group);}}>{label}</button>}</div>;
  }
  function renderCompleted(group:TodayProgramGroup){const actions=group.completed??[];return actions.length?<details className="daily-completed-list"><summary>{group.program.objective_title} · 已完成 {actions.length} 项</summary>{actions.map(action=>renderAction(action))}</details>:null;}
  function renderAdjustmentForm(id:string,version:number){return adjusting===id&&!proposal?<form className="adjustment-form" onSubmit={event=>{event.preventDefault();void propose(id,version)}}><label>为什么要调整？<textarea required maxLength={2000} value={reason} onChange={event=>setReason(event.target.value)}/></label><button className="button button-primary" disabled={busy!==null||!reason.trim()} type="submit">生成调整预览</button></form>:null;}
  function renderReview(group:TodayProgramGroup){
    const target=proposal?.program_id===group.program.id?proposal:group.review?.proposal;
    return <>
      {group.review&&<DailyReviewCard review={group.review} busy={busy!==null} onRetry={()=>void retryReview(group.review!.id)}/>}
      {renderAdjustmentForm(group.program.id,group.program.version)}
      {target&&<AdjustmentProposalCard proposal={target} busy={busy!==null} onAccept={()=>void decide("accept",target)} onReject={()=>void decide("reject",target)} onSync={()=>void sync(false,target)} onRebaseSync={()=>void sync(true,target)}/>}
    </>;
  }
  if(!data)return <section className="today-load-state" aria-busy={loading}>
    {!embedded&&<h2>今日行动</h2>}
    {loadError?<div className="today-load-error" role="alert"><h3>今日行动暂时无法加载</h3><p>{loadError}</p><button className="button button-primary" disabled={loading} type="button" onClick={()=>void load()}>{loading?"正在重试…":"重试加载"}</button></div>:<p role="status">正在加载今天的行动…</p>}
  </section>;
  const linkedPlanIds=new Set(programs.map(program=>program.source_plan_document_id));
  const pendingPlans=programsLoaded?plans.filter(plan=>!linkedPlanIds.has(plan.id)):[];
  const selectedProgram=programs.find(program=>selectedItem===`program:${program.id}`)??null;
  const selectedPlan=pendingPlans.find(plan=>selectedItem===`plan:${plan.id}`)??null;
  const selected=data.programs.find(group=>selectedItem===`program:${group.program.id}`)??null;
  const statusLabel:Record<string,string>={DRAFT:"待激活",ACTIVE:"执行中",PAUSED:"已暂停",COMPLETED:"已完成",CANCELLED:"已取消"};
  const programTitle=(program:GoalProgram)=>program.objective_title.trim()||plans.find(plan=>plan.id===program.source_plan_document_id)?.title||"未命名执行项目";
  const programStatus=(program:GoalProgram)=>program.status==="DRAFT"?program.compile_status==="FAILED"?"生成失败":program.compile_status==="COMPILING"?"生成中":"待激活":statusLabel[program.status];
  const nextAction=selected?.overdue[0]??selected?.today[0]??selectedProgram?.actions.filter(action=>action.status==="SCHEDULED").sort((left,right)=>left.scheduled_date.localeCompare(right.scheduled_date)||left.position-right.position)[0]??null;
  const todayCount=data.programs.reduce((total,group)=>total+group.today.length,0);
  const overdueCount=data.programs.reduce((total,group)=>total+group.overdue.length,0);
  const todayMinutes=data.programs.reduce((total,group)=>total+group.today_estimated_minutes,0);
  const decisionPrograms=programs.filter(program=>program.status==="DRAFT"||program.status==="PAUSED"||(program.status==="ACTIVE"&&program.progress.completion_ready));
  const needsDecision=(group:TodayProgramGroup)=>Boolean(group.review&&(group.review.evidence_stale||group.review.status==="FAILED"||group.review.adjustment_status==="FAILED"||group.review.proposal?.status==="PENDING"));

  const decisionCount=decisionPrograms.length+pendingPlans.length;
  const renderInboxReview=(group:TodayProgramGroup)=>{
    const contents=<><header className="today-inbox-context"><h4>{group.program.objective_title}</h4><button className="button button-quiet" type="button" onClick={()=>selectProgram(group.program.id)}>查看执行项目</button></header>{renderReview(group)}</>;
    if(!needsDecision(group))return <div className="today-inbox-review" key={group.program.id}>{contents}</div>;
    const review=group.review!;
    const status=review.evidence_stale?"复盘待更新":review.status==="FAILED"?"复盘待重试":review.adjustment_status==="FAILED"?"调整建议待重试":"调整待确认";
    return <details className="today-inbox-review today-review-decision" key={group.program.id}><summary aria-label={`${group.program.objective_title} 复盘与调整`}><strong>{group.program.objective_title}</strong><time dateTime={review.local_date}>{review.local_date}</time><span>{status}</span></summary>{contents}</details>;
  };
  return <div className={`today-workspace${embedded?" today-embedded":""}`}>
    {!embedded&&<header className="today-page-toolbar"><div><h2>今日行动</h2><p>{new Intl.DateTimeFormat("zh-CN",{dateStyle:"long"}).format(new Date())}</p></div><div className="today-view-switch" role="group" aria-label="行动视图"><button type="button" aria-pressed={view==="inbox"} className={view==="inbox"?"button button-primary":"button button-quiet"} onClick={()=>selectProgram(null)}>今日清单</button><button type="button" aria-pressed={view==="projects"} className={view==="projects"?"button button-primary":"button button-quiet"} onClick={openProjectList}>执行项目</button></div></header>}
    {!embedded&&view==="projects"&&selectedItem&&<button className="button button-quiet today-project-back" type="button" onClick={openProjectList}>返回项目列表</button>}
    {actionLinkLoading&&<p role="status">正在定位关联行动…</p>}
    {actionLinkError&&<div className="today-load-warning" role="alert"><span>无法打开关联行动：{actionLinkError}</span><button className="button button-secondary" type="button" onClick={()=>setActionLinkRetry(value=>value+1)}>重试定位行动</button><button className="button button-quiet" type="button" onClick={()=>selectProgram(null)}>返回今日清单</button></div>}
    {error&&<p className="error-message" role="alert">{error}</p>}
    {loadError&&<div className="today-load-warning" role="alert"><span>今日数据更新失败，当前显示上次记录。{loadError}</span><button className="button button-secondary" disabled={loading} type="button" onClick={()=>void load()}>重试加载</button></div>}
    {(detailErrors.programs||detailErrors.plans)&&<div className="today-load-warning" role="alert"><span>{detailErrors.programs?`执行项目加载失败：${detailErrors.programs}`:""}{detailErrors.plans?` 计划资料加载失败：${detailErrors.plans}`:""}</span><button className="button button-secondary" disabled={detailsLoading} type="button" onClick={()=>void loadDetails()}>重试项目资料</button></div>}
    {view==="inbox"?<div className="today-inbox">
      <div className="today-inbox-summary" aria-label="今日行动概况"><strong>{todayCount} 项今日行动</strong><span>预计 {todayMinutes} 分钟</span>{overdueCount>0&&<span>{overdueCount} 项逾期</span>}{decisionCount>0&&<span>{decisionCount} 项待决定</span>}</div>
      {decisionCount>0&&<section className="today-inbox-section" aria-labelledby="today-decisions-title"><header><h3 id="today-decisions-title">待决定</h3><span>{decisionCount}</span></header>
        {decisionPrograms.map(program=><article className="today-inbox-decision" key={program.id}><div><span className={`program-status status-${program.status.toLowerCase()}`}>{programStatus(program)}</span><h4>{programTitle(program)}</h4></div><button className="button button-secondary" type="button" onClick={()=>selectProgram(program.id)}>{program.status==="DRAFT"?"查看执行预览":program.status==="PAUSED"?"查看暂停目标":"确认目标完成"}</button></article>)}
        {pendingPlans.map(plan=><article className="today-inbox-decision" key={plan.id}><div><span className="program-status status-draft">待启动</span><h4>{plan.title}</h4></div><button className="button button-secondary" disabled={!onOpenPlan} type="button" onClick={()=>onOpenPlan?.(plan.id)}>设置执行并启动</button></article>)}

      </section>}
      {data.programs.map(group=><section className="today-inbox-section today-plan-group" key={group.program.id} aria-label={group.program.objective_title}>
        <header className="today-inbox-context"><div><h3>{group.program.objective_title}</h3><small>{compactDate(group.local_date)} · {group.today.length} 项待办</small></div><button className="button button-quiet" type="button" onClick={()=>selectProgram(group.program.id)}>查看计划</button></header>
        {group.today.map(action=>renderAction(action))}
        {group.overdue.length>0&&<details className="today-overdue-list"><summary>此前逾期 · {group.overdue.length} 项</summary>{group.overdue.map(action=>renderAction(action,true))}</details>}
        {renderCompleted(group)}
        {renderDayClose(group)}
        {embedded?renderReviewLink(group):group.review?renderInboxReview(group):null}
      </section>)}
      {todayCount===0&&overdueCount===0&&<section className="today-complete-state"><h3>{data.programs.length?"今天没有待完成行动":"今天还没有行动"}</h3>{decisionCount===0&&<a className="button button-primary" href="/">开始一个目标</a>}</section>}

    </div>:<div className="today-page">
    {!embedded&&<aside className="today-list" aria-label="执行项目">
      <header className="today-list-head"><div><h3>执行项目</h3></div><span className="today-count">{programs.length+pendingPlans.length}</span></header>
      <div className="today-list-scroll">
        {programs.length>0&&<section className="today-list-group" aria-labelledby="execution-projects-label"><h3 id="execution-projects-label">全部项目</h3>{programs.map(program=><button aria-pressed={selectedItem===`program:${program.id}`} className={`today-list-item${selectedItem===`program:${program.id}`?" active":""}`} key={program.id} type="button" onClick={()=>selectProgram(program.id)}><span className={`program-status status-${program.status==="DRAFT"&&program.compile_status==="FAILED"?"failed":program.status.toLowerCase()}`}>{programStatus(program)}</span><strong>{programTitle(program)}</strong><span>{program.start_date} - {program.end_date}</span><small>{program.progress.required_total>0?`必做 ${program.progress.required_completed}/${program.progress.required_total}`:program.status==="DRAFT"?program.compile_status==="FAILED"?"执行预览未生成":"尚未生成行动":"暂无必做行动"}</small></button>)}</section>}
        {pendingPlans.length>0&&<section className="today-list-group" aria-labelledby="pending-plans-label"><h3 id="pending-plans-label">待启动计划</h3>{pendingPlans.map(plan=><button aria-pressed={selectedItem===`plan:${plan.id}`} className={`today-list-item today-plan-item${selectedItem===`plan:${plan.id}`?" active":""}`} key={plan.id} type="button" onClick={()=>setSelectedItem(`plan:${plan.id}`)}><span className="program-status status-draft">待启动</span><strong>{plan.title}</strong><span>{plan.version?`计划 v${plan.version}`:"尚无可用版本"}</span><small>保存于 {new Intl.DateTimeFormat("zh-CN",{month:"numeric",day:"numeric"}).format(new Date(plan.updated_at))}</small></button>)}</section>}
        {programs.length===0&&pendingPlans.length===0&&<div className="today-list-empty">{detailsLoading?<p role="status">正在加载执行项目…</p>:detailErrors.programs?<p>执行项目暂时不可用</p>:<><strong>还没有目标或计划</strong><a href="/">开始一个目标</a></>}</div>}
      </div>
    </aside>}
    <section className="today-detail" aria-label="行动详情">{selectedProgram?<section className="today-program" key={selectedProgram.id}>
<header className="today-program-header"><div><span className="eyebrow">{programStatus(selectedProgram)} · {selectedProgram.timezone}</span>{!embedded&&<><h3>{programTitle(selectedProgram)}</h3>{selectedProgram.objective_summary&&<p className="today-program-summary">{selectedProgram.objective_summary}</p>}</>}<p>{selectedProgram.status==="DRAFT"?selectedProgram.compile_status==="FAILED"?"执行预览生成失败":"执行预览尚未激活":`${selected?`待办预计 ${selected.today_estimated_minutes} 分钟`:"今日待办用时未知"} · ${selectedProgram.progress.required_total>0?`必做 ${selectedProgram.progress.required_completed}/${selectedProgram.progress.required_total}`:"暂无必做行动"}`}</p></div><div className="button-row">{selectedProgram.status==="ACTIVE"&&<><button className="button button-quiet" disabled={busy!==null} type="button" onClick={()=>{setAdjusting(selectedProgram.id);setProposal(null)}}>考虑调整</button><button className="button button-secondary" disabled={busy!==null} type="button" onClick={()=>void pause(selectedProgram.id,selectedProgram.version)}>暂停</button>{selectedProgram.progress.completion_ready&&<button className="button button-primary" disabled={busy!==null} type="button" onClick={()=>setConfirm({program:selectedProgram,operation:"complete"})}>完成目标</button>}<button className="button button-quiet" disabled={busy!==null} type="button" onClick={()=>setConfirm({program:selectedProgram,operation:"cancel"})}>取消目标</button></>}{selectedProgram.status==="PAUSED"&&<><button className="button button-primary" disabled={busy!==null} type="button" onClick={()=>void transition(selectedProgram,"resume")}>恢复执行</button><button className="button button-quiet" disabled={busy!==null} type="button" onClick={()=>setConfirm({program:selectedProgram,operation:"cancel"})}>取消目标</button></>}{selectedProgram.status==="DRAFT"&&<><button className="button button-primary" type="button" onClick={()=>onOpenPlan?.(selectedProgram.source_plan_document_id)}>{selectedProgram.compile_status==="FAILED"?"返回计划并重试":"返回计划并激活"}</button><button className="button button-quiet" disabled={busy!==null} type="button" onClick={()=>setConfirm({program:selectedProgram,operation:"cancel"})}>取消草稿</button></>}{["COMPLETED","CANCELLED"].includes(selectedProgram.status)&&<button className="button button-danger" disabled={busy!==null} type="button" onClick={()=>setConfirm({program:selectedProgram,operation:"delete"})}>删除记录</button>}</div></header>
      {!embedded&&selectedProgram.progress.required_total>0&&<progress aria-label={`${selectedProgram.objective_title} 完成进度`} max="1" value={selectedProgram.progress.completion_rate}/>}
      {selectedProgram.status==="DRAFT"&&<section className="goal-lifecycle-note"><strong>{selectedProgram.compile_status==="FAILED"?"执行预览生成失败":"尚未激活"}</strong><p>{selectedProgram.compile_status==="FAILED"?"计划文档已保存，但执行预览没有成功生成。返回计划页重试后，再激活目标。":"执行预览已经生成，但只有激活目标后，今日行动才会按日期产生当天行动。"}</p></section>}
      {!embedded&&<section className="today-focus-card" aria-label="当前执行重点">
        <div><span className="eyebrow">当前重点</span><h4>{selectedProgram.status==="DRAFT"?selectedProgram.compile_status==="FAILED"?"重新生成执行预览":"完成最后一步：激活目标":nextAction?`下一步：${nextAction.title}`:selectedProgram.status==="ACTIVE"?"今天的行动已处理完":"当前没有待执行行动"}</h4><p>{selectedProgram.status==="DRAFT"?selectedProgram.compile_status==="FAILED"?"返回计划页检查设置并重新生成，成功后即可激活。":"返回计划确认执行日期和每日时长，激活后即可开始。":nextAction?`${compactDate(nextAction.scheduled_date)} · 预计 ${nextAction.estimated_minutes} 分钟`:"可以查看复盘和完整日程，确认接下来的节奏。"}</p></div>
        <div className="today-progress-number"><strong>{selectedProgram.progress.required_total>0?`${Math.round(selectedProgram.progress.completion_rate*100)}%`:"--"}</strong><span>{selectedProgram.progress.required_total>0?"目标进度":"尚未生成行动"}</span></div>
      </section>}
      {(!embedded||contentView==="review")&&selectedProgram.completion_summary&&<section className="goal-completion-summary"><span className="eyebrow">目标总结</span><h4>目标周期总结</h4><p>{selectedProgram.completion_summary}</p><small>已保存到长期记忆</small></section>}
      {selectedProgram.status==="PAUSED"&&<section className="goal-lifecycle-note"><strong>执行已暂停</strong><p>行动和历史记录都已保留，恢复后会重新出现在今日行动。</p></section>}
      {selectedProgram.status==="CANCELLED"&&<section className="goal-lifecycle-note"><strong>目标已取消</strong><p>历史行动保留，仅从日常执行中移除。</p></section>}
      {contentView==="review"?<>
        {selected?.review?renderReview(selected):<><div className="today-review-empty"><h4>暂无今日复盘</h4><p>记录执行进度并结束今天后，复盘会出现在这里。</p></div>{renderAdjustmentForm(selectedProgram.id,selectedProgram.version)}{proposal?.program_id===selectedProgram.id&&<AdjustmentProposalCard proposal={proposal} busy={busy!==null} onAccept={()=>void decide("accept",proposal)} onReject={()=>void decide("reject",proposal)} onSync={()=>void sync(false,proposal)} onRebaseSync={()=>void sync(true,proposal)}/>}</>}
      </>:<>
        {selected&&<>
          {selected.overdue.length>0&&<div className="today-action-section"><h4>此前逾期</h4>{selected.overdue.map(action=>renderAction(action,true))}</div>}
          <div className="today-action-section"><h4>今天</h4>{selected.today.length?selected.today.map(action=>renderAction(action)):<div className="today-complete-state"><strong>今天的行动已处理完</strong></div>}</div>
          {renderCompleted(selected)}
          {renderDayClose(selected)}
          {embedded?renderReviewLink(selected):renderReview(selected)}
        </>}
        {embedded&&adjusting===selectedProgram.id&&<>{renderAdjustmentForm(selectedProgram.id,selectedProgram.version)}{proposal?.program_id===selectedProgram.id&&<AdjustmentProposalCard proposal={proposal} busy={busy!==null} onAccept={()=>void decide("accept",proposal)} onReject={()=>void decide("reject",proposal)} onSync={()=>void sync(false,proposal)} onRebaseSync={()=>void sync(true,proposal)}/>}</>}
        <ProgramDayPlan program={selectedProgram} localDate={selected?.local_date} busyId={busy} onComplete={action=>void complete(action,{})} onReopen={action=>void mutate(action,"reopen")}/>
      </>}
    </section>:selected?<section className="today-program">{!embedded&&<header className="today-program-header"><h3>{selected.program.objective_title}</h3></header>}{contentView==="review"?selected.review?renderReview(selected):<p>暂无今日复盘</p>:<>{selected.overdue.map(action=>renderAction(action,true))}{selected.today.map(action=>renderAction(action))}{embedded?renderReviewLink(selected):renderReview(selected)}</>}</section>:selectedPlan?<section className="today-pending-detail"><span className="program-status status-draft">待启动计划</span><div><span className="eyebrow">已保存 · 尚未执行</span><h3>{selectedPlan.title}</h3><p>这份计划已经保存，但还没有设置执行日期并激活，所以今日行动暂时不会生成当天行动。</p></div><dl><div><dt>计划版本</dt><dd>{selectedPlan.version?`v${selectedPlan.version}`:"尚无可用版本"}</dd></div><div><dt>文件状态</dt><dd>{selectedPlan.file_status==="ready"?"已就绪":"仍在准备"}</dd></div></dl><button className="button button-primary" disabled={!onOpenPlan} type="button" onClick={()=>onOpenPlan?.(selectedPlan.id)}>设置执行并启动</button></section>:<div className="today-empty-detail"><h3>{selectedItem?(detailsLoading?"正在加载执行项目":"这个执行项目暂时不可用"):"选择一个执行项目"}</h3>{selectedItem&&!detailsLoading&&<button className="button button-secondary" type="button" onClick={()=>void loadDetails()}>重试项目资料</button>}</div>}
    </section>
    </div>}
    <ConfirmDialog open={Boolean(confirm)} title={confirm?.operation==="complete"?"确认完成目标？":confirm?.operation==="cancel"?"取消这个目标？":"删除执行记录？"} description={confirm?.operation==="complete"?"确认后目标将进入已完成状态，并生成一条有限的执行总结保存到记忆。":confirm?.operation==="cancel"?"未来未执行行动将取消，已发生的行动历史会保留。":"删除后将从执行项目列表隐藏，计划文档不会被删除。"} confirmLabel={confirm?.operation==="complete"?"确认完成":confirm?.operation==="cancel"?"确认取消":"确认删除"} busy={busy!==null} onCancel={()=>setConfirm(null)} onConfirm={()=>{if(!confirm)return;if(confirm.operation==="delete")void remove(confirm.program);else void transition(confirm.program,confirm.operation)}}/>
    {notice&&<AppToast message={notice.message} tone={notice.tone} onDismiss={()=>setNotice(null)}/>}
  </div>;
}
