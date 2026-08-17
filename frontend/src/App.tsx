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
          <span className="eyebrow">LOCAL / SINGLE USER</span>
          <h1>Better Agent</h1>
        </div>
        <div className="topbar-meta">
          <span className={`state-pill state-${(run?.state ?? "RECEIVED").toLowerCase()}`}>{run?.state ?? "RECEIVED"}</span>
          <span className="local-mark"><span aria-hidden="true" />127.0.0.1</span>
        </div>
      </header>
      <nav className="tabs" aria-label="主页面">
        {pages.map((item) => (
          <button aria-label={item.label} className={item.id === page ? "tab active" : "tab"} key={item.id} onClick={() => setPage(item.id)} type="button">
            <span aria-hidden="true" className="tab-index">0{pages.findIndex((pageItem) => pageItem.id === item.id) + 1}</span>{item.label}
          </button>
        ))}
      </nav>
      <div className="content-header"><div><span className="eyebrow">V1 WORKSPACE</span><span className="page-name">{headings[page]}</span></div>{run && <span className="run-id">{run.id}</span>}</div>
      {page === "chat" && <ChatPage csrfToken={csrfToken} run={run} onRun={setRun} />}
      {page === "plan" && <PlanPage csrfToken={csrfToken} run={run} onRun={setRun} />}
      {page === "trajectory" && <TrajectoryPage run={run} />}
      {page === "memory" && <MemoryPage csrfToken={csrfToken} />}
      {run && <button className="sr-only" type="button" onClick={() => void refreshRun(run.id)}>刷新当前 Run</button>}
    </main>
  );
}
