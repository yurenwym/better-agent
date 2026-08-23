import { useEffect, useState } from "react";
import { getBootstrap, setHumanMode } from "./api";
import WorkspaceSidebar, { type WorkspacePage } from "./components/WorkspaceSidebar";
import type { Bootstrap, Run } from "./types";
import ChatPage from "./pages/ChatPage";
import PlanPage from "./pages/PlanPage";
import TrajectoryPage from "./pages/TrajectoryPage";
import MemoryPage from "./pages/MemoryPage";
import ResearchPage from "./pages/ResearchPage";
import SchedulesPage from "./pages/SchedulesPage";

const headings: Record<WorkspacePage, string> = {
  chat: "目标对话",
  plan: "计划版本",
  trajectory: "运行轨迹",
  research: "深度研究",
  schedules: "定时研究",
  memory: "长期记忆",
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

function pageFromPath(): WorkspacePage { const path=typeof window!=="undefined"?window.location.pathname:"/";if(/^\/plans\//.test(path))return "plan";if(path==="/research")return "research";if(path==="/schedules")return "schedules";return "chat"; }
export default function App() {
  const [page, setPage] = useState<WorkspacePage>(pageFromPath);
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [planId, setPlanId] = useState<string | null>(() => {
    const match = typeof window !== "undefined" ? window.location.pathname.match(/^\/plans\/([^/]+)$/) : null;
    return match?.[1] ?? null;
  });

  useEffect(() => {
    getBootstrap().then(setBootstrap).catch(() => undefined);
    const pop=()=>setPage(pageFromPath());window.addEventListener("popstate",pop);return()=>window.removeEventListener("popstate",pop);
  }, []);

  const csrfToken = bootstrap?.csrf_token ?? "";
  const state = run?.state ?? "RECEIVED";
  const widePage = page === "chat" || page === "trajectory" || page === "plan" || page === "research";
  const fluidPage = page === "chat" || page === "research";
  const showTopbar = page !== "chat";
  const showPageHeader = page !== "chat" && page !== "research";

  return (
    <div className="workspace-app">
      <WorkspaceSidebar
        activePage={page}
        bootstrap={bootstrap}
        run={run}
        onNavigate={setPage}
        onNewConversation={() => { setRun(null); setThreadId(null); setPlanId(null); window.history.pushState({}, "", "/"); setPage("chat"); }}
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
          {page === "chat" && <ChatPage csrfToken={csrfToken} run={run} threadId={threadId} onThread={setThreadId} onRun={setRun} onOpenTrajectory={() => setPage("trajectory")} onOpenPlan={(nextPlanId) => { if (nextPlanId) { setPlanId(nextPlanId); window.history.pushState({}, "", `/plans/${nextPlanId}`); } setPage("plan"); }} />}
          {page === "plan" && <PlanPage csrfToken={csrfToken} run={run} threadId={threadId} planId={planId} onRun={setRun} onSelectPlan={(nextPlanId) => { setPlanId(nextPlanId); window.history.pushState({}, "", `/plans/${nextPlanId}`); setPage("plan"); }} onDeleted={() => { setPlanId(null); setPage("chat"); window.history.pushState({}, "", "/"); }} />}
          {page === "trajectory" && <TrajectoryPage run={run} threadId={threadId} />}
          {page === "memory" && <MemoryPage csrfToken={csrfToken} />}
          {page === "research" && <ResearchPage csrfToken={csrfToken} />}
          {page === "schedules" && <SchedulesPage csrfToken={csrfToken} />}
        </div>
      </main>
    </div>
  );
}
