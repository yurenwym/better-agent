import EventStream from "../components/EventStream";
import StatsBar from "../components/StatsBar";
import { useThreadTelemetry } from "../hooks/useThreadTelemetry";
import { useRunTelemetry } from "../hooks/useRunTelemetry";
import type { Run } from "../types";

interface TrajectoryPageProps {
  run: Run | null;
  threadId?: string | null;
}

export default function TrajectoryPage({ run, threadId = null }: TrajectoryPageProps) {
  const telemetry = useRunTelemetry(run?.id ?? null);
  const threadTelemetry = useThreadTelemetry(run ? null : threadId);

  if (!run && !threadId) {
    return (
      <section className="empty-panel">
        <span className="eyebrow">TRACE / OBSERVE</span>
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
            <span className="eyebrow">TRACE / CONVERSATION</span>
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
        <EventStream events={[]} threadEvents={threadTelemetry.events} mode="thread" loading={threadTelemetry.loading} />
      </div>
    );
  }

  return (
    <div className="page-stack">
      <section className="hero-panel trajectory-hero">
        <div>
          <span className="eyebrow">TRACE / OBSERVE</span>
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
