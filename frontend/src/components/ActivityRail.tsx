import { describeEvent } from "../trajectory";
import type { EventRecord, Run, Stats } from "../types";

interface ActivityRailProps {
  run: Run;
  events: EventRecord[];
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

function iterationValue(run: Run): string {
  const value = run.budget.react_iteration;
  return typeof value === "number" ? `${value} 次` : "未开始";
}

export default function ActivityRail({ run, events, stats, loading = false, onOpenTrajectory }: ActivityRailProps) {
  const activity = events.slice(-5).map(describeEvent);
  const planProgress = stats?.plan_total && stats.plan_completed !== null && stats.plan_completed !== undefined
    ? `${stats.plan_completed}/${stats.plan_total}`
    : "等待计划";

  return (
    <aside className="activity-rail" aria-label="当前运行进度">
      <div className="activity-rail-header">
        <div>
          <span className="eyebrow">LIVE RUN</span>
          <h3>现在发生什么</h3>
        </div>
        <span className={`state-pill state-${run.state.toLowerCase()}`}>{stateCopy[run.state] ?? run.state}</span>
      </div>
      <p className="activity-status" role="status" aria-live="polite">
        {stateCopy[run.state] ?? "运行状态已更新"}
      </p>
      <div className="activity-facts">
        <div><span>计划进度</span><strong>{planProgress}</strong></div>
        <div><span>已执行循环</span><strong>{iterationValue(run)}</strong></div>
        <div><span>已记录事件</span><strong>{events.length}</strong></div>
      </div>
      <div className="activity-list" aria-label="最近活动">
        {loading && <div className="activity-loading"><span className="loading-bar" />正在同步本地轨迹</div>}
        {!loading && activity.length === 0 && <p className="activity-empty">发送目标后，这里会出现实时进展。</p>}
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
