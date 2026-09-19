import { useMemo, useState } from "react";
import {
  describeEvent, describeThreadEvent, formatDuration, groupEvents,
  groupThreadEvents, keyTrajectoryEvents,
} from "../trajectory";
import type { TrajectoryStage, TrajectoryView } from "../trajectory";
import type { EventRecord, ThreadEvent } from "../types";

interface EventStreamProps {
  events: EventRecord[];
  threadEvents?: ThreadEvent[];
  mode?: "run" | "thread";
  loading?: boolean;
}

type Filter = "all" | TrajectoryStage;

const filters: Array<{ id: Filter; label: string }> = [
  { id: "all", label: "全部阶段" }, { id: "interaction", label: "交互" },
  { id: "context", label: "上下文" }, { id: "model", label: "模型" },
  { id: "plan", label: "计划" }, { id: "react", label: "ReAct" },
  { id: "tool", label: "工具与审批" }, { id: "memory", label: "长期记忆" },
  { id: "state", label: "状态" },
];

const metricLabels: Array<[string, string]> = [
  ["queue_wait_ms", "排队"], ["context_ms", "上下文"],
  ["model_ttft_ms", "模型首字"], ["stream_ms", "输出"], ["total_ms", "总耗时"],
];

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString("zh-CN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function metricEvents(events: ThreadEvent[]): ThreadEvent[] {
  const latest = new Map<string, ThreadEvent>();
  for (const event of events) {
    if (event.type === "turn.metrics.updated") latest.set(event.turn_id, event);
  }
  return [...latest.values()].sort((left, right) => left.seq - right.seq);
}

export default function EventStream({ events, threadEvents = [], mode = "run", loading = false }: EventStreamProps) {
  const [view, setView] = useState<TrajectoryView>("summary");
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const sourceEvents = mode === "thread" ? threadEvents : events;
  const viewEvents = useMemo(() => {
    if (mode === "thread") {
      return view === "summary" ? keyTrajectoryEvents(threadEvents, "thread") : threadEvents;
    }
    return view === "summary" ? keyTrajectoryEvents(events, "run") : events;
  }, [events, mode, threadEvents, view]);
  const visible = useMemo(() => (
    mode === "thread"
      ? (viewEvents as ThreadEvent[]).map(describeThreadEvent)
      : (viewEvents as EventRecord[]).map(describeEvent)
  ).filter((item) => {
    const searchable = `${item.stageLabel} ${item.title} ${item.detail} ${item.event.type}`.toLowerCase();
    return (filter === "all" || item.stage === filter)
      && (!query.trim() || searchable.includes(query.trim().toLowerCase()));
  }), [filter, mode, query, viewEvents]);
  const groups = useMemo(() => {
    const visibleIds = new Set(visible.map((item) => item.event.event_id));
    return mode === "thread"
      ? groupThreadEvents((viewEvents as ThreadEvent[]).filter((event) => visibleIds.has(event.event_id)))
      : groupEvents((viewEvents as EventRecord[]).filter((event) => visibleIds.has(event.event_id)));
  }, [mode, viewEvents, visible]);
  const metrics = mode === "thread" ? metricEvents(threadEvents) : [];
  const title = mode === "thread" ? "对话时间线" : "运行时间线";

  return (
    <section className="event-panel" aria-label={title}>
      <div className="panel-toolbar timeline-toolbar">
        <div>
          <span className="eyebrow">{mode === "thread" ? "对话轨迹" : "可读轨迹"}</span>
          <h3>{title}</h3>
          <p className="panel-caption">{view === "summary"
            ? `${visible.length} 个关键节点，原始事件仍保留在调试视图`
            : `${sourceEvents.length} 条原始事件`}</p>
        </div>
        <div className="timeline-controls">
          <div className="trajectory-view-switch" role="group" aria-label="轨迹显示模式">
            <button aria-pressed={view === "summary"} className={view === "summary" ? "active" : ""} type="button" onClick={() => setView("summary")}>摘要</button>
            <button aria-pressed={view === "debug"} className={view === "debug" ? "active" : ""} type="button" onClick={() => setView("debug")}>调试</button>
          </div>
          <label className="select-label"><span>阶段</span>
            <select aria-label="轨迹阶段筛选" value={filter} onChange={(event) => setFilter(event.target.value as Filter)}>
              {filters.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
          </label>
          <label className="select-label timeline-search"><span>查找事件</span>
            <input aria-label="搜索轨迹" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="事件类型或内容" />
          </label>
        </div>
      </div>

      {mode === "thread" && metrics.length > 0 && <div className="turn-metrics-list" aria-label="每轮耗时">
        {metrics.map((event, index) => <div className="turn-metrics" key={event.event_id}>
          <strong>第 {index + 1} 轮</strong>
          {metricLabels.map(([key, label]) => <span key={key}>{label}<b>{formatDuration(event.data[key])}</b></span>)}
        </div>)}
      </div>}

      {loading ? <div className="timeline-empty" role="status"><strong>正在加载轨迹</strong><p>正在同步当前线程的事件记录。</p></div>
        : visible.length === 0 ? <div className="timeline-empty" role="status">
          <strong>{sourceEvents.length === 0 ? "等待第一条轨迹事件" : "没有匹配的关键事件"}</strong>
          <p>{sourceEvents.length === 0 ? "发送消息后，交互、模型和计划进展会按顺序出现在这里。" : "可切换到调试视图查看全部原始事件。"}</p>
        </div>
          : <div className="timeline-groups">{groups.map((group) => <section className="timeline-group" key={group.id}>
            <div className="timeline-group-heading"><span>{group.label}</span><span>{group.events.length} 个节点</span></div>
            <div className="timeline-list">{group.events.map((item) => <article className={`timeline-row tone-${item.tone}`} key={item.event.event_id}>
              <div className="timeline-spine" aria-hidden="true"><span className="timeline-marker" /></div>
              <div className="timeline-content">
                <div className="timeline-meta"><span className="timeline-stage">{item.stageLabel}</span>{view === "debug" && <span>#{item.seq}</span>}<time dateTime={item.occurredAt}>{formatTime(item.occurredAt)}</time></div>
                <h4>{item.title}</h4><p>{item.detail}</p>
                {view === "debug" && <details className="raw-event"><summary>查看原始事件</summary>
                  <div className="raw-event-label">{item.event.type} · {item.event.actor}</div>
                  <pre>{JSON.stringify(item.event.data, null, 2)}</pre>
                </details>}
              </div>
            </article>)}</div>
          </section>)}</div>}
    </section>
  );
}
