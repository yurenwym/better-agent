import { useCallback, useEffect, useRef, useState, lazy, Suspense, type MouseEvent } from "react";
import { ArrowLeft, Activity, Plus } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { navigateTo, pagePaths, readRoute, todayPath } from "./navigation";
import { router } from "./router";
import { queryKeys, useBootstrapQuery, useThreadsQuery } from "./queries";
import { deleteThread, getLatestExpertRun, setHumanMode } from "./api";
import WorkspaceSidebar, { type WorkspacePage } from "./components/WorkspaceSidebar";
import ConfirmDialog from "./components/ConfirmDialog";
import AppToast from "./components/AppToast";
import { clearConversationDraft } from "./components/ConversationThread";
import type { Bootstrap, Run, Thread } from "./types";
import type { AgentRun } from "./types";
import { runStateLabels } from "./localization";
import "./index.css";
import "./workbench.css";

const ChatPage = lazy(() => import("./pages/ChatPage"));
const PlanPage = lazy(() => import("./pages/PlanPage"));
const TrajectoryPage = lazy(() => import("./pages/TrajectoryPage"));
const MemoryPage = lazy(() => import("./pages/MemoryPage"));
const ResearchPage = lazy(() => import("./pages/ResearchPage"));
const SchedulesPage = lazy(() => import("./pages/SchedulesPage"));
const TodayPage = lazy(() => import("./pages/TodayPage"));
const GrowthPage = lazy(() => import("./pages/GrowthPage"));
const GoalWorkspacePage = lazy(() => import("./pages/GoalWorkspacePage"));
const ModelsPage = lazy(() => import("./pages/ModelsPage"));
const UsagePage = lazy(() => import("./pages/UsagePage"));
const EvaluationPage = lazy(() => import("./pages/EvaluationPage"));
const SkillsPage = lazy(() => import("./pages/SkillsPage"));
const PlanningWorkspace = lazy(() => import("./pages/PlanningWorkspace"));

function PageLoading() {
  return (
    <div className="workspace-page-loading" role="status" aria-live="polite">
      <span>加载中…</span>
    </div>
  );
}

const headings: Record<WorkspacePage, string> = {
  workspace: "计划",
  chat: "对话",
  today: "计划",
  plan: "计划",
  trajectory: "运行轨迹",
  research: "深度研究",
  schedules: "定时研究",
  memory: "长期记忆",
  growth: "回顾与改进",
  models: "模型",
  usage: "用量",
  evaluation: "评测",
  skills: "技能",
};

interface PageShellLayout { wide: boolean; fluid: boolean; topbar: boolean; pageHeader: boolean; }
// 单张 per-page 布局表：合并原 widePage/fluidPage/showTopbar/showPageHeader 四份名单，
// 各页取值与原名单一致（渲染输出的类名组合逐字节不变）。
const pageLayouts: Record<WorkspacePage, PageShellLayout> = {
  chat: { wide: true, fluid: true, topbar: false, pageHeader: false },
  workspace: { wide: true, fluid: false, topbar: true, pageHeader: false },
  today: { wide: false, fluid: true, topbar: true, pageHeader: false },
  plan: { wide: true, fluid: false, topbar: true, pageHeader: false },
  trajectory: { wide: true, fluid: false, topbar: true, pageHeader: true },
  research: { wide: true, fluid: true, topbar: true, pageHeader: false },
  schedules: { wide: false, fluid: false, topbar: true, pageHeader: true },
  memory: { wide: false, fluid: false, topbar: true, pageHeader: true },
  growth: { wide: true, fluid: false, topbar: true, pageHeader: true },
  models: { wide: true, fluid: false, topbar: true, pageHeader: false },
  usage: { wide: true, fluid: false, topbar: true, pageHeader: false },
  evaluation: { wide: true, fluid: false, topbar: true, pageHeader: false },
  skills: { wide: true, fluid: false, topbar: true, pageHeader: false },
};

export default function App() {
  const [route, setRoute] = useState(readRoute);
  const { page, threadId, planId, workspaceResourceId, evaluationId } = route;
  const lastThreadId = useRef<string | null>(threadId);
  const navigationRevision = useRef(0);
  const currentRoute = useRef(route);
  currentRoute.current = route;
  const renderedRevision = navigationRevision.current;
  const queryClient = useQueryClient();
  const bootstrapQuery = useBootstrapQuery();
  const bootstrap = bootstrapQuery.data ?? null;
  const bootstrapError = bootstrapQuery.isError;
  const threads = useThreadsQuery().data?.threads ?? [];
  const [run, setRun] = useState<Run | null>(null);
  const [expertRun, setExpertRun] = useState<AgentRun | null>(null);
  const [threadToDelete,setThreadToDelete]=useState<Thread|null>(null);
  const [deletingThread,setDeletingThread]=useState(false);
  const [notice,setNotice]=useState<{message:string;tone:"success"|"error"}|null>(null);

  useEffect(() => {
    const pop=()=>{
      const next=readRoute();
      if(JSON.stringify(next)!==JSON.stringify(currentRoute.current)) {
        navigationRevision.current++;
        if(!next.threadId || next.threadId!==currentRoute.current.threadId) { setRun(null);setExpertRun(null); }
      }
      currentRoute.current=next;setRoute(next);
    };
    // 同时订阅 React Router 导航与浏览器 popstate，覆盖 navigateTo(router.navigate) 与前进/后退两种来源。
    const unsubscribe = router.subscribe(() => pop());
    window.addEventListener("popstate",pop);
    return()=>{ unsubscribe(); window.removeEventListener("popstate",pop); };
  }, []);

  useEffect(() => {
    let active = true;
    if (threadId) lastThreadId.current = threadId;
    if (!threadId) { setExpertRun(null); return () => { active = false; }; }
    setExpertRun(null);
    void getLatestExpertRun(threadId).then((latest) => { if (active) setExpertRun(latest); }).catch(() => undefined);
    return () => { active = false; };
  }, [threadId]);

  async function confirmThreadDelete() {
    if (!threadToDelete) return;
    setDeletingThread(true);
    try {
      await deleteThread(threadToDelete.id,csrfToken);
      clearConversationDraft(threadToDelete.id);
      // 服务端状态由 Query 缓存持有：删除后只更新缓存，不再维护一份组件内副本。
      queryClient.setQueryData(queryKeys.threads, (current: { threads: Thread[] } | undefined) =>
        current ? { threads: current.threads.filter(item=>item.id!==threadToDelete.id) } : current);
      if(lastThreadId.current===threadToDelete.id)lastThreadId.current=null;
      if(threadId===threadToDelete.id){setRun(null);setExpertRun(null);navigateTo("/");}
      setThreadToDelete(null);
      setNotice({message:"会话已删除",tone:"success"});
    } catch {
      setNotice({message:"删除会话失败。请先停止正在进行的回复或研究后重试。",tone:"error"});
      setThreadToDelete(null);
    } finally {
      setDeletingThread(false);
    }
  }

  const csrfToken = bootstrap?.csrf_token ?? "";
  const state = run?.state ?? "RECEIVED";
  const layout = ["workspace","today","plan"].includes(page) ? pageLayouts.workspace : pageLayouts[page];
  const widePage = layout.wide;
  const fluidPage = layout.fluid;
  function navigate(next: WorkspacePage) {
    if((next === "chat" || next === "trajectory") && threadId) {
      const query=new URLSearchParams(window.location.search);
      if(next==="trajectory")query.set("view","activity");else query.delete("view");
      navigateTo(`/threads/${threadId}${query.size?`?${query}`:""}`);
    }
    else if(next === "chat") navigateTo(lastThreadId.current ? `/threads/${lastThreadId.current}` : "/");
    else if(next === "trajectory") navigateTo("/trajectory");
    else navigateTo(pagePaths[next]);
  }
  function openPlan(nextPlanId:string) {
    navigateTo(`/plans/${nextPlanId}`);
  }
  const acceptRun=useCallback((next:Run)=>{if(renderedRevision===navigationRevision.current)setRun(next);},[renderedRevision]);
  const acceptExpertRun=useCallback((next:AgentRun|null)=>{if(renderedRevision===navigationRevision.current)setExpertRun(next);},[renderedRevision]);
  function followLink(event: MouseEvent<HTMLDivElement>) {
    if(event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor=(event.target as HTMLElement).closest<HTMLAnchorElement>("a[href]");
    if(!anchor || anchor.target || anchor.hasAttribute("download")) return;
    const url=new URL(anchor.href);
    if(url.origin!==window.location.origin || url.hash || !/^\/(?:threads|workspace|plans|today|research|schedules|memory|growth|models|usage|evaluations|skills|trajectory)(?:\/|$)/.test(url.pathname) && url.pathname!=="/") return;
    event.preventDefault();navigateTo(url.pathname+url.search);
  }

  return (
    <div className="workspace-app workbench" onClick={followLink}>
      <WorkspaceSidebar
        activePage={page}
        activeThreadId={threadId}
        bootstrap={bootstrap}
        run={run}
        threads={threads}
        onNavigate={navigate}
        onNewConversation={() => { setRun(null); setExpertRun(null); lastThreadId.current=null; navigateTo("/"); }}
        onSelectThread={(nextThreadId)=>{setRun(null);setExpertRun(null);navigateTo(`/threads/${nextThreadId}`);}}
        onDeleteThread={(deleteThreadId)=>setThreadToDelete(threads.find(item=>item.id===deleteThreadId)??null)}
        onHumanMode={(enabled)=>{void setHumanMode(enabled,csrfToken).then(result=>queryClient.setQueryData(queryKeys.bootstrap, (current: Bootstrap | null | undefined)=>current?{...current,human_mode:result.human_mode}:current)).catch(()=>setNotice({message:"真人对话模式设置失败，请稍后重试。",tone:"error"}))}}
      />
      <main className={`workspace-main${fluidPage ? " workspace-main-viewport" : ""}`} id="main-content">
        {(
          <header className="workspace-topbar">
            <div className="workspace-title-lockup">
              <h1>{page === "chat" && threadId ? threads.find(thread=>thread.id===threadId)?.title ?? "当前对话" : headings[page]}</h1>
            </div>
            <div className="topbar-meta">
              {(page === "chat" || page === "trajectory") && run && <span className={`state-pill state-${state.toLowerCase()}`}>{runStateLabels[state] ?? state}</span>}
              {bootstrapError && <span className="local-mark backend-offline">后端未连接，操作可能不可用</span>}
              {page === "chat" && threadId && <button className="icon-button" title="运行详情" aria-label="运行详情" onClick={()=>navigate("trajectory")}><Activity size={18}/></button>}
              {page === "trajectory" && <button className="button button-quiet" onClick={()=>navigate("chat")}><ArrowLeft size={16}/>返回对话</button>}
              {page === "workspace" && <button className="button button-primary" onClick={()=>navigateTo("/")}><Plus size={16}/>新建计划</button>}
            </div>
          </header>
        )}

        <div className={`workspace-page workspace-page-${page}${widePage ? " workspace-page-wide" : ""}${fluidPage ? " workspace-page-fluid" : ""}`}>
          <Suspense fallback={<PageLoading />}>
          {page === "chat" && <ChatPage csrfToken={csrfToken} run={run} threadId={threadId} sourceActionId={route.actionId} initialExpertRun={expertRun} onThread={(nextThreadId)=>{if(renderedRevision===navigationRevision.current)navigateTo(`/threads/${nextThreadId}`);void queryClient.invalidateQueries({queryKey:queryKeys.threads});}} onRun={acceptRun} onExpertRun={acceptExpertRun} onOpenTrajectory={() => navigate("trajectory")} onOpenPlan={(nextPlanId) => nextPlanId ? openPlan(nextPlanId) : navigate("plan")} />}
          {["workspace","today","plan"].includes(page) && <PlanningWorkspace csrfToken={csrfToken} route={route}/>}
          {page === "trajectory" && <TrajectoryPage run={run} threadId={threadId} expertRun={expertRun} csrfToken={csrfToken} onExpertRun={acceptExpertRun} />}
          {page === "memory" && <MemoryPage csrfToken={csrfToken} />}
          {page === "research" && <ResearchPage csrfToken={csrfToken} jobId={route.jobId} onSelectJob={id=>navigateTo(id?`/research?${new URLSearchParams({job:id})}`:"/research")} onOpenSchedules={()=>navigate("schedules")} />}
          {page === "schedules" && <SchedulesPage csrfToken={csrfToken} />}
          {page === "growth" && <GrowthPage csrfToken={csrfToken} view={route.growthView} onViewChange={view=>navigateTo(view==="agent"?"/growth?view=agent":"/growth")} />}
          {page === "models" && <ModelsPage csrfToken={csrfToken} />}
          {page === "usage" && <UsagePage csrfToken={csrfToken} />}
          {page === "evaluation" && <EvaluationPage evaluationId={evaluationId} csrfToken={csrfToken} />}
          {page === "skills" && <SkillsPage csrfToken={csrfToken} />}
          </Suspense>
        </div>
      </main>
      <ConfirmDialog open={Boolean(threadToDelete)} title="删除会话？" description={`确定删除“${threadToDelete?.title??""}”吗？已保存的计划、研究和目标不会被删除。`} busy={deletingThread} onCancel={()=>setThreadToDelete(null)} onConfirm={()=>void confirmThreadDelete()} />
      {notice&&<AppToast message={notice.message} tone={notice.tone} onDismiss={()=>setNotice(null)} />}
    </div>
  );
}
