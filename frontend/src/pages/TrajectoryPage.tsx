import EventStream from "../components/EventStream";
import StatsBar from "../components/StatsBar";
import { useEffect, useState } from "react";
import { cancelAgentRun, getAgentArtifacts, getAgentRun, getAgentTasks } from "../api";
import ExpertRunCard from "../components/ExpertRunCard";
import { useTraceTelemetry } from "../hooks/useTraceTelemetry";
import { useRunTelemetry } from "../hooks/useRunTelemetry";
import { describeTraceGaps } from "../trajectory";
import type { AgentArtifact, AgentRun, AgentTask, Run, TraceScope } from "../types";

interface TrajectoryPageProps {
  run: Run | null;
  threadId?: string | null;
  expertRun?: AgentRun | null;
  csrfToken?: string;
  onExpertRun?: (run: AgentRun) => void;
}

function traceScope(threadId: string | null, expertRun: AgentRun | null): TraceScope | null {
  if (threadId) return { kind: "thread", id: threadId };
  if (expertRun) return { kind: "task", taskKind: "expert", id: expertRun.id };
  return null;
}

export default function TrajectoryPage({ run, threadId = null, expertRun = null, csrfToken = "", onExpertRun }: TrajectoryPageProps) {
  const scope = traceScope(threadId, expertRun);
  const trace = useTraceTelemetry(scope);
  const telemetry = useRunTelemetry(scope ? null : run?.id ?? null);
  const [expertTasks, setExpertTasks] = useState<AgentTask[]>([]);
  const [expertArtifacts, setExpertArtifacts] = useState<AgentArtifact[]>([]);
  const [expertBusy, setExpertBusy] = useState(false);
  useEffect(() => {
    if (!expertRun) { setExpertTasks([]); setExpertArtifacts([]); return; }
    let active = true;
    const refresh = () => Promise.all([getAgentRun(expertRun.id), getAgentTasks(expertRun.id), getAgentArtifacts(expertRun.id)])
      .then(([nextRun, tasks, artifacts]) => {
        if (!active) return;
        onExpertRun?.(nextRun); setExpertTasks(tasks.tasks); setExpertArtifacts(artifacts.artifacts);
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

  if (scope) {
    const notices = describeTraceGaps({ completeness: trace.completeness, legacyScanTruncated: trace.legacyScanTruncated });
    return (
      <div className="page-stack">
        <section className="hero-panel trajectory-hero">
          <div>
            <span className="eyebrow">统一轨迹</span>
            <h2>看懂这条链路发生了什么</h2>
            <p>对话、执行和专家事件来自同一次轨迹查询。缺失、截断和尚未接入的来源会单独标出。</p>
          </div>
          <button className="button button-secondary" type="button" onClick={trace.refresh}>重新读取</button>
        </section>
        <section className="stats-grid trajectory-thread-stats" aria-label="统一轨迹统计">
          <div className="stat-cell"><span className="stat-label">已读取事件</span><strong className="stat-value">{trace.events.length}</strong></div>
          <div className="stat-cell"><span className="stat-label">完整程度</span><strong className="stat-value">{trace.completeness?.status === "exact" ? "完整" : "部分"}</strong></div>
          <div className="stat-cell"><span className="stat-label">继续读取</span><strong className="stat-value">{trace.cursor ? "未结束" : "已结束"}</strong></div>
        </section>
        {notices.length > 0 && <div className="timeline-empty" role="status">{notices.map((line) => <p key={line}>{line}</p>)}</div>}
        {trace.error && <p className="error-message" role="alert">{trace.error}</p>}
        {trace.cursor && <button className="button button-secondary" type="button" onClick={trace.loadMore}>继续读取</button>}
        {expertRun && <ExpertRunCard run={expertRun} tasks={expertTasks} artifacts={expertArtifacts} busy={expertBusy} onCancel={() => void cancelExpert()} />}
        <EventStream events={[]} mode="trace" traceEvents={trace.events} loading={trace.loading} />
      </div>
    );
  }

  if (!run) return null;
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
      {telemetry.error && <p className="error-message" role="alert">{telemetry.error}</p>}
      <EventStream events={telemetry.events} />
    </div>
  );
}
