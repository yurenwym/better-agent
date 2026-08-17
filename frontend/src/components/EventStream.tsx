import { useMemo, useState } from "react";
import type { EventRecord } from "../types";

interface EventStreamProps {
  events: EventRecord[];
}

const lanes = ["all", "runtime", "model", "tool", "user"] as const;

function laneFor(event: EventRecord): string {
  if (event.actor === "model" || event.type.startsWith("model.")) return "model";
  if (event.actor === "tool" || event.type.startsWith("tool.")) return "tool";
  if (event.actor === "user") return "user";
  return "runtime";
}

export default function EventStream({ events }: EventStreamProps) {
  const [filter, setFilter] = useState<(typeof lanes)[number]>("all");
  const visible = useMemo(
    () => events.filter((event) => filter === "all" || laneFor(event) === filter),
    [events, filter],
  );

  return (
    <section className="event-panel" aria-label="运行事件">
      <div className="panel-toolbar">
        <div>
          <span className="eyebrow">APPEND-ONLY LOG</span>
          <h3>事件泳道</h3>
        </div>
        <label className="select-label">
          <span>筛选</span>
          <select aria-label="轨迹筛选" value={filter} onChange={(event) => setFilter(event.target.value as (typeof lanes)[number])}>
            <option value="all">全部</option>
            <option value="runtime">运行时</option>
            <option value="model">模型</option>
            <option value="tool">工具</option>
            <option value="user">用户</option>
          </select>
        </label>
      </div>
      {visible.length === 0 ? (
        <p className="empty-state">暂无已提交事件。</p>
      ) : (
        <div className="event-list">
          {visible.map((event) => (
            <article className={`event-row lane-${laneFor(event)}`} key={event.event_id}>
              <div className="event-marker" aria-hidden="true" />
              <div className="event-main">
                <div className="event-meta">
                  <span className="event-seq">#{event.seq}</span>
                  <strong>{event.type}</strong>
                  <span>{event.actor}</span>
                  <time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
                </div>
                <details>
                  <summary>展开事件数据</summary>
                  <pre>{JSON.stringify(event.data, null, 2)}</pre>
                </details>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
