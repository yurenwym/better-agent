import type { GoalDailyReview } from "../types";

interface Props { review: GoalDailyReview; busy?:boolean; onRetry?:()=>void; }

const signalLabels:Record<string,string>={unfinished_actions:"还有未完成的行动",day_interrupted:"今天有任务跳过或延期",high_difficulty:"反馈难度较高",time_overrun:"实际用时明显超出预计"};

function EvidenceText({text}:{text:string|null}){
  if(!text)return null;
  return <>{text.split(/(\[\[source:[^\]]+\]\])/g).map((part,index)=>part.startsWith("[[source:")?<details className="daily-source-reference" key={index}><summary>依据：执行记录</summary><code>{part.slice(9,-2)}</code></details>:part)}</>;
}

export default function DailyReviewCard({review,busy=false,onRetry}:Props){
  const date=<time dateTime={review.local_date}>{review.local_date}</time>;
  const anchor={id:`daily-review-${review.id}`,tabIndex:-1};
  if(review.evidence_stale)return <section {...anchor} className="daily-review-card" aria-busy={busy} role="status">{date}<h4>执行记录已更新</h4><p>之前的复盘依据已过期。</p>{review.summary&&<blockquote><EvidenceText text={review.summary}/></blockquote>}{onRetry&&!["QUEUED","RUNNING"].includes(review.status)&&<button className="button button-secondary" disabled={busy} onClick={onRetry} type="button">重新复盘</button>}</section>;
  if(review.status==="QUEUED"||review.status==="RUNNING")return <section className="daily-review-card daily-review-pending" role="status"><span className="review-pulse" aria-hidden="true"/><div><span className="eyebrow">每日复盘 · {date}</span><h4>Agent 正在复盘今天的执行</h4><p>完成情况已保存。复盘结束后会在这里给出简短总结。</p></div></section>;
  if(review.status==="FAILED")return <section className="daily-review-card" aria-busy={busy} role="alert"><div><span className="eyebrow">每日复盘 · {date}</span><h4>今天的记录已保存</h4><p>复盘生成失败，不影响计划和完成进度。你可以直接重新复盘。</p>{onRetry&&<button className="button button-secondary" disabled={busy} onClick={onRetry} type="button">{busy?"正在重新复盘…":"重新复盘"}</button>}</div></section>;
  const adjustmentPending=["PENDING","RUNNING"].includes(review.adjustment_status??"");
  return <section {...anchor} className="daily-review-card" aria-busy={busy}>
    <div className="daily-review-heading"><div><span className="eyebrow">每日复盘 · {date} · 已完成</span><h4>今天的复盘</h4></div>{review.needs_adjustment&&review.adjustment_status!=="NO_CHANGE"&&<span className="review-adjustment-badge">建议调整</span>}</div>
    <div className="daily-review-summary"><EvidenceText text={review.summary}/></div><div><EvidenceText text={review.encouragement}/></div>
    {review.signals.length>0&&<ul className="review-signals">{review.signals.map(signal=><li key={signal}>{signalLabels[signal]??signal}</li>)}</ul>}
    {review.needs_adjustment&&review.adjustment_reason&&<div className="daily-review-reason"><strong>为什么建议调整</strong><EvidenceText text={review.adjustment_reason}/></div>}
    {adjustmentPending&&<p role="status">复盘已保存，正在准备调整建议。</p>}
    {review.adjustment_status==="NO_CHANGE"&&<p role="status">当前无需调整后续安排。</p>}
    {review.adjustment_status==="FAILED"&&<div role="alert"><p>复盘已保存，调整建议暂未生成。现有计划不变。</p>{onRetry&&<button className="button button-secondary" type="button" disabled={busy} onClick={onRetry}>{busy?"正在重试…":"重试调整建议"}</button>}</div>}
  </section>;
}
