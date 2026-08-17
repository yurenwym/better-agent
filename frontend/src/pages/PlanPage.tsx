import { useEffect, useState } from "react";
import { approvePlan, cancelStep, getPlans, revisePlan } from "../api";
import type { PlanResponse, PlanStep, Run } from "../types";

interface PlanPageProps {
  csrfToken: string;
  run: Run | null;
  onRun: (run: Run) => void;
}

function editableSteps(steps: PlanStep[]): Array<{ id: string; title: string }> {
  return steps.filter((step) => step.status !== "completed" && step.status !== "cancelled").map((step) => ({ id: step.id, title: step.title }));
}

export default function PlanPage({ csrfToken, run, onRun }: PlanPageProps) {
  const [plans, setPlans] = useState<PlanResponse | null>(null);
  const [draft, setDraft] = useState<Array<{ id: string; title: string }>>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!run) {
      setPlans(null);
      return;
    }
    getPlans(run.id).then((result) => {
      setPlans(result);
      setDraft(result.current ? editableSteps(result.current.steps) : []);
    }).catch((caught) => setError(caught instanceof Error ? caught.message : "计划加载失败"));
  }, [run]);

  if (!run) return <section className="empty-panel"><span className="eyebrow">PLAN VERSIONS</span><h2>计划版本</h2><p>创建目标后，这里会展示可批准和可调整的 PlanVersion。</p></section>;

  const runId = run.id;
  const current = plans?.current ?? null;
  async function approve() {
    if (!current) return;
    try { onRun(await approvePlan(runId, current.version, csrfToken)); } catch (caught) { setError(caught instanceof Error ? caught.message : "批准失败"); }
  }

  async function revise() {
    if (!current || draft.some((step) => !step.title.trim())) return;
    try {
      const next = await revisePlan(runId, current.version, draft.map((step) => ({ ...step, title: step.title.trim() })), csrfToken);
      setPlans({ current: next, history: [...(plans?.history ?? []), next] });
    } catch (caught) { setError(caught instanceof Error ? caught.message : "计划修订失败"); }
  }

  async function stopStep(stepId: string) {
    try { onRun(await cancelStep(runId, stepId, csrfToken)); } catch (caught) { setError(caught instanceof Error ? caught.message : "步骤取消失败"); }
  }

  return (
    <div className="page-stack">
      <section className="hero-panel"><div><span className="eyebrow">PLAN / VERSIONED</span><h2>计划版本</h2><p>每次修改都创建新版本，已完成的原子步骤不会被重写。</p></div>{current && <span className="version-badge">v{current.version} · {current.status}</span>}</section>
      {error && <p className="error-message" role="alert">{error}</p>}
      {!current ? <p className="empty-state">等待 Runtime 生成计划。</p> : <>
        <section className="card plan-card">
          <div className="panel-toolbar"><div><span className="eyebrow">CURRENT PLAN</span><h3>{current.summary || "执行步骤"}</h3></div>{current.status !== "approved" && <button className="button button-primary" type="button" onClick={() => void approve()}>批准计划</button>}</div>
          <ol className="step-list">
            {current.steps.map((step) => <li className={`step-row step-${step.status}`} key={step.id}><span className="step-number">{step.position + 1}</span><div><strong>{step.title}</strong><span>{step.status}</span></div>{step.status === "pending" && <button className="button button-quiet" type="button" onClick={() => void stopStep(step.id)}>取消步骤</button>}</li>)}
          </ol>
        </section>
        <section className="card">
          <div className="panel-toolbar"><div><span className="eyebrow">REVISION</span><h3>调整未完成步骤</h3></div><button className="button button-quiet" type="button" onClick={() => setDraft([...draft, { id: `step-${Date.now()}`, title: "" }])}>新增步骤</button></div>
          <div className="draft-list">{draft.map((step, index) => <div className="draft-row" key={step.id}><span>{index + 1}</span><input aria-label={`步骤 ${index + 1}`} value={step.title} onChange={(event) => setDraft(draft.map((item) => item.id === step.id ? { ...item, title: event.target.value } : item))} /><button aria-label={`上移步骤 ${index + 1}`} className="button button-quiet" disabled={index === 0} type="button" onClick={() => setDraft(moveStep(draft, index, -1))}>↑</button><button aria-label={`下移步骤 ${index + 1}`} className="button button-quiet" disabled={index === draft.length - 1} type="button" onClick={() => setDraft(moveStep(draft, index, 1))}>↓</button><button className="button button-quiet" type="button" onClick={() => setDraft(draft.filter((item) => item.id !== step.id))}>移除</button></div>)}</div>
          <div className="form-footer"><span className="muted">当前版本 v{current.version}</span><button className="button button-primary" type="button" disabled={draft.length === 0 || draft.some((step) => !step.title.trim())} onClick={() => void revise()}>保存新版本</button></div>
        </section>
        <section className="card history-card"><div className="section-heading"><span className="eyebrow">AUDIT TRAIL</span><h3>历史版本</h3></div><div className="history-list">{(plans?.history ?? []).map((plan) => <div key={plan.id}><strong>v{plan.version}</strong><span>{plan.status}</span><span>{plan.steps.length} 步</span></div>)}</div></section>
      </>}
    </div>
  );
}

function moveStep<T>(items: T[], index: number, delta: number): T[] {
  const next = [...items];
  const target = index + delta;
  if (target < 0 || target >= next.length) return next;
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}
