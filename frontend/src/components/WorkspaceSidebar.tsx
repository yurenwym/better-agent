import type { Bootstrap, Run, Thread } from "../types";

export type WorkspacePage = "chat" | "workspace" | "today" | "plan" | "trajectory" | "research" | "schedules" | "memory" | "growth" | "models" | "usage" | "evaluation" | "skills";

interface WorkspaceSidebarProps {
  activePage: WorkspacePage;
  bootstrap: Bootstrap | null;
  run: Run | null;
  threads?: Thread[];
  activeThreadId?: string | null;
  onNavigate: (page: WorkspacePage) => void;
  onNewConversation: () => void;
  onSelectThread?: (threadId:string)=>void;
  onDeleteThread?: (threadId:string)=>void;
  onHumanMode?: (enabled:boolean)=>void;
}

const navItems: Array<{ id: WorkspacePage; label: string; glyph: "chat" | "plan" | "trace" | "memory" | "growth" }> = [
  { id: "chat", label: "对话", glyph: "chat" },
  { id: "workspace", label: "目标工作区", glyph: "plan" },
  { id: "today", label: "Today", glyph: "plan" },
  { id: "plan", label: "计划", glyph: "plan" },
  { id: "trajectory", label: "轨迹", glyph: "trace" },
  { id: "research", label: "研究", glyph: "trace" },
  { id: "schedules", label: "定时", glyph: "plan" },
  { id: "memory", label: "记忆", glyph: "memory" },
  { id: "growth", label: "成长", glyph: "growth" },
  { id: "models", label: "模型", glyph: "growth" },
  { id: "usage", label: "用量", glyph: "trace" },
  { id: "evaluation", label: "评测", glyph: "plan" },
  { id: "skills", label: "Skill", glyph: "memory" },
];

function NavGlyph({ name }: { name: typeof navItems[number]["glyph"] }) {
  const paths: Record<typeof name, string> = {
    chat: "M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7a2.5 2.5 0 0 1-2.5 2.5H11l-4.5 4v-4H6.5A2.5 2.5 0 0 1 4 12.5v-7Z",
    plan: "M6 4h12M6 9h12M6 14h7M4 4h.01M4 9h.01M4 14h.01",
    trace: "M5 4v16M5 7h10a3 3 0 0 1 0 6H9a3 3 0 0 0 0 6h10",
    memory: "M5 5.5A2.5 2.5 0 0 1 7.5 3h9A2.5 2.5 0 0 1 19 5.5V19l-3.5-2-4 2-3.5-2L5 19V5.5Z",
    growth: "M12 21V10m0 0c0-4-3-6-7-6 0 4 3 6 7 6Zm0 4c0-3 2.5-5 6-5 0 3-2.5 5-6 5Z",
  };
  return <svg aria-hidden="true" className="nav-glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]} /></svg>;
}

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

export default function WorkspaceSidebar({ activePage, bootstrap, run, threads=[], activeThreadId=null, onNavigate, onNewConversation, onSelectThread, onDeleteThread, onHumanMode }: WorkspaceSidebarProps) {
  return (
    <aside className="workspace-sidebar" aria-label="工作区侧栏">
      <div className="sidebar-brand-row">
        <div className="sidebar-brand">
          <span className="sidebar-brand-mark" aria-hidden="true">BA</span>
          <div><strong>Better Agent</strong><span>本地工作区</span></div>
        </div>
        <button className="sidebar-new-session" type="button" onClick={onNewConversation}>
          <span className="sidebar-plus" aria-hidden="true">+</span>
          <span>新建会话</span>
        </button>
      </div>

      <div className="sidebar-section-label">工作区</div>
      <nav className="sidebar-nav" aria-label="工作区导航">
        {navItems.map((item) => (
          <button
            aria-current={activePage === item.id ? "page" : undefined}
            className={activePage === item.id ? "sidebar-nav-item active" : "sidebar-nav-item"}
            key={item.id}
            type="button"
            onClick={() => onNavigate(item.id)}
          >
            <NavGlyph name={item.glyph} />
            <span>{item.label}</span>
            {item.id === "trajectory" && run && <span aria-hidden="true" className="sidebar-nav-meta">实时</span>}
            {item.id === "chat" && run?.pending_approvals.length ? <span className="sidebar-badge">{run.pending_approvals.length}</span> : null}
          </button>
        ))}
      </nav>

      <div className="sidebar-section-label">会话历史</div>
      <nav className="sidebar-session-list" aria-label="会话历史">{threads.length?threads.map(thread=><div className={`sidebar-session-row${activeThreadId===thread.id?" active":""}`} key={thread.id}><button aria-current={activeThreadId===thread.id?"page":undefined} className="sidebar-session-item" type="button" onClick={()=>onSelectThread?.(thread.id)}><strong>{thread.title}</strong><span>{new Intl.DateTimeFormat("zh-CN",{month:"numeric",day:"numeric"}).format(new Date(thread.updated_at))}</span></button><button aria-label={`删除会话：${thread.title}`} className="sidebar-session-delete" type="button" onClick={()=>onDeleteThread?.(thread.id)}><svg aria-hidden="true" viewBox="0 0 24 24" fill="none"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"/></svg></button></div>):<p>还没有历史会话</p>}</nav>
      {run&&<div className="sidebar-session-card"><span className="session-dot" aria-hidden="true"/><div><strong>当前目标</strong><span>{stateLabels[run.state]??run.state}</span></div><code title={run.id}>{run.id.slice(-8)}</code></div>}

      <div className="sidebar-spacer" />
      <details className="sidebar-settings">
        <summary><svg aria-hidden="true" className="settings-glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z" /><path d="m19.4 15 .1.1a1.8 1.8 0 0 1-2.5 2.5l-.1-.1a1.8 1.8 0 0 0-3.1 1.3v.2a1.8 1.8 0 0 1-3.6 0v-.2a1.8 1.8 0 0 0-3.1-1.3l-.1.1a1.8 1.8 0 1 1-2.5-2.5l.1-.1a1.8 1.8 0 0 0-1.3-3.1h-.2a1.8 1.8 0 0 1 0-3.6h.2a1.8 1.8 0 0 0 1.3-3.1l-.1-.1a1.8 1.8 0 1 1 2.5-2.5l.1.1a1.8 1.8 0 0 0 3.1-1.3v-.2a1.8 1.8 0 0 1 3.6 0v.2a1.8 1.8 0 0 0 3.1 1.3l.1-.1a1.8 1.8 0 1 1 2.5 2.5l-.1.1a1.8 1.8 0 0 0 1.3 3.1h.2a1.8 1.8 0 0 1 0 3.6h-.2a1.8 1.8 0 0 0-1.3 3.1Z" /></svg><span>设置</span></summary>
        <div className="settings-content">
          <div><span>模型连接</span><strong>{bootstrap?.api_key_configured ? "模型已连接" : "等待模型配置"}</strong></div>
          <div><span>服务地址</span><code>127.0.0.1:8000</code></div>
          <div><span>API 环境变量</span><code>{bootstrap?.api_key_env ?? "AGENT_MODEL_API_KEY"}</code></div>
          <label className="settings-toggle"><span>真人对话模式</span><input type="checkbox" checked={bootstrap?.human_mode??false} onChange={event=>onHumanMode?.(event.target.checked)}/></label>
        </div>
      </details>
    </aside>
  );
}
