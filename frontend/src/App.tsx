import { useState } from "react";

const pages = ["对话", "计划", "轨迹", "记忆"] as const;

export default function App() {
  const [page, setPage] = useState<(typeof pages)[number]>("对话");
  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <span className="eyebrow">LOCAL / SINGLE USER</span>
          <h1>Better Agent</h1>
        </div>
        <span className="state-pill">RECEIVED</span>
      </header>
      <nav className="tabs" aria-label="主页面">
        {pages.map((item) => (
          <button className={item === page ? "tab active" : "tab"} key={item} onClick={() => setPage(item)}>
            {item}
          </button>
        ))}
      </nav>
      <section className="card">
        <span className="eyebrow">V1 WORKSPACE</span>
        <h2>{page}</h2>
        <p>本地目标执行与反思工作台已启动。</p>
      </section>
    </main>
  );
}
