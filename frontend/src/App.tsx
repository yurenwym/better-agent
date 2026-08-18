import { useCallback, useEffect, useState } from "react";
import { getBootstrap, getRun } from "./api";
import type { Bootstrap, Run } from "./types";
import ChatPage from "./pages/ChatPage";
import PlanPage from "./pages/PlanPage";
import TrajectoryPage from "./pages/TrajectoryPage";
import MemoryPage from "./pages/MemoryPage";

type Page = "chat" | "plan" | "trajectory" | "memory";

const pages: Array<{ id: Page; label: string }> = [
  { id: "chat", label: "对话" },
  { id: "plan", label: "计划" },
  { id: "trajectory", label: "轨迹" },
  { id: "memory", label: "记忆" },
];

const headings: Record<Page, string> = {
  chat: "目标对话",
  plan: "计划版本",
  trajectory: "运行轨迹",
  memory: "长期记忆",
};

const stateLabels: Record<string, string> = {
  RECEIVED: "已收到目标",
  CLARIFYING: "正在澄清",
  PLANNING: "正在规划",
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
  const [page, setPage] = useState<Page>("chat");
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [run, setRun] = useState<Run | null>(null);

  useEffect(() => {
    getBootstrap().then(setBootstrap).catch(() => undefined);
  }, []);

  const refreshRun = useCallback(async (runId: string) => {
    setRun(await getRun(runId));
  }, []);

  const csrfToken = bootstrap?.csrf_token ?? "";

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand-lockup">
          <span className="brand-kicker"><span className="brand-mark" aria-hidden="true" />LOCAL / SINGLE USER</span>
          <h1>Better Agent</h1>
          <p className="brand-caption">一条可追溯、可暂停、可恢复的个人工作流。</p>
        </div>
        <div className="topbar-meta">
          <span className={`state-pill state-${(run?.state ?? "RECEIVED").toLowerCase()}`}>{stateLabels[run?.state ?? "RECEIVED"]}</span>
          <span className="local-mark"><span aria-hidden="true" />LOCALHOST / 127.0.0.1</span>
        </div>
      </header>
      <nav className="tabs" aria-label="主页面">
        {pages.map((item) => (
          <button aria-label={item.label} className={item.id === page ? "tab active" : "tab"} key={item.id} onClick={() => setPage(item.id)} type="button">
            <span aria-hidden="true" className="tab-index">0{pages.findIndex((pageItem) => pageItem.id === item.id) + 1}</span>{item.label}
          </button>
        ))}
      </nav>
      <div className="content-header"><div><span className="eyebrow">V1 WORKSPACE</span><span className="page-name">{headings[page]}</span></div>{run && <div className="run-context"><span>ACTIVE RUN</span><code title={run.id}>{run.id.slice(-8)}</code></div>}</div>
      {page === "chat" && <ChatPage csrfToken={csrfToken} run={run} onRun={setRun} onOpenTrajectory={() => setPage("trajectory")} />}
      {page === "plan" && <PlanPage csrfToken={csrfToken} run={run} onRun={setRun} />}
      {page === "trajectory" && <TrajectoryPage run={run} />}
      {page === "memory" && <MemoryPage csrfToken={csrfToken} />}
      {run && <button className="sr-only" type="button" onClick={() => void refreshRun(run.id)}>刷新当前 Run</button>}
    </main>
  );
}
