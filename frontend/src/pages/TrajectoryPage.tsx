import EventStream from "../components/EventStream";
import StatsBar from "../components/StatsBar";
import { useEffect, useState } from "react";
import { cancelAgentRun, getAgentArtifacts, getAgentEvents, getAgentRun, getAgentTasks } from "../api";
import ExpertRunCard from "../components/ExpertRunCard";
import { useThreadTelemetry } from "../hooks/useThreadTelemetry";
import { useRunTelemetry } from "../hooks/useRunTelemetry";
import type { AgentArtifact, AgentEvent, AgentRun, AgentTask, Run } from "../types";

interface TrajectoryPageProps {
  run: Run | null;
  threadId?: string | null;
  expertRun?: AgentRun | null;
  csrfToken?: string;
  onExpertRun?: (run: AgentRun) => void;
}

const expertMilestones: Record<string, string> = {
  "agent.run.created": "专家协同已创建", "agent.task.created": "专家子任务已分派", "agent.task.started": "专家开始处理",
  "agent.artifact.committed": "专家成果已提交", "agent.join.ready": "专家结果已汇合", "agent.task.failed": "专家任务未完成",
  "agent.task.cancelled": "专家任务已取消", "agent.run.completed": "专家协同已完成",
};

export default function TrajectoryPage({ run, threadId = null, expertRun = null, csrfToken = "", onExpertRun }: TrajectoryPageProps) {
  const telemetry = useRunTelemetry(run?.id ?? null);
  const threadTelemetry = useThreadTelemetry(run ? null : threadId);
  const [expertTasks, setExpertTasks] = useState<AgentTask[]>([]);
  const [expertArtifacts, setExpertArtifacts] = useState<AgentArtifact[]>([]);
  const [expertEvents, setExpertEvents] = useState<AgentEvent[]>([]);
  const [expertBusy, setExpertBusy] = useState(false);
  useEffect(() => {
    if (!expertRun) { setExpertTasks([]); setExpertArtifacts([]); setExpertEvents([]); return; }
    let active = true;
    const refresh = () => Promise.all([getAgentRun(expertRun.id), getAgentTasks(expertRun.id), getAgentArtifacts(expertRun.id), getAgentEvents(expertRun.id)])
      .then(([nextRun, tasks, artifacts, events]) => {
        if (!active) return;
        onExpertRun?.(nextRun); setExpertTasks(tasks.tasks); setExpertArtifacts(artifacts.artifacts); setExpertEvents(events.events);
      }).catch(() => undefined);
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => { active = false; window.clearInterval(timer); };
  }, [expertRun?.id, onExpertRun]);
  async function cancelExpert() {
    if (!expertRun) return;
    setExpertBusy(true);
    try { onExpertRun?.(await cancelAgentRun(expertRun.id, csrfToken)); }
    finally { setExpertBusy(false); }
  }

  if (!run && !threadId && !expertRun) {
    return (
      <section className="empty-panel">
        <span className="eyebrow">执行轨迹</span>
        <h2>运行轨迹</h2>
        <p>创建目标并发送消息后，这里会把运行时的每个阶段翻译成可读的时间线。</p>
      </section>
    );
  }

  if (!run) {
    const currentTurn = threadTelemetry.thread?.turns?.find((turn) => turn.id === threadTelemetry.thread?.active_turn_id)
      ?? threadTelemetry.thread?.turns?.at(-1);
    const threadState = currentTurn?.status ?? "等待同步";

    return (
      <div className="page-stack">
        <section className="hero-panel trajectory-hero">
          <div>
            <span className="eyebrow">对话轨迹</span>
            <h2>看懂这轮对话发生了什么</h2>
            <p>消息、模型路由、Ask 询问和回答结果都按 seq 进入同一条可读时间线。</p>
          </div>
          <span className="version-badge">{threadTelemetry.events.length} 条事件</span>
        </section>
        <section className="stats-grid trajectory-thread-stats" aria-label="对话轨迹统计">
          <div className="stat-cell"><span className="stat-label">对话轮次</span><strong className="stat-value">{threadTelemetry.thread?.turns?.length ?? 0}</strong></div>
          <div className="stat-cell"><span className="stat-label">当前状态</span><strong className="stat-value">{threadState}</strong></div>
          <div className="stat-cell"><span className="stat-label">已记录事件</span><strong className="stat-value">{threadTelemetry.events.length}</strong></div>
        </section>
        {threadTelemetry.error && <p className="error-message" role="alert">{threadTelemetry.error}</p>}
        {expertRun && <><ExpertRunCard run={expertRun} tasks={expertTasks} artifacts={expertArtifacts} busy={expertBusy} onCancel={() => void cancelExpert()} /><section className="expert-milestones" aria-label="专家里程碑"><div className="section-heading"><span className="eyebrow">专家里程碑</span><h3>专家协同里程碑</h3><p>仅展示任务事实和已提交成果，不展示模型内部推理。</p></div>{expertEvents.map((event) => <article key={event.event_id}><span>{event.seq}</span><div><strong>{expertMilestones[event.type] ?? "专家任务状态更新"}</strong><time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleString("zh-CN")}</time></div></article>)}</section></>}
        <EventStream events={[]} threadEvents={threadTelemetry.events} mode="thread" loading={threadTelemetry.loading} />
      </div>
    );
  }

  return (
    <div className="page-stack">
      <section className="hero-panel trajectory-hero">
        <div>
          <span className="eyebrow">执行轨迹</span>
          <h2>看懂每一步发生了什么</h2>
          <p>交互、上下文、模型、计划、工具和检查点都按 seq 进入同一条可读时间线。原始 JSON 只在需要审计时展开。</p>
        </div>
        <a className="button button-secondary" href={`/api/runs/${run.id}/export?mode=redacted`}>导出脱敏 JSONL</a>
      </section>
      <StatsBar stats={telemetry.stats} run={run} />
      {expertRun && <><ExpertRunCard run={expertRun} tasks={expertTasks} artifacts={expertArtifacts} busy={expertBusy} onCancel={() => void cancelExpert()} /><section className="expert-milestones" aria-label="专家里程碑"><div className="section-heading"><span className="eyebrow">专家里程碑</span><h3>专家协同里程碑</h3></div>{expertEvents.map((event) => <article key={event.event_id}><span>{event.seq}</span><div><strong>{expertMilestones[event.type] ?? "专家任务状态更新"}</strong><time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleString("zh-CN")}</time></div></article>)}</section></>}
      {telemetry.error && <p className="error-message" role="alert">{telemetry.error}</p>}
      <EventStream events={telemetry.events} />
    </div>
  );
}
