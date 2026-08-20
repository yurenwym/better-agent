import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  approvePlan,
  cancelStep,
  getPlans,
  getPlanDocument,
  getPlanVersion,
  getThreadPlan,
  putPlanDocument,
  restorePlanDocument,
  retryPlanProjection,
  syncPlanFile,
  revisePlan,
} from "../api";
import MarkdownMessage from "../components/MarkdownMessage";
import type { PlanDocument, PlanDocumentVersion, PlanResponse, PlanStep, Run } from "../types";

interface PlanPageProps {
  csrfToken: string;
  run: Run | null;
  threadId?: string | null;
  planId?: string | null;
  onRun: (run: Run) => void;
}

function editableSteps(steps: PlanStep[]): Array<{ id: string; title: string }> {
  return steps.filter((step) => step.status !== "completed" && step.status !== "cancelled").map((step) => ({ id: step.id, title: step.title }));
}

export default function PlanPage({ csrfToken, run, threadId = null, planId = null, onRun }: PlanPageProps) {
  const [document, setDocument] = useState<PlanDocument | null>(null);
  const [sourceDocument, setSourceDocument] = useState<PlanDocument | null>(null);
  const [structuredPlans, setStructuredPlans] = useState<PlanResponse | null>(null);
  const [title, setTitle] = useState("");
  const [markdown, setMarkdown] = useState("");
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [draft, setDraft] = useState<Array<{ id: string; title: string }>>([]);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);

  function applyDocument(next: PlanDocument) {
    setDocument(next);
    setTitle(next.current?.title ?? next.title);
    setMarkdown(next.current?.markdown ?? "");
    setSelectedVersion(next.current?.version ?? null);
  }

  useEffect(() => {
    let active = true;
    setError("");
    setConflict(null);
    setDocument(null);
    setSourceDocument(null);
    setStructuredPlans(null);
    async function load() {
      try {
        let nextDocument: PlanDocument | null = null;
        let nextStructuredPlans: PlanResponse | null = null;
        let nextSourceDocument: PlanDocument | null = null;
        if (planId) {
          nextDocument = await getPlanDocument(planId);
        } else if (threadId) {
          const response = await getThreadPlan(threadId);
          nextDocument = response.plan;
        }
        if (run) {
          nextStructuredPlans = await getPlans(run.id);
          if (run.source_plan_document_id) {
            if (nextDocument?.id === run.source_plan_document_id) {
              nextSourceDocument = nextDocument;
            } else {
              try {
                nextSourceDocument = await getPlanDocument(run.source_plan_document_id);
              } catch {
                nextSourceDocument = null;
              }
            }
          }
        }
        if (active) {
          if (nextDocument) applyDocument(nextDocument);
          setStructuredPlans(nextStructuredPlans);
          setSourceDocument(nextSourceDocument);
          setDraft(nextStructuredPlans?.current ? editableSteps(nextStructuredPlans.current.steps) : []);
        }
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : "Plan failed to load");
      }
    }
    void load();
    return () => { active = false; };
  }, [planId, threadId, run?.id, run?.source_plan_document_id]);

  const current = document?.current ?? null;
  const history = useMemo(() => [...(document?.versions ?? [])].sort((a, b) => b.version - a.version), [document?.versions]);
  const effectivePlanId = planId ?? document?.id ?? null;

  function handleDocumentError(caught: unknown, fallback: string) {
    if (caught instanceof ApiError && caught.status === 409 && caught.payload && typeof caught.payload === "object") {
      const payload = caught.payload as Record<string, unknown>;
      setConflict((payload.current as Record<string, unknown> | undefined) ?? null);
      setError("Plan conflict: the server has a newer version. Your local draft is preserved.");
      return;
    }
    if (caught instanceof ApiError && caught.status === 503) {
      setDocument((currentDocument) => currentDocument ? { ...currentDocument, file_status: "failed" } : currentDocument);
      setError("计划文件写入失败，可以重试。");
      return;
    }
    setError(caught instanceof Error ? caught.message : fallback);
  }

  async function saveDocument() {
    if (!document || !current || !effectivePlanId) return;
    setBusy(true); setError(""); setConflict(null);
    try {
      const saved = await putPlanDocument(effectivePlanId, {
        expected_version: current.version,
        expected_content_hash: current.content_hash,
        title: title.trim() || current.title,
        markdown,
        change_summary: "Edited in plan workspace",
      }, csrfToken);
      applyDocument({ ...document, title: saved.title, current_version_id: saved.id, projected_version_id: saved.id, file_status: "ready", current: saved, versions: [...document.versions, saved] });
    } catch (caught) { handleDocumentError(caught, "Plan save failed"); }
    finally { setBusy(false); }
  }

  async function restore(version: PlanDocumentVersion) {
    if (!document || !current || !effectivePlanId) return;
    setBusy(true);
    try {
      const restored = await restorePlanDocument(effectivePlanId, { version: version.version, expected_version: current.version, expected_content_hash: current.content_hash }, csrfToken);
      applyDocument({ ...document, title: restored.title, current_version_id: restored.id, projected_version_id: restored.id, file_status: "ready", current: restored, versions: [...document.versions, restored] });
    } catch (caught) { handleDocumentError(caught, "Restore failed"); }
    finally { setBusy(false); }
  }

  async function syncFile() {
    if (!effectivePlanId) return;
    setBusy(true);
    try {
      const synced = await syncPlanFile(effectivePlanId, csrfToken);
      if (document) applyDocument({ ...document, current: synced, current_version_id: synced.id, projected_version_id: synced.id, file_status: "ready", versions: [...document.versions, synced] });
    } catch (caught) { handleDocumentError(caught, "File sync failed"); }
    finally { setBusy(false); }
  }

  async function retryProjection() {
    if (!effectivePlanId) return;
    setBusy(true);
    try { applyDocument(await retryPlanProjection(effectivePlanId, csrfToken)); }
    catch (caught) { handleDocumentError(caught, "Projection retry failed"); }
    finally { setBusy(false); }
  }

  async function selectHistoryVersion(version: PlanDocumentVersion) {
    setSelectedVersion(version.version);
    setTitle(version.title);
    if (typeof version.markdown === "string") {
      setMarkdown(version.markdown);
      return;
    }
    if (!effectivePlanId) return;
    setBusy(true);
    try {
      const fullVersion = await getPlanVersion(effectivePlanId, version.version);
      setTitle(fullVersion.title);
      setMarkdown(fullVersion.markdown ?? "");
    } catch (caught) {
      handleDocumentError(caught, "Version failed to load");
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!run || !structuredPlans?.current) return;
    try { onRun(await approvePlan(run.id, structuredPlans.current.version, csrfToken)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Approval failed"); }
  }

  async function revise() {
    if (!run || !structuredPlans?.current || draft.some((step) => !step.title.trim())) return;
    try {
      const next = await revisePlan(run.id, structuredPlans.current.version, draft.map((step) => ({ ...step, title: step.title.trim() })), csrfToken);
      setStructuredPlans({ current: next, history: [...(structuredPlans.history ?? []), next] });
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Revision failed"); }
  }

  async function stopStep(stepId: string) {
    if (!run) return;
    try { onRun(await cancelStep(run.id, stepId, csrfToken)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Step cancellation failed"); }
  }

  function renderStructuredPlan() {
    const structured = structuredPlans?.current;
    if (!run || !structured) return null;
    return (
      <>
        <section className="card plan-card">
          <div className="panel-toolbar"><div><span className="eyebrow">CURRENT EXECUTION PLAN</span><h3>{structured.summary || "执行步骤"}</h3></div>{structured.status !== "approved" && <button className="button button-primary" type="button" onClick={() => void approve()}>批准计划</button>}</div>
          <ol className="step-list">{structured.steps.map((step) => <li className={`step-row step-${step.status}`} key={step.id}><span className="step-number">{step.position + 1}</span><div><strong>{step.title}</strong><span>{step.status}</span></div>{step.status === "pending" && <button className="button button-quiet" type="button" onClick={() => void stopStep(step.id)}>取消步骤</button>}</li>)}</ol>
        </section>
        <section className="card">
          <div className="panel-toolbar"><div><span className="eyebrow">REVISION</span><h3>调整未完成步骤</h3></div><button className="button button-quiet" type="button" onClick={() => setDraft([...draft, { id: `step-${Date.now()}`, title: "" }])}>新增步骤</button></div>
          <div className="draft-list">{draft.map((step, index) => <div className="draft-row" key={step.id}><span>{index + 1}</span><input aria-label={`步骤 ${index + 1}`} value={step.title} onChange={(event) => setDraft(draft.map((item) => item.id === step.id ? { ...item, title: event.target.value } : item))} /><button className="button button-quiet" type="button" onClick={() => setDraft(draft.filter((item) => item.id !== step.id))}>移除</button></div>)}</div>
          <div className="form-footer"><span className="muted">当前版本 v{structured.version}</span><button className="button button-primary" type="button" disabled={draft.length === 0 || draft.some((step) => !step.title.trim())} onClick={() => void revise()}>保存新版本</button></div>
        </section>
      </>
    );
  }

  if (document && !current) {
    return (
      <div className="page-stack plan-document-page">
        <section className="plan-document-hero"><div><span className="eyebrow">PLAN DOCUMENT / PENDING</span><h2>{document.title}</h2><p>计划 ID · {document.id} · {document.file_path}</p></div><span className={`version-badge file-status-${document.file_status}`}>{document.file_status}</span></section>
        {error && <p className="error-message" role="alert">{error}</p>}
        <section className="card plan-pending-card" role="status"><span className="eyebrow">DOCUMENT PROJECTION</span><h3>计划文档还没有可用版本</h3><p>计划地址已经保留。文件写入失败或仍在准备中，可以从这里重试，不需要重新调用模型。</p><button className="button button-primary" disabled={busy} type="button" onClick={() => void retryProjection()}>{busy ? "正在重试…" : "重试写入计划文件"}</button></section>
      </div>
    );
  }

  if (document && current) {
    return (
      <div className="page-stack plan-document-page">
        <section className="plan-document-hero"><div><span className="eyebrow">PLAN DOCUMENT / MARKDOWN</span><h2>{document.title}</h2><p>Conversation-owned document · version {current.version} · {current.content_hash}</p></div><span className={`version-badge file-status-${document.file_status}`}>{document.file_status}</span></section>
        {error && <div className="error-message" role="alert"><span>{error}</span>{document.file_status === "failed" && <button className="button button-danger" type="button" onClick={() => void retryProjection()}>Retry file write</button>}</div>}
        {conflict && <section className="plan-conflict" role="status"><strong>Newer server version detected</strong><p>Keep your draft, compare the server copy, then reload or merge manually.</p><pre>{String(conflict.markdown ?? "")}</pre></section>}
        <section className="plan-editor-grid">
          <div className="card plan-editor-card"><div className="panel-toolbar"><div><span className="eyebrow">SOURCE OF TRUTH</span><h3>Edit Markdown</h3></div><span className="muted">CAS v{current.version}</span></div><label className="field-label" htmlFor="plan-title">Title</label><input id="plan-title" value={title} maxLength={120} onChange={(event) => setTitle(event.target.value)} /><label className="field-label" htmlFor="plan-markdown">Markdown</label><textarea aria-label="Markdown editor" id="plan-markdown" value={markdown} onChange={(event) => setMarkdown(event.target.value)} rows={24} /><div className="form-footer"><span className="muted">{document.file_path}</span><button className="button button-primary" disabled={busy || !markdown.trim()} type="button" onClick={() => void saveDocument()}>Save plan</button></div></div>
          <div className="card plan-preview-card"><div className="panel-toolbar"><div><span className="eyebrow">PREVIEW</span><h3>Readable view</h3></div><span className="muted">Exact Markdown</span></div><MarkdownMessage content={markdown} /></div>
        </section>
        <section className="card plan-history-card"><div className="panel-toolbar"><div><span className="eyebrow">IMMUTABLE HISTORY</span><h3>Version history</h3></div><div className="button-row"><button className="button button-secondary" disabled={busy} type="button" onClick={() => void syncFile()}>Sync file</button><button className="button button-quiet" disabled={busy} type="button" onClick={() => void retryProjection()}>Retry projection</button></div></div><div className="history-list">{history.map((version) => <div className={selectedVersion === version.version ? "history-row history-row-selected" : "history-row"} key={version.id}><button className="history-version" disabled={busy} type="button" onClick={() => void selectHistoryVersion(version)}>v{version.version}</button><span>{version.actor}</span><span>{version.change_summary || "No summary"}</span><span>{version.content_hash.slice(0, 18)}</span>{version.status === "committed" && version.version !== current.version && <button className="button button-quiet" disabled={busy} type="button" aria-label={`Restore version ${version.version}`} onClick={() => void restore(version)}>Restore</button>}</div>)}</div></section>
        {renderStructuredPlan()}
      </div>
    );
  }

  if (!run && !threadId && !planId) return <section className="empty-panel"><span className="eyebrow">PLAN DOCUMENTS</span><h2>计划版本</h2><p>保存一份计划后，它会在这里以 Markdown 文档、版本历史和可恢复文件的形式出现。</p></section>;
  if (!document && threadId) return <section className="empty-panel"><span className="eyebrow">PLAN DOCUMENT</span><h2>当前对话还没有计划</h2><p>{error || "模型明确保存计划后，文档会出现在这里。"}</p></section>;

  const structured = structuredPlans?.current;
  const sourceVersionId = structured?.source_document_version_id ?? run?.source_plan_document_version_id ?? null;
  const sourceVersion = sourceVersionId
    ? sourceDocument?.versions.find((version) => version.id === sourceVersionId) ?? null
    : null;
  return <div className="page-stack"><section className="hero-panel"><div><span className="eyebrow">PLAN / EXECUTION PROJECTION</span><h2>计划版本</h2><p>结构化执行快照只在明确进入执行流程后出现；聊天中保存的 Markdown 计划不会自动创建 Run。</p>{sourceVersion && <p className="plan-source-reference">执行来源：{sourceVersion.title} · 文档 v{sourceVersion.version}{sourceDocument?.current && sourceDocument.current.version !== sourceVersion.version ? ` · 当前文档 v${sourceDocument.current.version}` : ""}</p>}</div>{structured && <span className="version-badge">v{structured.version} · {structured.status}</span>}</section>{error && <p className="error-message" role="alert">{error}</p>}{!structured ? <p className="empty-state">等待 Runtime 生成结构化执行计划。</p> : renderStructuredPlan()}</div>;
}
