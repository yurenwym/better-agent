import { useEffect, useRef, useState } from "react";
import { ArrowLeft, X } from "lucide-react";
import { getGoalAction, getGoalWorkspace, listGoalPrograms } from "../api";
import { navigateTo, readRoute } from "../navigation";
import type { GoalWorkspace } from "../types";
import TodayPage from "./TodayPage";
import PlanPage from "./PlanPage";
import GoalWorkspacePage from "./GoalWorkspacePage";
import ChatPage from "./ChatPage";
import "./planning-workspace.css";

export default function PlanningWorkspace({csrfToken,route}:{csrfToken:string;route:ReturnType<typeof readRoute>}) {
  const query = new URLSearchParams(window.location.search);
  const library = query.get("view") === "all" || (route.page === "plan" && !route.planId);
  const resource = route.workspaceResourceId || route.planId;
  const [workspace,setWorkspace] = useState<GoalWorkspace|null>(null);
  const [error,setError] = useState("");
  const [reload,setReload] = useState(0);
  const [help,setHelp] = useState<{threadId:string;actionId:string}|null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const from = query.get("from") === "all" ? "all" : "today";
  const requestedTab = query.get("tab");
  const tab = route.page === "plan" ? "plan" : requestedTab === "review" || window.location.hash === "#goal-review" ? "review" : requestedTab === "plan" ? "plan" : requestedTab === "actions" ? "actions" : workspace?.program ? "actions" : "plan";
  useEffect(()=>{
    let active=true;setWorkspace(null);setError("");
    async function load(){
      try {
        let id=resource;
        if(!id&&!route.programId&&route.actionId){const action=await getGoalAction(route.actionId);const result=await listGoalPrograms();id=result.programs.find(p=>p.id===action.program.id)?.source_plan_document_id??null;}
        if(!id&&route.programId){const result=await listGoalPrograms();id=result.programs.find(p=>p.id===route.programId)?.source_plan_document_id??null;}
        if(id){const value=await getGoalWorkspace(id);if(active)setWorkspace(value);}
        else if(route.programId&&active)setError("计划不存在或已删除");
      }catch{if(active)setError("计划暂时无法加载");}
    }
    void load();return()=>{active=false;};
  },[resource,route.programId,route.actionId,reload]);
  useEffect(()=>{setHelp(null);},[resource,route.programId]);
  useEffect(()=>{
    if(!help)return;
    const previous=document.activeElement as HTMLElement|null;
    dialog.current?.showModal?.();
    return()=>{dialog.current?.close?.();(document.getElementById(`action-${help.actionId}`)??previous)?.focus({preventScroll:true});};
  },[help]);
  function openPlan(id:string,section?:string){navigateTo(`/workspace/${encodeURIComponent(id)}?${new URLSearchParams({from:library?"all":from,...(section?{tab:section}:{})})}`);}
  async function openProgram(id:string|null){
    if(!id){navigateTo("/workspace");return;}
    try {const result=await listGoalPrograms();const program=result.programs.find(p=>p.id===id);if(program)openPlan(program.source_plan_document_id,"actions");else setError("计划不存在或已删除");}catch{setError("计划暂时无法加载");}
  }
  const detail=Boolean(resource||route.programId||route.actionId);
  return <div className="planning-workspace">
    <nav className="planning-views" aria-label="计划视图"><a href="/workspace" aria-current={detail?from==="today"?"page":undefined:!library?"page":undefined}>今日</a><a href="/workspace?view=all" aria-current={detail?from==="all"?"page":undefined:library?"page":undefined}>全部计划</a></nav>
    {error?<div role="alert">{error}<button type="button" onClick={()=>setReload(x=>x+1)}>重新加载</button></div>:detail&&!workspace?<p role="status">正在加载计划…</p>:workspace?<>
      <a className="planning-back" href={from==="all"?"/workspace?view=all":"/workspace"}><ArrowLeft size={16}/>{from==="all"?"返回全部计划":"返回今日"}</a>
      <header className="planning-heading"><div><h2>{workspace.plan.title}</h2><div className="planning-heading-meta"><span>方案 v{workspace.plan.version}</span>{workspace.program&&<span>{workspace.program.start_date} 至 {workspace.program.end_date}</span>}</div></div></header>
      <nav className="planning-tabs" aria-label="计划内容">{[["actions","行动"],["plan","方案"],["review","复盘"]].map(([id,label])=><a key={id} aria-current={tab===id?"page":undefined} href={`/workspace/${encodeURIComponent(workspace.plan_document_id)}?${new URLSearchParams({tab:id,from})}`}>{label}</a>)}</nav>
      {tab==="plan"?<div className="planning-document"><PlanPage embedded csrfToken={csrfToken} run={null} planId={workspace.plan_document_id} onRun={()=>{}} onSelectPlan={id=>openPlan(id,"plan")} onDeleted={()=>navigateTo("/workspace?view=all")} onOpenToday={()=>{setReload(x=>x+1);openPlan(workspace.plan_document_id,"actions");}}/></div>:workspace.program?<div className="planning-actions"><TodayPage embedded contentView={tab==="review"?"review":"actions"} onOpenReview={()=>openPlan(workspace.plan_document_id,"review")} csrfToken={csrfToken} programId={workspace.program.id} actionId={route.actionId} onSelectProgram={openProgram} onOpenPlan={id=>openPlan(id,"plan")} onHelp={(threadId,actionId)=>setHelp({threadId,actionId})}/></div>:<section><p>{tab==="review"?"开始执行并记录进度后，复盘会保存在这里。":"尚未生成每日行动。"}</p><button type="button" className="button button-primary" onClick={()=>openPlan(workspace.plan_document_id,"plan")}>查看方案并安排日程</button></section>}
    </>:library?<div className="planning-library"><GoalWorkspacePage listOrigin="all"/></div>:<div className="planning-today"><TodayPage embedded csrfToken={csrfToken} onSelectProgram={openProgram} onOpenPlan={id=>openPlan(id,"plan")} onHelp={(threadId,actionId)=>setHelp({threadId,actionId})}/></div>}
    {help&&<dialog className="planning-help" ref={dialog} onCancel={()=>setHelp(null)}><header><strong>当前行动求助</strong><a href={`/threads/${help.threadId}?action=${help.actionId}`}>打开完整对话</a><button type="button" className="icon-button" aria-label="关闭求助，返回任务" onClick={()=>setHelp(null)}><X size={20}/></button></header><div className="planning-help-body"><ChatPage embedded csrfToken={csrfToken} run={null} threadId={help.threadId} sourceActionId={help.actionId} onThread={()=>{}} onRun={()=>{}} onExpertRun={()=>{}} onOpenTrajectory={()=>{}} onOpenPlan={()=>{}}/></div></dialog>}
  </div>;
}
