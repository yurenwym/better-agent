import { useEffect, useState } from "react";
import { getGoalWorkspace } from "../api";
import type { GoalWorkspace } from "../types";

interface Props { resourceId?: string | null; }
const phaseLabels: Record<GoalWorkspace["phase"], string> = {
  DISCOVERING: "探索中", PLANNING: "计划中", READY_TO_START: "准备开始", EXECUTING: "执行中",
  REVIEWING: "每日复盘", ADJUSTING: "等待调整确认", COMPLETED: "已完成", PAUSED: "已暂停", CANCELLED: "已取消",
};

export default function GoalWorkspacePage({ resourceId }: Props) {
  const [workspace, setWorkspace] = useState<GoalWorkspace | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true; setError("");
    if (!resourceId) { setWorkspace(null); return () => { active = false; }; }
    void getGoalWorkspace(resourceId).then(value => { if (active) setWorkspace(value); }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : "工作区加载失败"); });
    return () => { active = false; };
  }, [resourceId]);
  if (!resourceId) return <section className="workspace-overview-empty"><h2>选择一个目标工作区</h2><p>从计划、Today 或研究页面进入后，这里会集中显示当前阶段和下一步。</p></section>;
  if (!workspace) return <section className="workspace-overview-empty" aria-busy="true"><p role="status">正在整理目标进展…</p>{error && <p className="error-message" role="alert">{error}</p>}</section>;
  return <div className="goal-workspace-page">
    <section className="goal-workspace-hero"><div><span className="eyebrow">GOAL WORKSPACE</span><h2>{workspace.plan.title}</h2><p>所有进展都来自已有的计划、执行、复盘和成长记录。</p></div><span className={`goal-phase goal-phase-${workspace.phase.toLowerCase()}`}>{phaseLabels[workspace.phase]}</span></section>
    {workspace.next_action && <section className="goal-next-action" aria-label="下一步行动"><div><span className="eyebrow">NEXT ACTION</span><h3>{workspace.next_action.label}</h3><p>{workspace.next_action.reason}</p></div><a className="button button-primary" href={workspace.next_action.href}>现在去做</a></section>}
    <section className="goal-workspace-grid"><article className="goal-workspace-card"><span className="eyebrow">PLAN</span><h3>{workspace.plan.title}</h3><p>当前版本 v{workspace.plan.version} · {workspace.plan.file_status}</p><a href={`/plans/${workspace.plan.id}`}>查看计划</a></article><article className="goal-workspace-card"><span className="eyebrow">EXECUTION</span><h3>{workspace.program ? phaseLabels[workspace.phase] : "尚未开始执行"}</h3><p>{workspace.program ? `${workspace.program.start_date} 至 ${workspace.program.end_date}` : "生成执行预览后，Today 会出现每日行动。"}</p><a href="/today">打开 Today</a></article><article className="goal-workspace-card"><span className="eyebrow">GROWTH</span><h3>{workspace.growth.episode_count} 条成长经历</h3><p>{workspace.growth.latest_summary ?? "完成目标后，这里会记录一次可追溯的成长总结。"}</p><a href="/memory">查看记忆</a></article></section>
    <section className="goal-lineage" aria-label="目标来源链"><div className="section-heading"><span className="eyebrow">LINEAGE</span><h3>进展从哪里来</h3></div><div className="goal-lineage-list">{workspace.sources.map(source => <span key={`${source.kind}-${source.id}`}><strong>{source.label}</strong><small>{source.kind}</small></span>)}</div></section>
  </div>;
}
