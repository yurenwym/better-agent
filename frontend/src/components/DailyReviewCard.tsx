import type { GoalDailyReview } from "../types";

interface Props { review: GoalDailyReview; }

const signalLabels:Record<string,string>={day_interrupted:"今天有任务跳过或延期",high_difficulty:"反馈难度较高",time_overrun:"实际用时明显超出预计"};

export default function DailyReviewCard({review}:Props){
  if(review.status==="QUEUED"||review.status==="RUNNING")return <section className="daily-review-card daily-review-pending" role="status"><span className="review-pulse" aria-hidden="true"/><div><span className="eyebrow">DAILY REVIEW</span><h4>Agent 正在复盘今天的执行</h4><p>完成情况已保存。复盘结束后会在这里给出简短总结。</p></div></section>;
  if(review.status==="FAILED")return <section className="daily-review-card"><div><span className="eyebrow">DAILY REVIEW</span><h4>今天的记录已保存</h4><p>暂时无法生成复盘，不影响计划和完成进度。</p></div></section>;
  return <section className="daily-review-card"><div className="daily-review-heading"><div><span className="eyebrow">DAILY REVIEW · COMPLETED</span><h4>今天的复盘</h4></div>{review.needs_adjustment&&<span className="review-adjustment-badge">建议调整</span>}</div><p className="daily-review-summary">{review.summary}</p><p>{review.encouragement}</p>{review.signals.length>0&&<ul className="review-signals">{review.signals.map(signal=><li key={signal}>{signalLabels[signal]??signal}</li>)}</ul>}{review.needs_adjustment&&review.adjustment_reason&&<p className="daily-review-reason"><strong>为什么建议调整</strong>{review.adjustment_reason}</p>}</section>;
}
