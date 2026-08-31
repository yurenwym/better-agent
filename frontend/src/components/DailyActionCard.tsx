import { useState } from "react";
import type { GoalAction } from "../types";

interface Props {
  action: GoalAction; busy: boolean; overdue?: boolean;
  onComplete: (feedback: { actual_minutes?: number; difficulty?: number }) => void; onSkip: () => void; onDefer: (date: string) => void;
  onFeedback: (difficulty: number) => void;
  onHelp?: () => void;
}

export default function DailyActionCard({ action, busy, overdue = false, onComplete, onSkip, onDefer, onFeedback, onHelp }: Props) {
  const [deferDate, setDeferDate] = useState(action.scheduled_date);
  const [actualMinutes,setActualMinutes]=useState("");
  const [completionDifficulty,setCompletionDifficulty]=useState("");
  return (
    <article className={`daily-action-card${overdue ? " daily-action-overdue" : ""}`} aria-busy={busy}>
      <div className="daily-action-heading">
        <div><span className="eyebrow">{overdue ? "已逾期" : action.required ? "必做" : "可选"}</span><h4>{action.title}</h4></div>
        <span className="daily-action-minutes">{action.estimated_minutes} 分钟</span>
      </div>
      {action.description && <p>{action.description}</p>}
      <p className="daily-action-criteria"><strong>完成标准</strong>{action.completion_criteria}</p>
      <div className="daily-action-controls">
        <label><span>实际分钟（可选）</span><input aria-label={`${action.title} 实际分钟`} disabled={busy} inputMode="numeric" min="0" max="1440" type="number" value={actualMinutes} onChange={event=>setActualMinutes(event.target.value)}/></label>
        <label><span>完成难度（可选）</span><select aria-label={`${action.title} 完成难度`} disabled={busy} value={completionDifficulty} onChange={event=>setCompletionDifficulty(event.target.value)}><option value="">不填写</option><option value="1">1 · 轻松</option><option value="2">2</option><option value="3">3 · 合适</option><option value="4">4</option><option value="5">5 · 困难</option></select></label>
        <button className="button button-primary" disabled={busy} type="button" onClick={()=>onComplete({...(actualMinutes?{actual_minutes:Number(actualMinutes)}:{}),...(completionDifficulty?{difficulty:Number(completionDifficulty)}:{})})}>完成</button>
        <button className="button button-secondary" disabled={busy} type="button" onClick={onSkip}>跳过</button>
        <label><span>延期到</span><input aria-label={`${action.title} 延期日期`} disabled={busy} min={action.scheduled_date} type="date" value={deferDate} onChange={(event)=>setDeferDate(event.target.value)} /></label>
        <button className="button button-quiet" disabled={busy || !deferDate} type="button" onClick={()=>onDefer(deferDate)}>确认延期</button>
        {onHelp && <button className="button button-quiet" disabled={busy} type="button" onClick={onHelp}>让 Agent 帮我</button>}
      </div>
      <fieldset className="daily-feedback"><legend>难度反馈（可选）</legend>{[1,2,3,4,5].map(value=><button aria-label={`难度 ${value}`} className="button button-quiet" disabled={busy} key={value} type="button" onClick={()=>onFeedback(value)}>{value}</button>)}</fieldset>
    </article>
  );
}
