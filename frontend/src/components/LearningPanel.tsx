import { useEffect, useState } from "react";
import { getLearningHistory, getLearningPolicy, saveLearningPolicy, suspendLearningJob } from "../api";
import type { LearningJob, LearningPolicy } from "../api";

const assets: Record<string, string> = { memory: "明确记忆", skill: "流程技能", task_policy: "任务策略", prompt: "研究写作 Prompt" };
const statuses: Record<string, string> = { QUEUED: "等待处理", RUNNING: "处理中", UNKNOWN: "结果未知，不自动重发", NO_CHANGE: "没有足够依据改变", APPLIED: "已采用", REJECTED: "未通过采用条件", SUSPENDED: "已暂停" };
const effects: Record<string, string> = { UNKNOWN: "效果待验证", SUPPORTED: "已有支持证据", REFUTED: "发现退化" };

export default function LearningPanel({ csrfToken }: { csrfToken: string }) {
  const [policy, setPolicy] = useState<LearningPolicy | null>(null);
  const [jobs, setJobs] = useState<LearningJob[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  async function reload() {
    setError("");
    try {
      const [nextPolicy, history] = await Promise.all([getLearningPolicy(), getLearningHistory()]);
      setPolicy({ ...nextPolicy, config: { allowed_assets: ["memory"], trial_days: 7, max_attempts: 1, cycle_microusd: 0, daily_microusd: 0, monthly_microusd: 0, ...nextPolicy.config } });
      setJobs(history.items);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "学习记录加载失败，请重试。"); }
  }
  useEffect(() => { void reload(); }, []);
  async function save() {
    if (!policy) return;
    setBusy(true); setError(""); setNotice("");
    try { setPolicy(await saveLearningPolicy(policy, csrfToken)); setNotice("学习政策已保存，符合条件的更新将自动处理。"); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "保存失败，请刷新后重试。"); }
    finally { setBusy(false); }
  }
  async function suspend(job: LearningJob) {
    setBusy(true); setError("");
    try { await suspendLearningJob(job, csrfToken); await reload(); setNotice("已暂停这项学习变更。"); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "暂停失败，请刷新后重试。"); }
    finally { setBusy(false); }
  }
  return <section className="control-panel learning-panel" aria-label="自主学习政策与记录" aria-busy={busy}>
    <div className="section-heading"><span className="eyebrow">自主学习</span><h3>设定一次，按证据学习</h3><p>你可以限定学习范围、暂停学习或撤回变更。采用状态与效果证据分别记录。</p></div>
    {error && <p role="alert" className="error-message">{error} <button className="button button-secondary" disabled={busy} onClick={() => void reload()}>重新加载</button></p>}
    {!policy && !error && <p role="status">正在加载学习政策…</p>}
    {policy && <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset disabled={busy} className="learning-settings">
        <legend>学习范围</legend>
        <label><input type="checkbox" checked={!policy.paused} onChange={event => setPolicy({ ...policy, paused: !event.target.checked })} />启用自主学习</label>
        <div className="chip-row">{Object.entries(assets).map(([key, label]) => <label key={key}><input type="checkbox" checked={policy.config.allowed_assets?.includes(key) ?? false} onChange={event => setPolicy({ ...policy, config: { ...policy.config, allowed_assets: event.target.checked ? [...(policy.config.allowed_assets ?? []), key] : policy.config.allowed_assets?.filter(item => item !== key) } })} />{label}</label>)}</div>
        <label><input type="checkbox" checked={(policy.config.cycle_microusd??0)>0} onChange={event=>setPolicy({...policy,config:{...policy.config,cycle_microusd:event.target.checked?1:0,daily_microusd:event.target.checked?1:0,monthly_microusd:event.target.checked?1:0}})}/>允许模型参与学习</label>
        <label>试用期限（天）<input type="number" min="1" max="90" required value={policy.config.trial_days ?? 7} onChange={event => setPolicy({ ...policy, config: { ...policy.config, trial_days: event.target.valueAsNumber } })} /></label>
        <label>每轮最多模型调用<input type="number" min="1" max="500" required value={policy.config.max_attempts ?? 1} onChange={event => setPolicy({ ...policy, config: { ...policy.config, max_attempts: event.target.valueAsNumber } })} /></label>
        {policy.config.allowed_assets?.includes("prompt") && <details><summary>研究写作评测设置</summary>
          <p className="muted">60 个案例的完整生成与评测至少需要 241 次模型调用。</p>
          <label>冻结评测集编号<input value={policy.config.prompt_suite_digest ?? ""} pattern="[0-9a-f]{64}" onChange={event => setPolicy({ ...policy, config: { ...policy.config, prompt_suite_digest: event.target.value || null } })} /></label>
        </details>}
        <button className="button button-primary" type="submit">{busy ? "正在保存…" : "保存学习政策"}</button>
      </fieldset>
    </form>}
    {notice && <p role="status">{notice}</p>}
    <div className="control-panel-head"><h3>最近的学习变更</h3><button className="button button-secondary" disabled={busy} onClick={() => void reload()}>刷新记录</button></div>
    {policy && jobs.length === 0 && <p className="muted">暂无学习记录。开启后，有来源的反馈会进入学习流程。</p>}
    {jobs.slice(0, 10).map(job => <article className="control-row" key={job.id}><div><strong>{statuses[job.status] ?? job.status}</strong><p>{new Date(job.created_at).toLocaleString()} · {job.provenance === "production" ? "日常任务" : "验收数据"}</p>
      {job.changes.map(change => <p key={change.after}>{assets[change.asset_type] ?? change.asset_type} · {change.adoption === "TRIAL" ? "试用中" : change.adoption === "ACTIVE" ? "已启用" : "已暂停"} · {effects[change.effect] ?? change.effect}</p>)}
      {job.reason && <details><summary>查看处理依据</summary><p>{job.reason}</p></details>}</div>
      {job.status === "APPLIED" && <button className="button button-secondary" disabled={busy} onClick={() => void suspend(job)}>暂停变更</button>}
    </article>)}
  </section>;
}
