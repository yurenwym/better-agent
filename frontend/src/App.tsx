import { useEffect, useState } from "react";
import { getBootstrap } from "./api";
import WorkspaceSidebar, { type WorkspacePage } from "./components/WorkspaceSidebar";
import type { Bootstrap, Run } from "./types";
import ChatPage from "./pages/ChatPage";
import PlanPage from "./pages/PlanPage";
import TrajectoryPage from "./pages/TrajectoryPage";
import MemoryPage from "./pages/MemoryPage";

const headings: Record<WorkspacePage, string> = {
  chat: "目标对话",
  plan: "计划版本",
  trajectory: "运行轨迹",
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

export default function App() {
  const [page, setPage] = useState<WorkspacePage>(() => typeof window !== "undefined" && /^\/plans\//.test(window.location.pathname) ? "plan" : "chat");
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [planId, setPlanId] = useState<string | null>(() => {
    const match = typeof window !== "undefined" ? window.location.pathname.match(/^\/plans\/([^/]+)$/) : null;
    return match?.[1] ?? null;
  });

  useEffect(() => {
    getBootstrap().then(setBootstrap).catch(() => undefined);
  }, []);

  const csrfToken = bootstrap?.csrf_token ?? "";
  const state = run?.state ?? "RECEIVED";
  const widePage = page === "chat" || page === "trajectory";
  const fluidPage = page === "chat";

  return (
    <div className="workspace-app">
      <WorkspaceSidebar
        activePage={page}
        bootstrap={bootstrap}
        run={run}
        onNavigate={setPage}
        onNewConversation={() => { setRun(null); setThreadId(null); setPlanId(null); window.history.pushState({}, "", "/"); setPage("chat"); }}
      />
      <main className={`workspace-main${fluidPage ? " workspace-main-viewport" : ""}`} id="main-content">
        {!fluidPage && (
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

        {!fluidPage && (
          <div className={`workspace-page-header workspace-page-header-${page}${widePage ? " workspace-page-header-wide" : ""}`}>
            <div><span className="eyebrow">V1 WORKSPACE</span><h2>{headings[page]}</h2></div>
            {run && <div className="run-context"><span>ACTIVE RUN</span><code title={run.id}>{run.id.slice(-8)}</code></div>}
          </div>
        )}

        <div className={`workspace-page workspace-page-${page}${widePage ? " workspace-page-wide" : ""}${fluidPage ? " workspace-page-fluid" : ""}`}>
          {page === "chat" && <ChatPage csrfToken={csrfToken} run={run} threadId={threadId} onThread={setThreadId} onRun={setRun} onOpenTrajectory={() => setPage("trajectory")} onOpenPlan={(nextPlanId) => { if (nextPlanId) { setPlanId(nextPlanId); window.history.pushState({}, "", `/plans/${nextPlanId}`); } setPage("plan"); }} />}
          {page === "plan" && <PlanPage csrfToken={csrfToken} run={run} threadId={threadId} planId={planId} onRun={setRun} />}
          {page === "trajectory" && <TrajectoryPage run={run} threadId={threadId} />}
          {page === "memory" && <MemoryPage csrfToken={csrfToken} />}
        </div>
      </main>
    </div>
  );
}
