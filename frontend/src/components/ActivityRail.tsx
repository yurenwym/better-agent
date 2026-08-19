import { describeEvent, describeThreadEvent } from "../trajectory";
import type { EventRecord, Run, Stats, Thread, ThreadEvent, Turn } from "../types";

interface ActivityRailProps {
  run: Run | null;
  events: EventRecord[];
  thread?: Thread | null;
  threadEvents?: ThreadEvent[];
  stats: Stats | null;
  loading?: boolean;
  onOpenTrajectory: () => void;
}

const stateCopy: Record<string, string> = {
  RECEIVED: "已收到目标",
  CLARIFYING: "正在澄清目标",
  PLANNING: "正在生成计划",
  AWAITING_APPROVAL: "等待你的审批",
  EXECUTING: "正在执行计划",
  AWAITING_OUTCOME: "等待外部结果",
  REFLECTING: "正在复盘记忆",
  COMPLETED: "目标已完成",
  BLOCKED: "需要处理后继续",
  FAILED: "运行失败",
  CANCELLED: "运行已取消",
};

const threadStateCopy: Record<string, string> = {
  ACCEPTED: "已收到消息",
  ROUTING: "正在判断下一步",
  STREAMING: "正在生成回答",
  AWAITING_INPUT: "等待你的回答",
  COMPLETED: "本轮对话完成",
  AWAITING_DIRECTION: "等待你的选择",
  MATERIALIZING: "正在创建执行任务",
  FAILED: "本轮对话未完成",
  CANCELLED: "本轮对话已停止",
};

function iterationValue(run: Run): string {
  const value = run.budget.react_iteration;
  return typeof value === "number" ? `${value} 次` : "未开始";
}

export default function ActivityRail({ run, events, thread = null, threadEvents = [], stats, loading = false, onOpenTrajectory }: ActivityRailProps) {
  const currentTurn: Turn | undefined = thread?.turns?.find((turn) => turn.id === thread.active_turn_id)
    ?? thread?.turns?.at(-1);
  const threadStatus = currentTurn?.status ?? "ACCEPTED";
  const activity = run
    ? events.slice(-5).map(describeEvent)
    : threadEvents.slice(-5).map(describeThreadEvent);
  const eventCount = run ? events.length : threadEvents.length;
  const state = run?.state ?? threadStatus;
  const stateLabel = run ? (stateCopy[run.state] ?? run.state) : (threadStateCopy[threadStatus] ?? threadStatus);
  const planProgress = stats?.plan_total && stats.plan_completed !== null && stats.plan_completed !== undefined
    ? `${stats.plan_completed}/${stats.plan_total}`
    : "等待计划";

  return (
    <aside className="activity-rail" aria-label={run ? "当前运行进度" : "当前对话轨迹"}>
      <div className="activity-rail-header">
        <div>
          <span className="eyebrow">{run ? "LIVE RUN" : "LIVE THREAD"}</span>
          <h3>{run ? "现在发生什么" : "这轮对话发生了什么"}</h3>
        </div>
        <span className={`state-pill state-${state.toLowerCase()}`}>{stateLabel}</span>
      </div>
      <div className="activity-current" role="status" aria-live="polite">
        <div className="activity-current-meta"><span>当前阶段</span><span className={`activity-current-dot state-${state.toLowerCase()}`} aria-hidden="true" /></div>
        <strong>{stateLabel}</strong>
        <p>{run ? "轨迹会随着 Run 实时更新" : "线程事件会随着对话实时更新"}</p>
      </div>
      <div className="activity-facts">
        {run ? <>
          <div><span>计划进度</span><strong>{planProgress}</strong></div>
          <div><span>已执行循环</span><strong>{iterationValue(run)}</strong></div>
        </> : <>
          <div><span>当前轮次</span><strong>{thread?.turns?.length ?? 0}</strong></div>
          <div><span>回答方式</span><strong>{currentTurn?.policy ?? "判断中"}</strong></div>
        </>}
        <div><span>已记录事件</span><strong>{eventCount}</strong></div>
      </div>
      <div className="activity-list-heading"><strong>最近活动</strong><span>{eventCount} 条</span></div>
      <div className="activity-list" aria-label="最近活动">
        {loading && <div className="activity-loading"><span className="loading-bar" />正在同步本地轨迹</div>}
        {!loading && activity.length === 0 && <p className="activity-empty">发送消息后，这里会出现实时进展。</p>}
        {activity.map((item) => (
          <article className={`activity-item activity-${item.tone}`} key={item.event.event_id}>
            <span className="activity-dot" aria-hidden="true" />
            <div>
              <div className="activity-item-meta"><span>{item.stageLabel}</span><span>#{item.seq}</span></div>
              <strong>{item.title}</strong>
              <p>{item.detail}</p>
            </div>
          </article>
        ))}
      </div>
      <button className="button button-secondary activity-link" type="button" onClick={onOpenTrajectory}>查看完整轨迹</button>
    </aside>
  );
}
