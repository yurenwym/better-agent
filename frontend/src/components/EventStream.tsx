import { useMemo, useState } from "react";
import { describeEvent, describeThreadEvent, groupEvents, groupThreadEvents } from "../trajectory";
import type { TrajectoryStage } from "../trajectory";
import type { EventRecord, ThreadEvent } from "../types";

interface EventStreamProps {
  events: EventRecord[];
  threadEvents?: ThreadEvent[];
  mode?: "run" | "thread";
  loading?: boolean;
}

type Filter = "all" | TrajectoryStage;

const filters: Array<{ id: Filter; label: string }> = [
  { id: "all", label: "全部阶段" },
  { id: "interaction", label: "交互" },
  { id: "context", label: "上下文" },
  { id: "model", label: "模型" },
  { id: "plan", label: "计划" },
  { id: "react", label: "ReAct" },
  { id: "tool", label: "工具与审批" },
  { id: "memory", label: "长期记忆" },
  { id: "state", label: "状态" },
];

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export default function EventStream({ events, threadEvents = [], mode = "run", loading = false }: EventStreamProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const sourceEvents = mode === "thread" ? threadEvents : events;
  const visible = useMemo(() => (mode === "thread" ? threadEvents.map(describeThreadEvent) : events.map(describeEvent))
    .filter((item) => {
      const searchable = `${item.stageLabel} ${item.title} ${item.detail} ${item.event.type}`.toLowerCase();
      return (filter === "all" || item.stage === filter) && (!query.trim() || searchable.includes(query.trim().toLowerCase()));
    }), [events, filter, mode, query, threadEvents]);
  const groups = useMemo(() => {
    const visibleIds = new Set(visible.map((item) => item.event.event_id));
    return mode === "thread"
      ? groupThreadEvents(threadEvents.filter((event) => visibleIds.has(event.event_id)))
      : groupEvents(events.filter((event) => visibleIds.has(event.event_id)));
  }, [events, mode, threadEvents, visible]);
  const title = mode === "thread" ? "对话时间线" : "运行时间线";
  const eventCount = sourceEvents.length;

  return (
    <section className="event-panel" aria-label={title}>
      <div className="panel-toolbar timeline-toolbar">
        <div>
          <span className="eyebrow">{mode === "thread" ? "对话轨迹" : "可读轨迹"}</span>
          <h3>{title}</h3>
          <p className="panel-caption">{eventCount} 条事件，按发生顺序实时更新</p>
        </div>
        <div className="timeline-controls">
          <label className="select-label">
            <span>阶段</span>
            <select aria-label="轨迹阶段筛选" value={filter} onChange={(event) => setFilter(event.target.value as Filter)}>
              {filters.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
          </label>
          <label className="select-label timeline-search">
            <span>查找进展</span>
            <input aria-label="搜索轨迹" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索阶段或摘要" />
          </label>
        </div>
      </div>
      {loading ? (
        <div className="timeline-empty" role="status">
          <span className="timeline-empty-mark" aria-hidden="true">…</span>
          <strong>正在加载轨迹</strong>
          <p>正在同步当前线程的事件记录。</p>
        </div>
      ) : visible.length === 0 ? (
        <div className="timeline-empty" role="status">
          <span className="timeline-empty-mark" aria-hidden="true">01</span>
          <strong>{eventCount === 0 ? "等待第一条轨迹事件" : "没有匹配的轨迹事件"}</strong>
          <p>{eventCount === 0 ? "发送消息后，交互、模型和计划进展会按顺序出现在这里。" : "尝试切换阶段或清空搜索词，继续查看完整运行轨迹。"}</p>
        </div>
      ) : (
        <div className="timeline-groups">
          {groups.map((group) => (
            <section className="timeline-group" key={group.id}>
              <div className="timeline-group-heading"><span>{group.label}</span><span>{group.events.length} 个节点</span></div>
              <div className="timeline-list">
                {group.events.map((item) => (
                  <article className={`timeline-row tone-${item.tone}`} key={item.event.event_id}>
                    <div className="timeline-spine" aria-hidden="true"><span className="timeline-marker" /></div>
                    <div className="timeline-content">
                      <div className="timeline-meta"><span className="timeline-stage">{item.stageLabel}</span><span>#{item.seq}</span><time dateTime={item.occurredAt}>{formatTime(item.occurredAt)}</time></div>
                      <h4>{item.title}</h4>
                      <p>{item.detail}</p>
                      {!item.event.type.startsWith("model.response") && (
                        <details className="raw-event">
                          <summary>查看原始事件</summary>
                          <div className="raw-event-label">{item.event.type} · {item.event.actor}</div>
                          <pre>{JSON.stringify(item.event.data, null, 2)}</pre>
                        </details>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </section>
  );
}
