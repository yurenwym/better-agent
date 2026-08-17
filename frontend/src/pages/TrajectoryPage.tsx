import { useEffect, useState } from "react";
import { getEvents, getStats, subscribeToEvents } from "../api";
import EventStream from "../components/EventStream";
import StatsBar from "../components/StatsBar";
import type { EventRecord, Run, Stats } from "../types";

interface TrajectoryPageProps {
  run: Run | null;
}

export default function TrajectoryPage({ run }: TrajectoryPageProps) {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!run) { setEvents([]); setStats(null); return; }
    let active = true;
    Promise.all([getEvents(run.id), getStats(run.id)]).then(([eventResult, statsResult]) => {
      if (!active) return;
      setEvents(eventResult.events);
      setStats(statsResult);
    }).catch((caught) => setError(caught instanceof Error ? caught.message : "轨迹加载失败"));
    const close = subscribeToEvents(run.id, 0, (event) => {
      if (!active) return;
      setEvents((current) => current.some((item) => item.seq === event.seq) ? current : [...current, event].sort((left, right) => left.seq - right.seq));
    });
    return () => { active = false; close(); };
  }, [run]);

  if (!run) return <section className="empty-panel"><span className="eyebrow">TRACE / OBSERVE</span><h2>运行轨迹</h2><p>创建目标并发送消息后，这里会显示完整的 append-only 事件。</p></section>;

  return <div className="page-stack"><section className="hero-panel"><div><span className="eyebrow">TRACE / OBSERVE</span><h2>运行轨迹</h2><p>事件来自已提交的 SQLite 日志，客户端按 seq 去重。</p></div><a className="button button-quiet" href={`/api/runs/${run.id}/export?mode=redacted`}>导出脱敏 JSONL</a></section><StatsBar stats={stats} run={run} />{error && <p className="error-message" role="alert">{error}</p>}<EventStream events={events} /></div>;
}
