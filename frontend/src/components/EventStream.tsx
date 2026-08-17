import { useMemo, useState } from "react";
import type { EventRecord } from "../types";

interface EventStreamProps {
  events: EventRecord[];
}

const lanes = ["all", "input", "context", "model", "tools", "state", "memory"] as const;

function laneFor(event: EventRecord): string {
  if (event.type.startsWith("interaction.") || event.actor === "user") return "input";
  if (event.type.startsWith("context.")) return "context";
  if (event.actor === "model" || event.type.startsWith("model.")) return "model";
  if (event.actor === "tool" || event.type.startsWith("tool.") || event.type.startsWith("approval.")) return "tools";
  if (event.type.startsWith("memory.")) return "memory";
  return "state";
}

export default function EventStream({ events }: EventStreamProps) {
  const [filter, setFilter] = useState<(typeof lanes)[number]>("all");
  const [query, setQuery] = useState("");
  const visible = useMemo(
    () => events.filter((event) => {
      const searchable = JSON.stringify(event).toLowerCase();
      return (filter === "all" || laneFor(event) === filter) && (!query.trim() || searchable.includes(query.trim().toLowerCase()));
    }),
    [events, filter, query],
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
            <option value="input">Input</option>
            <option value="context">Context</option>
            <option value="model">模型</option>
            <option value="tools">Tools</option>
            <option value="state">State</option>
            <option value="memory">Memory</option>
          </select>
        </label>
        <label className="select-label">
          <span>搜索</span>
          <input aria-label="搜索事件" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="type / seq / actor" />
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
