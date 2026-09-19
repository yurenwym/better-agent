import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowUpRight, MessageSquare, Plus, Search } from "lucide-react";
import { getGoalWorkspace, listPlanDocuments } from "../api";
import { navigateTo, todayPath } from "../navigation";
import type { GoalWorkspace, PlanDocumentSummary } from "../types";

interface Props { resourceId?: string | null; listOrigin?: "all"; }

const phaseLabels: Record<GoalWorkspace["phase"], string> = {
  DISCOVERING: "探索中", PLANNING: "计划中", READY_TO_START: "准备开始", EXECUTING: "执行中",
  REVIEWING: "每日复盘", ADJUSTING: "等待调整确认", COMPLETED: "已完成", PAUSED: "已暂停", CANCELLED: "已取消",
};
const fileStatusLabels: Record<string, string> = {
  ready: "已保存", pending: "保存中", failed: "保存失败", conflict: "存在冲突", deleted: "已删除",
};
const reviewStatusLabels: Record<string, string> = {
  QUEUED: "等待复盘", RUNNING: "复盘中", COMPLETED: "复盘已完成", FAILED: "复盘失败",
};

function sourceHref(source: GoalWorkspace["sources"][number], workspace: GoalWorkspace): string | null {
  if (source.kind === "plan") return `/plans/${encodeURIComponent(source.id)}`;
  if (source.kind === "program") return todayPath(source.id);
  if (source.kind === "research") return `/research?${new URLSearchParams({ job: source.id })}`;
  if (source.kind === "review" && source.id === workspace.review?.id) return "#goal-review";
  if (source.kind === "memory") return "/memory";
  return null;
}

function updatedDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}

export default function GoalWorkspacePage({ resourceId, listOrigin }: Props) {
  const [workspace, setWorkspace] = useState<GoalWorkspace | null>(null);
  const [plans, setPlans] = useState<PlanDocumentSummary[]>([]);
  const [loading, setLoading] = useState(Boolean(resourceId));
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState(false);
  const [workspaceError, setWorkspaceError] = useState(false);
  const [query, setQuery] = useState("");
  const [reload, setReload] = useState(0);
  const reviewSection = useRef<HTMLElement>(null);
  function planPath(id:string) { return `/workspace/${encodeURIComponent(id)}${listOrigin ? "?from=all" : ""}`; }

  useEffect(() => {
    let active = true;
    setListLoading(true);
    setListError(false);
    void listPlanDocuments()
      .then(value => { if (active) setPlans(value.plans); })
      .catch(() => { if (active) setListError(true); })
      .finally(() => { if (active) setListLoading(false); });
    return () => { active = false; };
  }, [reload]);

  useEffect(() => {
    let active = true;
    setWorkspaceError(false);
    setLoading(Boolean(resourceId));
    setWorkspace(null);
    if (!resourceId) return () => { active = false; };
    void getGoalWorkspace(resourceId)
      .then(value => {
        if (!active) return;
        setWorkspace(value);
        // Legacy workspace URLs may contain a thread ID; new links use the resolved plan ID.
        if (window.location.pathname === `/workspace/${encodeURIComponent(resourceId)}` && resourceId !== value.plan_document_id) {
          navigateTo(`/workspace/${encodeURIComponent(value.plan_document_id)}${window.location.search}${window.location.hash}`, true);
        }
      })
      .catch(() => { if (active) setWorkspaceError(true); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [resourceId, reload]);

  useEffect(() => {
    if (!workspace || window.location.hash !== "#goal-review") return;
    reviewSection.current?.scrollIntoView?.({ block: "start" });
    reviewSection.current?.focus({ preventScroll: true });
  }, [workspace]);

  if (loading) return <section className="goal-workspace-page" aria-busy="true"><h2>目标详情</h2><p role="status">正在加载目标进展…</p></section>;

  if (!workspace) {
    const visiblePlans = plans.filter(plan => plan.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
    return <section className="goal-library-page">
      {workspaceError && <div className="goal-load-error" role="alert"><h2>这个目标暂时无法打开</h2><p>目标不存在、已删除或暂时无法连接。</p><button className="button button-secondary" type="button" onClick={() => setReload(value => value + 1)}>重新加载</button></div>}
      <header className="goal-library-header"><div><h2>全部计划</h2>{!listLoading && !listError && <p>{plans.length} 个已保存计划</p>}</div>{plans.length > 0 && <a className="button button-primary" href="/"><Plus size={16} aria-hidden="true" />新建计划</a>}</header>
      <label className="goal-search"><span>搜索计划</span><div><Search size={16} aria-hidden="true" /><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="计划名称" /></div></label>
{listLoading ? <p role="status" aria-busy="true">正在加载目标…</p> : listError ? <div className="goal-load-error" role="alert"><h3>目标列表加载失败</h3><button className="button button-secondary" type="button" onClick={() => setReload(value => value + 1)}>重试</button></div> : plans.length === 0 ? <div className="goal-library-empty"><h3>还没有保存的目标</h3><a className="button button-primary" href="/"><Plus size={16} aria-hidden="true" />新建对话</a></div> : visiblePlans.length === 0 ? <div className="goal-library-empty"><h3>没有匹配的目标</h3><button className="button button-secondary" type="button" onClick={() => setQuery("")}>清除搜索</button></div> : <ul className="goal-list">{visiblePlans.map(plan => <li key={plan.id}><a className="goal-list-item" href={planPath(plan.id)}><div><strong>{plan.title}</strong><span>{plan.version ? `计划 v${plan.version}` : "暂无可用版本"}</span></div><div className="goal-list-item-meta"><span className={`file-status-${plan.file_status}`}>{fileStatusLabels[plan.file_status] ?? "状态待确认"}</span>{updatedDate(plan.updated_at) && <time dateTime={plan.updated_at}>{updatedDate(plan.updated_at)} 更新</time>}<ArrowUpRight size={18} aria-hidden="true" /></div></a></li>)}</ul>}
    </section>;
  }

  const planHref = `/plans/${encodeURIComponent(workspace.plan_document_id)}`;
  const executionHref = workspace.program ? todayPath(workspace.program.id) : planHref;
  const nextHref = workspace.next_action?.href === "/today" && workspace.program
    ? todayPath(workspace.program.id, workspace.next_action.kind === "complete_action" ? workspace.next_action.resource_id : undefined)
    : workspace.next_action?.href;
  const review = workspace.review;

  return <div className="goal-workspace-page">
    <nav className="goal-breadcrumb" aria-label="目标路径"><a href="/workspace"><ArrowLeft size={16} aria-hidden="true" />全部目标</a></nav>
    <header className="goal-workspace-hero"><div><h2>{workspace.plan.title}</h2><p>计划 v{workspace.plan.version} · {fileStatusLabels[workspace.plan.file_status] ?? "状态待确认"}</p></div><span className={`goal-phase goal-phase-${workspace.phase.toLowerCase()}`}>{phaseLabels[workspace.phase]}</span></header>
    <nav className="goal-section-nav" aria-label="目标页面"><a aria-current="page" href={`/workspace/${encodeURIComponent(workspace.plan_document_id)}`}>概览</a><a href={planHref}>计划</a><a href={executionHref}>执行</a><a href="#goal-review">复盘</a><a className="goal-conversation-link" href={`/threads/${encodeURIComponent(workspace.thread_id)}`}><MessageSquare size={16} aria-hidden="true" />继续对话</a></nav>
    {workspace.next_action && nextHref && <section className="goal-next-action" aria-label="下一步行动"><div><span>下一步</span><h3>{workspace.next_action.label}</h3><p>{workspace.next_action.reason}</p></div><a className="button button-primary" href={nextHref}>现在去做<ArrowUpRight size={16} aria-hidden="true" /></a></section>}
    <dl className="goal-workspace-facts"><div><dt>计划版本</dt><dd>v{workspace.plan.version}</dd></div><div><dt>执行日期</dt><dd>{workspace.program ? `${workspace.program.start_date} 至 ${workspace.program.end_date}` : "尚未设置"}</dd></div><div><dt>关联研究</dt><dd>{workspace.research.length} 份</dd></div></dl>
    <section className="goal-review-summary" id="goal-review" aria-label="目标复盘" ref={reviewSection} tabIndex={-1}><h3>复盘与记录</h3>{review ? <><div className="goal-review-meta"><time dateTime={review.local_date}>{review.local_date}</time><span>{reviewStatusLabels[review.status] ?? "状态待确认"}</span></div>{review.summary && <p>{review.summary}</p>}{review.proposal && <p>{review.proposal.status === "PENDING" ? "有一项调整等待确认" : "已有计划调整记录"}</p>}<a href={executionHref}>查看行动与复盘<ArrowUpRight size={16} aria-hidden="true" /></a></> : <p>暂无可显示的复盘记录</p>}{workspace.growth.latest_summary && <div className="goal-latest-memory"><h4>最近经历</h4><p>{workspace.growth.latest_summary}</p><a href="/memory">查看记忆<ArrowUpRight size={16} aria-hidden="true" /></a></div>}</section>
    <section className="goal-lineage" aria-label="目标来源链"><h3>关联记录</h3><div className="goal-lineage-list">{workspace.sources.map(source => { const href = sourceHref(source, workspace); return href ? <a key={`${source.kind}-${source.id}`} href={href}><strong>{source.label}</strong><ArrowUpRight size={16} aria-hidden="true" /></a> : <span key={`${source.kind}-${source.id}`}><strong>{source.label}</strong></span>; })}</div></section>
  </div>;
}
