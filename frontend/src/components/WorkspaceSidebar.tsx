import { useEffect, useRef, useState } from "react";
import { Activity, BookOpen, CalendarCheck2, ChartNoAxesCombined, ChevronDown, Clock3, FlaskConical, History, Menu, MessageSquare, PanelLeftClose, Plus, Search, Settings2, SlidersHorizontal, Target, Trash2, Workflow, X, type LucideIcon } from "lucide-react";
import type { Bootstrap, Run, Thread } from "../types";
import { pagePaths, type WorkspacePage } from "../navigation";
export type { WorkspacePage } from "../navigation";

interface WorkspaceSidebarProps {
  activePage: WorkspacePage;
  bootstrap: Bootstrap | null;
  run: Run | null;
  threads?: Thread[];
  activeThreadId?: string | null;
  onNavigate: (page: WorkspacePage) => void;
  onNewConversation: () => void;
  onSelectThread?: (threadId: string) => void;
  onDeleteThread?: (threadId: string) => void;
  onHumanMode?: (enabled: boolean) => void;
}

const workItems: Array<{ id: WorkspacePage; label: string; icon: LucideIcon }> = [
  { id: "chat", label: "对话", icon: MessageSquare },
  { id: "workspace", label: "计划", icon: Target },
  { id: "research", label: "研究", icon: Search },
];
const controlItems: Array<{ id: WorkspacePage; label: string; icon: LucideIcon }> = [
  { id: "models", label: "模型", icon: SlidersHorizontal },
  { id: "usage", label: "用量", icon: ChartNoAxesCombined },
  { id: "skills", label: "技能", icon: Workflow },
  { id: "evaluation", label: "评测", icon: FlaskConical },
  { id: "trajectory", label: "运行轨迹", icon: Activity },
];

export default function WorkspaceSidebar({ activePage, bootstrap, run, threads = [], activeThreadId = null, onNavigate, onNewConversation, onSelectThread, onDeleteThread, onHumanMode }: WorkspaceSidebarProps) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const controlActive = controlItems.some(item => item.id === activePage);
  const [controlsOpen, setControlsOpen] = useState(controlActive);
  useEffect(() => { if (controlActive) setControlsOpen(true); }, [controlActive]);
  useEffect(() => {
    if (!open) return;
    dialog.current?.showModal?.();
    const media = window.matchMedia?.("(min-width: 901px)");
    const closeOnDesktop = () => { if (media?.matches) setOpen(false); };
    media?.addEventListener("change", closeOnDesktop);
    return () => { dialog.current?.close?.(); media?.removeEventListener("change", closeOnDesktop); trigger.current?.focus(); };
  }, [open]);
  function choose(page: WorkspacePage) { setOpen(false); onNavigate(page); }
  const selectedPage = activePage === "plan" || activePage === "today" ? "workspace" : activePage === "schedules" ? "research" : activePage;
  const navItem = (item: { id: WorkspacePage; label: string; icon: LucideIcon }) => {
    const Icon = item.icon;
    return <a key={item.id} href={pagePaths[item.id]} aria-current={selectedPage === item.id ? "page" : undefined} className={selectedPage === item.id ? "workbench-nav-item active" : "workbench-nav-item"} onClick={event => {
      if(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); choose(item.id);
    }}><Icon size={18} aria-hidden="true"/><span>{item.label}</span>{item.id === "chat" && Boolean(run?.pending_approvals.length) && <small>{run!.pending_approvals.length}</small>}</a>;
  };
  const visibleThreads = threads.filter(thread => thread.title.toLocaleLowerCase().includes(filter.toLocaleLowerCase()));
  const content = (mobile = false) => <>
    <div className="workbench-brand"><span className="workbench-monogram">BA</span><strong>Better Agent</strong>{mobile && <button className="icon-button" aria-label="关闭导航" title="关闭导航" onClick={() => setOpen(false)}><X size={18}/></button>}</div>
    <button className="workbench-new" onClick={() => { setOpen(false); onNewConversation(); }}><Plus size={18} aria-hidden="true"/>新建会话</button>
    <nav className="workbench-nav" aria-label="工作区导航">{workItems.map(navItem)}</nav>
    <nav className="workbench-nav workbench-personal" aria-label="个人导航">
      {navItem({ id: "memory", label: "记忆", icon: BookOpen })}
      {navItem({ id: "growth", label: "回顾与改进", icon: History })}
    </nav>
    <div className="workbench-history">
      <div className="workbench-section-label"><span>会话历史</span><span>{threads.length || ""}</span></div>
      {threads.length > 5 && <label className="workbench-history-search"><Search size={14} aria-hidden="true"/><input aria-label="搜索会话历史" placeholder="搜索会话" value={filter} onChange={event => setFilter(event.target.value)}/></label>}
      <nav className="workbench-history-list" aria-label="会话历史">{visibleThreads.map(thread => <div className={activeThreadId === thread.id ? "workbench-history-row active" : "workbench-history-row"} key={thread.id}>
        <a href={`/threads/${thread.id}`} title={thread.title} aria-current={activeThreadId === thread.id ? "page" : undefined} onClick={event => { if(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); setOpen(false); onSelectThread?.(thread.id); }}><span>{thread.title}</span><small>{new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(new Date(thread.updated_at))}</small></a>
        <button className="icon-button" aria-label={`删除会话：${thread.title}`} title="删除会话" onClick={() => {setOpen(false);onDeleteThread?.(thread.id);}}><Trash2 size={14}/></button>
      </div>)}{!visibleThreads.length && <p>{filter ? "没有匹配的会话" : "还没有历史会话"}</p>}</nav>
    </div>
    <div className="workbench-sidebar-bottom">
      <details open={controlsOpen} onToggle={event => setControlsOpen(event.currentTarget.open)} className="workbench-controls"><summary><SlidersHorizontal size={18} aria-hidden="true"/><span>控制台</span><ChevronDown size={14} aria-hidden="true"/></summary><nav aria-label="控制台导航">{controlItems.map(navItem)}</nav></details>
      <details className="workbench-settings"><summary><Settings2 size={18} aria-hidden="true"/><span>设置</span><ChevronDown size={14} aria-hidden="true"/></summary><div className="workbench-settings-content">
        <span>模型连接</span><strong>{bootstrap?.api_key_configured ? "模型已连接" : "等待模型配置"}</strong>
        <label><span>真人对话模式</span><input type="checkbox" checked={bootstrap?.human_mode ?? false} onChange={event => onHumanMode?.(event.target.checked)}/></label>
        <a href="/models" onClick={event=>{event.preventDefault();choose("models");}}>管理模型连接</a>
      </div></details>
    </div>
  </>;
  return <>
    <aside className="workbench-sidebar" aria-label="工作区侧栏">{content()}</aside>
    <button ref={trigger} className="icon-button workbench-menu-toggle" aria-label="打开导航" title="打开导航" aria-expanded={open} onClick={()=>setOpen(true)}><Menu size={20}/></button>
    {open && <dialog ref={dialog} className="workbench-drawer" aria-label="工作区导航抽屉" onCancel={()=>setOpen(false)} onClick={event=>{if(event.target===event.currentTarget)setOpen(false);}}><div className="workbench-drawer-content">{content(true)}</div></dialog>}
  </>;
}
