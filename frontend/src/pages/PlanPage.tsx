import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  ApiError,
  approvePlan,
  cancelStep,
  deletePlanDocument,
  getPlans,
  getPlanDocument,
  getPlanVersion,
  getThreadPlan,
  listPlanDocuments,
  putPlanDocument,
  restorePlanDocument,
  retryPlanProjection,
  syncPlanFile,
  revisePlan,
  previewGoalProgram,
  activateGoalProgram,
} from "../api";
import MarkdownMessage from "../components/MarkdownMessage";
import PlanVisualEditor from "../components/PlanVisualEditor";
import type { GoalProgram, PlanDocument, PlanDocumentSummary, PlanDocumentVersion, PlanResponse, PlanStep, Run } from "../types";

interface PlanPageProps {
  csrfToken: string;
  run: Run | null;
  threadId?: string | null;
  planId?: string | null;
  onRun: (run: Run) => void;
  onSelectPlan?: (planId: string) => void;
  onDeleted?: () => void;
}

interface PlanLibraryProps {
  plans: PlanDocumentSummary[];
  selectedPlanId: string | null;
  busy: boolean;
  onSelect: (planId: string) => void;
}

function formatPlanDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}

function planStatusLabel(status: string): string {
  if (status === "ready") return "已保存";
  if (status === "failed") return "待重试";
  if (status === "conflict") return "有冲突";
  if (status === "pending") return "准备中";
  return status;
}

function PlanLibrary({ plans, selectedPlanId, busy, onSelect }: PlanLibraryProps) {
  return (
    <nav className="plan-library" aria-label="Saved plans">
      <div className="plan-library-heading">
        <div>
          <span className="eyebrow">PLAN LIBRARY</span>
          <h2>已保存计划</h2>
        </div>
        <span className="plan-library-count">{plans.length} 份</span>
      </div>
      {plans.length === 0 ? (
        <p className="plan-library-empty">保存计划后，它们会按时间出现在这里。</p>
      ) : (
        <div className="plan-library-list">
          {plans.map((plan) => (
            <button
              aria-current={selectedPlanId === plan.id ? "page" : undefined}
              className={`plan-library-item${selectedPlanId === plan.id ? " plan-library-item-selected" : ""}`}
              disabled={busy}
              key={plan.id}
              type="button"
              onClick={() => onSelect(plan.id)}
            >
              <span className="plan-library-item-title">{plan.title}</span>
              <span className="plan-library-item-meta">
                <span>{plan.version ? `v${plan.version}` : "新计划"}</span>
                <span>{planStatusLabel(plan.file_status)}</span>
                <span>{formatPlanDate(plan.updated_at)}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </nav>
  );
}

function PlanShell({ children, plans, selectedPlanId, busy, onSelect }: PlanLibraryProps & { children: ReactNode }) {
  return (
    <div className="plan-workspace">
      <PlanLibrary plans={plans} selectedPlanId={selectedPlanId} busy={busy} onSelect={onSelect} />
      <section className="plan-detail-column">{children}</section>
    </div>
  );
}

function editableSteps(steps: PlanStep[]): Array<{ id: string; title: string }> {
  return steps.filter((step) => step.status !== "completed" && step.status !== "cancelled").map((step) => ({ id: step.id, title: step.title }));
}

export default function PlanPage({ csrfToken, run, threadId = null, planId = null, onRun, onSelectPlan, onDeleted }: PlanPageProps) {
  const [planSummaries, setPlanSummaries] = useState<PlanDocumentSummary[]>([]);
  const [selectedPlanId, setSelectedPlanId] = useState<string | null>(planId);
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
  const [editing, setEditing] = useState(false);
  const [program, setProgram] = useState<GoalProgram | null>(null);
  const [showExecution, setShowExecution] = useState(false);
  const [startDate, setStartDate] = useState(() => new Date().toISOString().slice(0,10));
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai");
  const [dailyMinutes, setDailyMinutes] = useState(60);

  const activePlanId = planId ?? selectedPlanId;

  useEffect(() => {
    setSelectedPlanId(planId);
  }, [planId]);

  useEffect(() => {
    let active = true;
    listPlanDocuments()
      .then((response) => {
        if (active) setPlanSummaries(response.plans);
      })
      .catch(() => undefined);
    return () => { active = false; };
  }, []);

  function applyDocument(next: PlanDocument) {
    setDocument(next);
    setTitle(next.current?.title ?? next.title);
    setMarkdown(next.current?.markdown ?? "");
    setSelectedVersion(next.current?.version ?? null);
    setPlanSummaries((items) => {
      const summary = {
        id: next.id,
        thread_id: next.thread_id,
        title: next.title,
        version: next.current?.version ?? null,
        file_status: next.file_status,
        created_at: next.created_at,
        updated_at: next.updated_at,
      } satisfies PlanDocumentSummary;
      const existing = items.some((item) => item.id === next.id);
      return existing ? items.map((item) => item.id === next.id ? summary : item) : [summary, ...items];
    });
  }

  async function createProgramPreview() {
    if (!document) return;
    setBusy(true); setError("");
    try { setProgram(await previewGoalProgram(document.id,{start_date:startDate,timezone,daily_minutes:dailyMinutes},crypto.randomUUID(),csrfToken)); }
    catch(reason){setError(reason instanceof Error?reason.message:"生成执行预览失败");}
    finally{setBusy(false);}
  }

  async function activateProgram() {
    if (!program) return;
    setBusy(true); setError("");
    try { setProgram(await activateGoalProgram(program.id,program.version,crypto.randomUUID(),csrfToken)); }
    catch(reason){setError(reason instanceof Error?reason.message:"激活执行项目失败");}
    finally{setBusy(false);}
  }

  useEffect(() => {
    let active = true;
    setError("");
    setConflict(null);
    setEditing(false);
    setDocument(null);
    setSourceDocument(null);
    setStructuredPlans(null);
    async function load() {
      try {
        let nextDocument: PlanDocument | null = null;
        let nextStructuredPlans: PlanResponse | null = null;
        let nextSourceDocument: PlanDocument | null = null;
        if (activePlanId) {
          nextDocument = await getPlanDocument(activePlanId);
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
  }, [activePlanId, threadId, run?.id, run?.source_plan_document_id]);

  const current = document?.current ?? null;
  const history = useMemo(() => [...(document?.versions ?? [])].sort((a, b) => b.version - a.version), [document?.versions]);
  const effectivePlanId = activePlanId ?? document?.id ?? null;

  function selectPlan(nextPlanId: string) {
    setSelectedPlanId(nextPlanId);
    onSelectPlan?.(nextPlanId);
  }

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
      setEditing(false);
    } catch (caught) { handleDocumentError(caught, "Plan save failed"); }
    finally { setBusy(false); }
  }

  function cancelEditing() {
    if (!current) return;
    setTitle(current.title);
    setMarkdown(current.markdown ?? "");
    setSelectedVersion(current.version);
    setConflict(null);
    setError("");
    setEditing(false);
  }

  async function deleteDocument() {
    if (!document || !current || !effectivePlanId || !window.confirm("Delete this plan?")) return;
    setBusy(true); setError(""); setConflict(null);
    try {
      await deletePlanDocument(effectivePlanId, {
        expected_version: current.version,
        expected_content_hash: current.content_hash,
      }, csrfToken);
      onDeleted?.();
    } catch (caught) { handleDocumentError(caught, "Plan delete failed"); }
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

  const shellProps = {
    plans: planSummaries,
    selectedPlanId: activePlanId,
    busy,
    onSelect: selectPlan,
  };

  if (document && !current) {
    return (
      <PlanShell {...shellProps}>
        <div className="page-stack plan-document-page">
        <section className="plan-document-hero"><div><span className="eyebrow">PLAN DOCUMENT / PENDING</span><h2>{document.title}</h2><p>计划 ID · {document.id} · {document.file_path}</p></div><span className={`version-badge file-status-${document.file_status}`}>{document.file_status}</span></section>
        {error && <p className="error-message" role="alert">{error}</p>}
        <section className="card plan-pending-card" role="status"><span className="eyebrow">DOCUMENT PROJECTION</span><h3>计划文档还没有可用版本</h3><p>计划地址已经保留。文件写入失败或仍在准备中，可以从这里重试，不需要重新调用模型。</p><button className="button button-primary" disabled={busy} type="button" onClick={() => void retryProjection()}>{busy ? "正在重试…" : "重试写入计划文件"}</button></section>
        </div>
      </PlanShell>
    );
  }

  if (document && current) {
    return (
      <PlanShell {...shellProps}>
        <div className="page-stack plan-document-page">
        <section className="plan-document-hero"><div><span className="eyebrow">PLAN DOCUMENT / RENDERED VIEW</span><h2>{document.title}</h2><p>Conversation-owned document · version {current.version} · {current.content_hash}</p></div><div className="button-row"><span className={`version-badge file-status-${document.file_status}`}>{document.file_status}</span>{editing ? <><button className="button button-secondary" disabled={busy} type="button" onClick={cancelEditing}>取消编辑</button><button className="button button-primary" disabled={busy || !markdown.trim()} type="button" onClick={() => void saveDocument()}>保存计划</button></> : <><button className="button button-secondary" disabled={busy} type="button" onClick={()=>setShowExecution(value=>!value)}>开始执行</button><button className="button button-primary" disabled={busy} type="button" onClick={() => setEditing(true)}>编辑计划</button></>}<button className="button button-danger" disabled={busy} type="button" onClick={() => void deleteDocument()}>Delete plan</button></div></section>
        {error && <div className="error-message" role="alert"><span>{error}</span>{document.file_status === "failed" && <button className="button button-danger" type="button" onClick={() => void retryProjection()}>Retry file write</button>}</div>}
        {conflict && <section className="plan-conflict" role="status"><strong>Newer server version detected</strong><p>Keep your draft, compare the server copy, then reload or merge manually.</p><div className="plan-conflict-preview"><MarkdownMessage content={String(conflict.markdown ?? "")} /></div></section>}
        {editing ? <section className="card plan-editor-card"><PlanVisualEditor title={title} onTitleChange={setTitle} markdown={markdown} onChange={setMarkdown} disabled={busy} /></section> : <section className="card plan-readable-card"><div className="panel-toolbar"><div><span className="eyebrow">PLAN CONTENT</span><h3>Readable plan</h3></div><span className="muted">Rendered view</span></div><MarkdownMessage content={markdown} /></section>}
        {showExecution&&<section className="card program-preview-card" aria-busy={busy}><div className="panel-toolbar"><div><span className="eyebrow">EXECUTION PROGRAM</span><h3>{program?.status==="ACTIVE"?"当前执行版本":"生成执行预览"}</h3></div>{program&&<span className="version-badge">{program.status} · v{program.version}</span>}</div>{!program?<div className="program-preview-form"><label>开始日期<input aria-label="执行开始日期" type="date" value={startDate} onChange={event=>setStartDate(event.target.value)}/></label><label>时区<input aria-label="执行时区" value={timezone} onChange={event=>setTimezone(event.target.value)}/></label><label>每日分钟<input aria-label="每日可用分钟" min="5" max="1440" type="number" value={dailyMinutes} onChange={event=>setDailyMinutes(Number(event.target.value))}/></label><button className="button button-primary" disabled={busy} type="button" onClick={()=>void createProgramPreview()}>{busy?"正在编译…":"生成预览"}</button></div>:<><p>来源文档 v{current.version} · {program.source_plan_content_hash}</p><p>{program.start_date} — {program.end_date} · {program.timezone} · 每日预算 {program.daily_minutes} 分钟</p>{program.structure?.assumptions.length?<ul>{program.structure.assumptions.map(item=><li key={item}>{item}</li>)}</ul>:null}<div className="program-calendar">{program.structure?.actions.map(action=><article key={action.logical_key}><span>{action.scheduled_date}</span><strong>{action.title}</strong><small>{action.estimated_minutes} 分钟</small></article>)}</div>{program.status==="DRAFT"&&<button className="button button-primary" disabled={busy} type="button" onClick={()=>void activateProgram()}>{busy?"正在激活…":"确认并激活"}</button>}<p className="muted">原计划文档与执行版本相互独立；继续编辑正文不会静默改变已激活安排。</p></>}</section>}
        <section className="card plan-history-card"><div className="panel-toolbar"><div><span className="eyebrow">IMMUTABLE HISTORY</span><h3>Version history</h3></div><div className="button-row"><button className="button button-secondary" disabled={busy} type="button" onClick={() => void syncFile()}>Sync file</button><button className="button button-quiet" disabled={busy} type="button" onClick={() => void retryProjection()}>Retry projection</button></div></div><div className="history-list">{history.map((version) => <div className={selectedVersion === version.version ? "history-row history-row-selected" : "history-row"} key={version.id}><button className="history-version" disabled={busy} type="button" onClick={() => void selectHistoryVersion(version)}>v{version.version}</button><span>{version.actor}</span><span>{version.change_summary || "No summary"}</span><span>{version.content_hash.slice(0, 18)}</span>{version.status === "committed" && version.version !== current.version && <button className="button button-quiet" disabled={busy} type="button" aria-label={`Restore version ${version.version}`} onClick={() => void restore(version)}>Restore</button>}</div>)}</div></section>
        {renderStructuredPlan()}
        </div>
      </PlanShell>
    );
  }

  if (!run && !threadId && !activePlanId) return <PlanShell {...shellProps}><section className="empty-panel plan-empty-state" aria-label="Plan detail placeholder"><span className="eyebrow">PLAN LIBRARY</span><h2>{planSummaries.length ? "选择一个计划" : "还没有已保存计划"}</h2><p>{planSummaries.length ? "从左侧选择计划名称，查看 Markdown 内容、版本历史和可编辑详情。" : "保存计划后，它们会显示在左侧列表中。"}</p></section></PlanShell>;
  if (!document && threadId) return <PlanShell {...shellProps}><section className="empty-panel"><span className="eyebrow">PLAN DOCUMENT</span><h2>当前对话还没有计划</h2><p>{error || "模型明确保存计划后，文档会出现在这里。"}</p></section></PlanShell>;

  const structured = structuredPlans?.current;
  const sourceVersionId = structured?.source_document_version_id ?? run?.source_plan_document_version_id ?? null;
  const sourceVersion = sourceVersionId
    ? sourceDocument?.versions.find((version) => version.id === sourceVersionId) ?? null
    : null;
  return <PlanShell {...shellProps}><div className="page-stack"><section className="hero-panel"><div><span className="eyebrow">PLAN / EXECUTION PROJECTION</span><h2>计划版本</h2><p>结构化执行快照只在明确进入执行流程后出现；聊天中保存的 Markdown 计划不会自动创建 Run。</p>{sourceVersion && <p className="plan-source-reference">执行来源：{sourceVersion.title} · 文档 v{sourceVersion.version}{sourceDocument?.current && sourceDocument.current.version !== sourceVersion.version ? ` · 当前文档 v${sourceDocument.current.version}` : ""}</p>}</div>{structured && <span className="version-badge">v{structured.version} · {structured.status}</span>}</section>{error && <p className="error-message" role="alert">{error}</p>}{!structured ? <p className="empty-state">等待 Runtime 生成结构化执行计划。</p> : renderStructuredPlan()}</div></PlanShell>;
}
