import { useState } from "react";
import { Check, CircleHelp, Pencil, Undo2 } from "lucide-react";
import type { GoalAction } from "../types";

interface Props {
  action: GoalAction; busy: boolean; overdue?: boolean;
  onComplete: (feedback: { actual_minutes?: number; difficulty?: number;note?:string;actual_date?:string }) => void; onSkip: () => void; onDefer: (date: string) => void;
  onProgress?: (feedback: Record<string,unknown>) => Promise<boolean>;
  onFeedback: (difficulty: number) => void;
  onNote?: (note: string) => void | Promise<boolean>;
  onHelp?: (content:string) => void | Promise<boolean>;
  localDate?:string;
  onReopen?:()=>void;
  onCorrection?:(feedback:Record<string,unknown>)=>Promise<boolean>;
}

export default function DailyActionCard({ action, busy, overdue = false, onComplete, onSkip, onDefer, onFeedback, onProgress, onNote, onHelp, localDate=action.scheduled_date, onReopen, onCorrection }: Props) {
  const nextDay=new Date(`${action.scheduled_date}T00:00:00Z`);nextDay.setUTCDate(nextDay.getUTCDate()+1);
  const minimumDeferDate=nextDay.toISOString().slice(0,10);
  const [deferDate, setDeferDate] = useState(localDate>minimumDeferDate?localDate:minimumDeferDate);
  const [form,setForm]=useState<"progress"|"help"|"correction"|null>(null);
  const [question,setQuestion]=useState("");
  const [recordDate,setRecordDate]=useState(localDate);
  const [saved,setSaved]=useState(false);
  const [clearMinutes,setClearMinutes]=useState(false);
  const [actualMinutes,setActualMinutes]=useState(action.time_entry?.actual_date===localDate&&action.time_entry.actual_minutes!==null?String(action.time_entry.actual_minutes):"");
  const [completionDifficulty,setCompletionDifficulty]=useState("");
  const [note,setNote]=useState("");
  const [completedWork,setCompletedWork]=useState(action.progress?.completed_work??"");
  const [remainingWork,setRemainingWork]=useState(action.progress?.remaining_work??"");
  const completed=action.status==="COMPLETED";
  const validMinutes=actualMinutes===""||(Number.isInteger(Number(actualMinutes))&&Number(actualMinutes)>=0&&Number(actualMinutes)<=1440);
  const validDeferDate=Boolean(deferDate)&&deferDate>action.scheduled_date;
  const feedback={actual_date:recordDate,...(actualMinutes!==""?{actual_minutes:Number(actualMinutes)}:{}),...(completionDifficulty?{difficulty:Number(completionDifficulty)}:{}),...(note.trim()?{note:note.trim()}:{})};
  function toggle(next:typeof form){setSaved(false);setClearMinutes(false);if(recordDate!==localDate)setActualMinutes(action.time_entry?.actual_date===localDate&&action.time_entry.actual_minutes!==null?String(action.time_entry.actual_minutes):"");setRecordDate(localDate);setForm(current=>current===next?null:next);}
  async function saveProgress(){const save=form==="correction"?onCorrection:onProgress;const payload:Record<string,unknown>={kind:form==="correction"?"correction":"partial",...feedback,...(completedWork.trim()?{completed_work:completedWork.trim()}:{}),...(remainingWork.trim()?{remaining_work:remainingWork.trim()}:{})};if(form==="correction"&&clearMinutes){delete payload.actual_minutes;payload.cleared_fields=["actual_minutes"];}if(save&&await save(payload)){setSaved(true);if(clearMinutes){setActualMinutes("");setClearMinutes(false);}}}
  const minutesInput=<label><span>{form==="correction"?"该日累计用时（分钟，可选）":"今日累计用时（分钟，可选）"}</span><input aria-label={`${action.title} 实际分钟`} aria-invalid={!validMinutes} disabled={busy} inputMode="numeric" min="0" max="1440" type="number" value={actualMinutes} onChange={event=>{setSaved(false);setActualMinutes(event.target.value)}}/></label>;
  return (
    <article id={`action-${action.id}`} tabIndex={-1} className={`daily-action-card${overdue ? " daily-action-overdue" : ""}`} aria-busy={busy}>
      <div className="daily-action-heading">
        <div><span className="eyebrow">{completed?"已完成":overdue ? "已逾期" : action.required ? "必做" : "可选"}</span><h4>{action.title}</h4></div>
        <span className="daily-action-minutes">预计 {action.estimated_minutes} 分钟</span>
      </div>
      {action.description && <p>{action.description}</p>}
      <p className="daily-action-criteria"><strong>完成标准</strong>{action.completion_criteria}</p>
      {!completed&&action.progress?.state==="PARTIAL"&&<p role="status">部分完成{action.progress.remaining_work?` · 待完成：${action.progress.remaining_work}`:""}</p>}
      {action.progress?.state==="CARRIED_OVER"&&<p role="status">接续上次进度{action.progress.remaining_work?` · 待完成：${action.progress.remaining_work}`:""}</p>}
      {action.time_entry?.actual_date===localDate&&action.time_entry.actual_minutes!==null&&<p>今日已记录 {action.time_entry.actual_minutes} 分钟</p>}
      <div className="daily-action-controls daily-primary-actions">
        {completed?<>
          {onReopen&&<button className="button button-secondary" disabled={busy} type="button" onClick={onReopen}><Undo2 size={16} aria-hidden="true"/>撤销完成</button>}
          {onCorrection&&<button className="button button-quiet" disabled={busy} type="button" aria-expanded={form==="correction"} onClick={()=>toggle("correction")}><Pencil size={16} aria-hidden="true"/>修改记录</button>}
        </>:<>
        {onProgress&&<button className="button button-primary" disabled={busy} type="button" aria-expanded={form==="progress"} onClick={()=>toggle("progress")}><Pencil size={16} aria-hidden="true"/>记录进度</button>}
        <button className="button button-secondary" disabled={busy||!validMinutes} type="button" onClick={()=>onComplete(recordDate===localDate?{...feedback,actual_date:localDate}:{actual_date:localDate})}><Check size={16} aria-hidden="true"/>完成</button>
        {onHelp && <button className="button button-quiet" disabled={busy} type="button" aria-expanded={form==="help"} onClick={()=>toggle("help")}><CircleHelp size={16} aria-hidden="true"/>遇到困难</button>}
        <details className="daily-action-more">
          <summary aria-label={`${action.title} 更多操作`}>更多</summary>
          <div className="daily-action-more-content">
            {onCorrection&&<button className="button button-quiet" disabled={busy} type="button" onClick={()=>toggle("correction")}>修改记录</button>}
            <button className="button button-secondary" disabled={busy} type="button" onClick={onSkip}>跳过</button>
            <label><span>延期到</span><input aria-label={`${action.title} 延期日期`} disabled={busy} min={minimumDeferDate} type="date" value={deferDate} onChange={(event)=>setDeferDate(event.target.value)} /></label>
            <button className="button button-quiet" disabled={busy || !validDeferDate} type="button" onClick={()=>onDefer(deferDate)}>确认延期</button>
          </div>
        </details></>}
      </div>
      {form==="help"&&<form className="daily-execution-form" onSubmit={async event=>{event.preventDefault();if(question.trim())await onHelp?.(question.trim());}}><label>卡在哪里？<textarea autoFocus disabled={busy} maxLength={2000} value={question} onChange={event=>setQuestion(event.target.value)}/></label><button type="submit" className="button button-primary" disabled={busy||!question.trim()}>{busy?"正在求助…":"帮我解决"}</button></form>}
      {(form==="progress"||form==="correction")&&<div className="daily-execution-form">
        {form==="correction"&&<label>记录日期<input type="date" max={localDate} disabled={busy} value={recordDate} onChange={event=>{setActualMinutes("");setClearMinutes(false);setSaved(false);setRecordDate(event.target.value)}}/></label>}
        <label>已完成部分<textarea disabled={busy} value={completedWork} maxLength={2000} onChange={event=>{setSaved(false);setCompletedWork(event.target.value)}}/></label>
        {!completed&&<label>待完成或待验收<textarea disabled={busy} value={remainingWork} maxLength={2000} onChange={event=>{setSaved(false);setRemainingWork(event.target.value)}}/></label>}
        {minutesInput}
        {form==="correction"&&<label className="daily-clear-time"><input type="checkbox" disabled={busy} checked={clearMinutes} onChange={event=>{setClearMinutes(event.target.checked);setSaved(false)}}/>清除该日用时</label>}
        <button type="button" className="button button-secondary" disabled={busy||(!validMinutes&&!clearMinutes)||!recordDate||(form==="progress"?!remainingWork.trim():!completedWork.trim()&&actualMinutes===""&&!note.trim()&&!clearMinutes)} onClick={()=>void saveProgress()}>{busy?"正在保存…":form==="correction"?"保存更正":"保存部分进度"}</button>
        {saved&&<p role="status">记录已保存</p>}
      </div>}
      <details className="daily-feedback-details">
        <summary aria-label={`${action.title} 反馈与进度`}>补充反馈（可选）</summary>
        <div className="daily-action-controls">
          {form!=="progress"&&form!=="correction"&&minutesInput}
          <label><span>完成难度（可选）</span><select aria-label={`${action.title} 完成难度`} disabled={busy} value={completionDifficulty} onChange={event=>setCompletionDifficulty(event.target.value)}><option value="">不填写</option><option value="1">1 · 轻松</option><option value="2">2 · 较轻松</option><option value="3">3 · 合适</option><option value="4">4 · 较难</option><option value="5">5 · 困难</option></select></label>
          <button className="button button-quiet" disabled={busy||!completionDifficulty} type="button" onClick={()=>onFeedback(Number(completionDifficulty))}>记录难度</button>
        </div>
      {onNote && (
        <div className="daily-note">
          <label><span>今天的感受（可选）</span><textarea aria-label={`${action.title} 今天的感受`} disabled={busy} maxLength={2000} placeholder="遇到的困难或今天的状态" value={note} onChange={event=>setNote(event.target.value)}/></label>
          <button className="button button-secondary" disabled={busy || !note.trim()} type="button" onClick={async()=>{if(await onNote(note.trim())!==false)setNote("");}}>记录感受</button>
        </div>
      )}
      </details>
      {!validMinutes&&<p id={`minutes-error-${action.id}`} role="alert">实际分钟应为 0 到 1440 之间的整数。</p>}
    </article>
  );
}
