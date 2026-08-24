import { useEffect, useState } from "react";
import { deleteThread, getBootstrap, getLatestExpertRun, listThreads, setHumanMode } from "./api";
import WorkspaceSidebar, { type WorkspacePage } from "./components/WorkspaceSidebar";
import ConfirmDialog from "./components/ConfirmDialog";
import AppToast from "./components/AppToast";
import type { Bootstrap, Run, Thread } from "./types";
import ChatPage from "./pages/ChatPage";
import PlanPage from "./pages/PlanPage";
import TrajectoryPage from "./pages/TrajectoryPage";
import MemoryPage from "./pages/MemoryPage";
import ResearchPage from "./pages/ResearchPage";
import SchedulesPage from "./pages/SchedulesPage";
import TodayPage from "./pages/TodayPage";
import GrowthPage from "./pages/GrowthPage";
import type { AgentRun } from "./types";
import "./index.css";

const headings: Record<WorkspacePage, string> = {
  chat: "目标对话",
  today: "今天的行动",
  plan: "计划",
  trajectory: "运行轨迹",
  research: "深度研究",
  schedules: "定时研究",
  memory: "长期记忆",
  growth: "受控成长",
};

const stateLabels: Record<string, string> = {
  RECEIVED: "等待输入",
  CLARIFYING: "需要澄清",
  PLANNING: "生成计划中",
  AWAITING_APPROVAL: "等待审批",
  EXECUTING: "执行中",
  AWAITING_OUTCOME: "等待结果",
  REFLECTING: "复盘中",
  COMPLETED: "已完成",
  BLOCKED: "已阻塞",
  FAILED: "运行失败",
  CANCELLED: "已取消",
};

function pageFromPath(): WorkspacePage { const path=typeof window!=="undefined"?window.location.pathname:"/";if(path==="/plans"||/^\/plans\//.test(path))return "plan";if(path==="/today")return "today";if(path==="/research")return "research";if(path==="/schedules")return "schedules";if(path==="/growth")return "growth";return "chat"; }
function threadFromPath(): string|null { const match=typeof window!=="undefined"?window.location.pathname.match(/^\/threads\/([^/]+)$/):null;return match?.[1]??null; }
export default function App() {
  const [page, setPage] = useState<WorkspacePage>(pageFromPath);
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [expertRun, setExpertRun] = useState<AgentRun | null>(null);
  const [threadId, setThreadId] = useState<string | null>(threadFromPath);
  const [threads,setThreads]=useState<Thread[]>([]);
  const [threadToDelete,setThreadToDelete]=useState<Thread|null>(null);
  const [deletingThread,setDeletingThread]=useState(false);
  const [notice,setNotice]=useState<{message:string;tone:"success"|"error"}|null>(null);
  const [planId, setPlanId] = useState<string | null>(() => {
    const match = typeof window !== "undefined" ? window.location.pathname.match(/^\/plans\/([^/]+)$/) : null;
    return match?.[1] ?? null;
  });

  useEffect(() => {
    getBootstrap().then(setBootstrap).catch(() => undefined);
    listThreads().then(result=>setThreads(result.threads)).catch(()=>undefined);
    const pop=()=>{setPage(pageFromPath());setThreadId(threadFromPath());};window.addEventListener("popstate",pop);return()=>window.removeEventListener("popstate",pop);
  }, []);

  useEffect(() => {
    let active = true;
    if (!threadId) { setExpertRun(null); return () => { active = false; }; }
    void getLatestExpertRun(threadId).then((latest) => { if (active) setExpertRun(latest); }).catch(() => undefined);
    return () => { active = false; };
  }, [threadId]);

  async function confirmThreadDelete() {
    if (!threadToDelete) return;
    setDeletingThread(true);
    try {
      await deleteThread(threadToDelete.id,csrfToken);
      setThreads(items=>items.filter(item=>item.id!==threadToDelete.id));
      if(threadId===threadToDelete.id){setRun(null);setExpertRun(null);setThreadId(null);setPlanId(null);setPage("chat");window.history.pushState({},"","/");}
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
  const widePage = page === "chat" || page === "trajectory" || page === "plan" || page === "research" || page === "growth";
  const fluidPage = page === "chat" || page === "research" || page === "today";
  const showTopbar = page !== "chat";
  const showPageHeader = page !== "chat" && page !== "research" && page !== "today" && page !== "plan";

  return (
    <div className="workspace-app">
      <WorkspaceSidebar
        activePage={page}
        activeThreadId={threadId}
        bootstrap={bootstrap}
        run={run}
        threads={threads}
        onNavigate={(next)=>{setPage(next);const paths:Partial<Record<WorkspacePage,string>>={today:"/today",plan:"/plans",research:"/research",schedules:"/schedules",growth:"/growth"};if(paths[next])window.history.pushState({},"",paths[next]);}}
        onNewConversation={() => { setRun(null); setExpertRun(null); setThreadId(null); setPlanId(null); window.history.pushState({}, "", "/"); setPage("chat"); }}
        onSelectThread={(nextThreadId)=>{setRun(null);setThreadId(nextThreadId);setPlanId(null);window.history.pushState({},"",`/threads/${nextThreadId}`);setPage("chat");}}
        onDeleteThread={(deleteThreadId)=>setThreadToDelete(threads.find(item=>item.id===deleteThreadId)??null)}
        onHumanMode={(enabled)=>{void setHumanMode(enabled,csrfToken).then(result=>setBootstrap(current=>current?{...current,human_mode:result.human_mode}:current))}}
      />
      <main className={`workspace-main${fluidPage ? " workspace-main-viewport" : ""}`} id="main-content">
        {showTopbar && (
          <header className="workspace-topbar">
            <div className="workspace-title-lockup">
              <span className="brand-kicker"><span className="brand-mark" aria-hidden="true" />LOCAL / SINGLE USER</span>
              <h1>Better Agent</h1>
              <p>一条可追溯、可暂停、可恢复的个人工作流。</p>
            </div>
            <div className="topbar-meta">
              <span className={`state-pill state-${state.toLowerCase()}`}>{stateLabels[state] ?? state}</span>
              <span className="local-mark"><span aria-hidden="true" />127.0.0.1 · 本地运行</span>
            </div>
          </header>
        )}

        {showPageHeader && (
          <div className={`workspace-page-header workspace-page-header-${page}${widePage ? " workspace-page-header-wide" : ""}`}>
            <div><span className="eyebrow">V1 WORKSPACE</span><h2>{headings[page]}</h2></div>
            {run && <div className="run-context"><span>ACTIVE RUN</span><code title={run.id}>{run.id.slice(-8)}</code></div>}
          </div>
        )}

        <div className={`workspace-page workspace-page-${page}${widePage ? " workspace-page-wide" : ""}${fluidPage ? " workspace-page-fluid" : ""}`}>
          {page === "chat" && <ChatPage csrfToken={csrfToken} run={run} threadId={threadId} onThread={(nextThreadId)=>{setThreadId(nextThreadId);window.history.pushState({},"",`/threads/${nextThreadId}`);void listThreads().then(result=>setThreads(result.threads));}} onRun={setRun} onExpertRun={setExpertRun} onOpenTrajectory={() => setPage("trajectory")} onOpenPlan={(nextPlanId) => { if (nextPlanId) { setPlanId(nextPlanId); window.history.pushState({}, "", `/plans/${nextPlanId}`); } setPage("plan"); }} />}
          {page === "today" && <TodayPage csrfToken={csrfToken} onHelp={(nextThreadId)=>{setThreadId(nextThreadId);setPage("chat");window.history.pushState({},"","/");}} />}
          {page === "plan" && <PlanPage csrfToken={csrfToken} run={run} threadId={threadId} planId={planId} onRun={setRun} onSelectPlan={(nextPlanId) => { setPlanId(nextPlanId); window.history.pushState({}, "", `/plans/${nextPlanId}`); setPage("plan"); }} onDeleted={() => { setPlanId(null); setPage("plan"); window.history.pushState({}, "", "/plans"); }} />}
          {page === "trajectory" && <TrajectoryPage run={run} threadId={threadId} expertRun={expertRun} csrfToken={csrfToken} onExpertRun={setExpertRun} />}
          {page === "memory" && <MemoryPage csrfToken={csrfToken} />}
          {page === "research" && <ResearchPage csrfToken={csrfToken} />}
          {page === "schedules" && <SchedulesPage csrfToken={csrfToken} />}
          {page === "growth" && <GrowthPage csrfToken={csrfToken} />}
        </div>
      </main>
      <ConfirmDialog open={Boolean(threadToDelete)} title="删除会话？" description={`确定删除“${threadToDelete?.title??""}”吗？已保存的计划、研究和目标不会被删除。`} busy={deletingThread} onCancel={()=>setThreadToDelete(null)} onConfirm={()=>void confirmThreadDelete()} />
      {notice&&<AppToast message={notice.message} tone={notice.tone} onDismiss={()=>setNotice(null)} />}
    </div>
  );
}
