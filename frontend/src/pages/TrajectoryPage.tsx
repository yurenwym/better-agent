import EventStream from "../components/EventStream";
import StatsBar from "../components/StatsBar";
import { useRunTelemetry } from "../hooks/useRunTelemetry";
import type { Run } from "../types";

interface TrajectoryPageProps {
  run: Run | null;
}

export default function TrajectoryPage({ run }: TrajectoryPageProps) {
  const telemetry = useRunTelemetry(run?.id ?? null);

  if (!run) {
    return (
      <section className="empty-panel">
        <span className="eyebrow">TRACE / OBSERVE</span>
        <h2>运行轨迹</h2>
        <p>创建目标并发送消息后，这里会把运行时的每个阶段翻译成可读的时间线。</p>
      </section>
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
