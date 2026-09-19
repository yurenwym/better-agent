import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, ArrowUpRight, MessageSquare } from "lucide-react";
import { ApiError, approvePlan, cancelStep, deletePlanDocument, getPlans, getPlanDocument, getPlanVersion, getThreadPlan, listPlanDocuments, putPlanDocument, restorePlanDocument, retryPlanProjection, syncPlanFile, revisePlan, previewGoalProgram, activateGoalProgram, retryGoalProgramCompile, listGoalPrograms } from "../api";
import MarkdownMessage from "../components/MarkdownMessage";
import PlanVisualEditor from "../components/PlanVisualEditor";
import ConfirmDialog from "../components/ConfirmDialog";
import { localizedCode } from "../localization";
import AppToast from "../components/AppToast";
import { todayPath } from "../navigation";
import type { GoalProgram, PlanDocument, PlanDocumentSummary, PlanDocumentVersion, PlanResponse, PlanStep, Run } from "../types";

interface PlanPageProps {
  embedded?: boolean;
  csrfToken: string;
  run: Run | null;
  threadId?: string | null;
  planId?: string | null;
  onRun: (run: Run) => void;
  onSelectPlan?: (planId: string) => void;
  onDeleted?: () => void;
  onOpenToday?: (programId?: string) => void;
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
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
  }).format(date);
}

function planStatusLabel(status: string): string {
  if (status === "ready") return "已保存";
  if (status === "failed") return "待重试";
  if (status === "conflict") return "有冲突";
  if (status === "pending") return "准备中";
  return status;
}

function executionStatusLabel(program: GoalProgram): string {
  if (program.status === "DRAFT" && program.compile_status === "FAILED") return "执行生成失败";
  if (program.status === "DRAFT" && program.compile_status === "COMPILING") return "正在生成执行预览";
  if (program.status === "DRAFT") return "执行待确认";
  if (program.status === "PAUSED") return "执行已暂停";
  if (program.status === "ACTIVE") return "正在执行";
  if (program.status === "COMPLETED") return "执行已完成";
  return "执行已取消";
}

function executionStatusClass(program: GoalProgram): string {
  if (program.status === "DRAFT" && program.compile_status === "FAILED") return "status-failed";
  if (program.status === "DRAFT" && program.compile_status === "COMPILING") return "status-compiling";
  return `status-${program.status.toLowerCase()}`;
}

function executionStatusDescription(program: GoalProgram): string {
  if (program.status === "DRAFT" && program.compile_status === "FAILED") return "执行安排尚未创建，今日行动暂时不会出现任务。";
  if (program.status === "DRAFT" && program.compile_status === "COMPILING") return "正在把计划整理为具体日期和行动。";
  if (program.status === "DRAFT") return "预览已经生成，确认后才会进入今日行动。";
  if (program.status === "ACTIVE") return "行动已进入今日行动，可以开始记录进度。";
  if (program.status === "PAUSED") return "当前行动已暂停，不会继续推进。";
  if (program.status === "COMPLETED") return "这份执行安排已经完成。";
  return "这份执行安排已经取消。";
}

function PlanLibrary({ plans, selectedPlanId, busy, onSelect }: PlanLibraryProps) {
  return (
    <nav className="plan-library" aria-label="已保存计划">
      <div className="plan-library-heading">
        <div>
          <span className="eyebrow">计划库</span>
          <h2>已保存计划</h2>
        </div>
        <span className="plan-library-count">{plans.length} 份</span>
      </div>
      {plans.length === 0 ? (
        <p className="plan-library-empty">保存计划后，它们会按时间出现在这里。</p>
      ) : (
        <div className="plan-library-list">
          {plans.map((plan) => (
            <button aria-current={selectedPlanId === plan.id ? "page" : undefined} className={`plan-library-item${selectedPlanId === plan.id ? " plan-library-item-selected" : ""}`} disabled={busy} key={plan.id} type="button" onClick={() => onSelect(plan.id)}>
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

function PlanShell({ children, plans, selectedPlanId, busy, onSelect, embedded=false }: PlanLibraryProps & { children: ReactNode; embedded?:boolean }) {
  return (
    <div className={`plan-workspace${embedded?" plan-workspace-embedded":""}`}>
      {!embedded&&<PlanLibrary plans={plans} selectedPlanId={selectedPlanId} busy={busy} onSelect={onSelect} />}
      <section className="plan-detail-column">{children}</section>
    </div>
  );
}

function editableSteps(steps: PlanStep[]): Array<{ id: string; title: string }> {
  return steps.filter((step) => step.status !== "completed" && step.status !== "cancelled").map((step) => ({ id: step.id, title: step.title }));
}

export default function PlanPage({ csrfToken, run, threadId = null, planId = null, onRun, onSelectPlan, onDeleted, onOpenToday, embedded=false }: PlanPageProps) {
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
  const executionPanelRef = useRef<HTMLElement>(null);
  function openExecution() {
    setShowExecution(true);
    requestAnimationFrame(() => {
      executionPanelRef.current?.scrollIntoView?.({ behavior: "auto", block: "start" });
      executionPanelRef.current?.focus({ preventScroll: true });
    });
  }
  const [showLibrary, setShowLibrary] = useState(false);
  const [startDate, setStartDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [endDate, setEndDate] = useState("");
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai");
  const [dailyMinutes, setDailyMinutes] = useState(60);
  const [availableWeekdays,setAvailableWeekdays] = useState([0,1,2,3,4,5,6]);
  const [excludedDates,setExcludedDates] = useState<string[]>([]);
  const [excludedDate,setExcludedDate] = useState("");
  const latestEndDate = startDate ? new Date(Date.parse(`${startDate}T00:00:00Z`)+29*86400000).toISOString().slice(0,10) : "";
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [notice, setNotice] = useState<{
    message: string;
    tone: "success" | "error";
    action?: { label: string; onClick: () => void };
    durationMs?: number;
  } | null>(null);
  const [linkedProgram, setLinkedProgram] = useState<GoalProgram | null>(null);

  const activePlanId = showLibrary ? null : (planId ?? selectedPlanId);

  useEffect(() => {
    setSelectedPlanId(planId);
    if (planId) setShowLibrary(false);
  }, [planId]);

  useEffect(() => {
    let active = true;
    listPlanDocuments()
      .then((response) => {
        if (active) setPlanSummaries(response.plans);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
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
      return existing ? items.map((item) => (item.id === next.id ? summary : item)) : [summary, ...items];
    });
  }

  async function createProgramPreview() {
    if (!document) return;
    setBusy(true);
    setError("");
    try {
      setProgram(
        await previewGoalProgram(
          document.id,
          {
            start_date: startDate,
            ...(endDate ? { requested_end_date: endDate } : {}),
            timezone,
            daily_minutes: dailyMinutes,
            schedule_constraints: {available_weekdays:availableWeekdays,excluded_dates:excludedDates},
          },
          crypto.randomUUID(),
          csrfToken,
        ),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "生成执行预览失败");
    } finally {
      setBusy(false);
    }
  }

  async function activateProgram() {
    if (!program) return;
    setBusy(true);
    setError("");
    try {
      const next = await activateGoalProgram(program.id, program.version, crypto.randomUUID(), csrfToken);
      setProgram(next);
      setLinkedProgram(next);
      setNotice({
        message: "执行已激活，今天起会按日期生成行动。",
        tone: "success",
        ...(onOpenToday ? { action: { label: "去查看今日行动", onClick: () => onOpenToday(next.id) }, durationMs: 0 } : {}),
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "激活执行项目失败");
    } finally {
      setBusy(false);
    }
  }

  async function retryProgramCompile() {
    if (!program) return;
    setBusy(true);
    setError("");
    try {
      const next = await retryGoalProgramCompile(program.id, program.version, crypto.randomUUID(), csrfToken);
      setProgram(next);
      setLinkedProgram(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重新生成执行预览失败");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    setError("");
    setConflict(null);
    setEditing(false);
    setDocument(null);
    setSourceDocument(null);
    setStructuredPlans(null);
    setLinkedProgram(null);
    setProgram(null);
    setShowExecution(false);
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
        const programs = nextDocument ? (await listGoalPrograms()).programs : [];
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
          const linked = nextDocument ? (programs.find((item) => item.source_plan_document_id === nextDocument!.id && ["DRAFT", "ACTIVE", "PAUSED"].includes(item.status)) ?? programs.find((item) => item.source_plan_document_id === nextDocument!.id) ?? null) : null;
          setLinkedProgram(linked);
          if (linked?.status === "DRAFT") {
            setProgram(linked);
            setShowExecution(true);
          } else if (!linked && nextDocument?.current && new URLSearchParams(window.location.search).get("execute") === "1") {
            openExecution();
          }
          setDraft(nextStructuredPlans?.current ? editableSteps(nextStructuredPlans.current.steps) : []);
        }
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : "计划加载失败");
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [activePlanId, threadId, run?.id, run?.source_plan_document_id]);

  const current = document?.current ?? null;
  const history = useMemo(() => [...(document?.versions ?? [])].sort((a, b) => b.version - a.version), [document?.versions]);
  const effectivePlanId = activePlanId ?? document?.id ?? null;

  function selectPlan(nextPlanId: string) {
    setShowLibrary(false);
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
      setDocument((currentDocument) => (currentDocument ? { ...currentDocument, file_status: "failed" } : currentDocument));
      setError("计划文件写入失败，可以重试。");
      return;
    }
    setError(caught instanceof Error ? caught.message : fallback);
  }

  async function saveDocument() {
    if (!document || !current || !effectivePlanId) return;
    setBusy(true);
    setError("");
    setConflict(null);
    try {
      const saved = await putPlanDocument(
        effectivePlanId,
        {
          expected_version: current.version,
          expected_content_hash: current.content_hash,
          title: title.trim() || current.title,
          markdown,
          change_summary: "Edited in plan workspace",
        },
        csrfToken,
      );
      applyDocument({
        ...document,
        title: saved.title,
        current_version_id: saved.id,
        projected_version_id: saved.id,
        file_status: "ready",
        current: saved,
        versions: [...document.versions, saved],
      });
      setEditing(false);
    } catch (caught) {
      handleDocumentError(caught, "计划保存失败");
    } finally {
      setBusy(false);
    }
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
    if (!document || !current || !effectivePlanId) return;
    setBusy(true);
    setError("");
    setConflict(null);
    try {
      await deletePlanDocument(
        effectivePlanId,
        {
          expected_version: current.version,
          expected_content_hash: current.content_hash,
        },
        csrfToken,
      );
      setPlanSummaries((items) => items.filter((item) => item.id !== effectivePlanId));
      setSelectedPlanId(null);
      setDocument(null);
      setSourceDocument(null);
      setStructuredPlans(null);
      setProgram(null);
      setShowExecution(false);
      setShowLibrary(true);
      setNotice({ message: "计划已删除", tone: "success" });
      onDeleted?.();
    } catch {
      setNotice({ message: "计划删除失败，请稍后重试", tone: "error" });
    } finally {
      setBusy(false);
      setDeleteOpen(false);
    }
  }

  async function restore(version: PlanDocumentVersion) {
    if (!document || !current || !effectivePlanId) return;
    setBusy(true);
    try {
      const restored = await restorePlanDocument(
        effectivePlanId,
        {
          version: version.version,
          expected_version: current.version,
          expected_content_hash: current.content_hash,
        },
        csrfToken,
      );
      applyDocument({
        ...document,
        title: restored.title,
        current_version_id: restored.id,
        projected_version_id: restored.id,
        file_status: "ready",
        current: restored,
        versions: [...document.versions, restored],
      });
    } catch (caught) {
      handleDocumentError(caught, "计划版本恢复失败");
    } finally {
      setBusy(false);
    }
  }

  async function syncFile() {
    if (!effectivePlanId) return;
    setBusy(true);
    try {
      const synced = await syncPlanFile(effectivePlanId, csrfToken);
      if (document)
        applyDocument({
          ...document,
          current: synced,
          current_version_id: synced.id,
          projected_version_id: synced.id,
          file_status: "ready",
          versions: [...document.versions, synced],
        });
    } catch (caught) {
      handleDocumentError(caught, "计划文件同步失败");
    } finally {
      setBusy(false);
    }
  }

  async function retryProjection() {
    if (!effectivePlanId) return;
    setBusy(true);
    try {
      applyDocument(await retryPlanProjection(effectivePlanId, csrfToken));
    } catch (caught) {
      handleDocumentError(caught, "计划文件重试失败");
    } finally {
      setBusy(false);
    }
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
      handleDocumentError(caught, "版本历史加载失败");
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!run || !structuredPlans?.current || busy) return;
    setBusy(true);
    try {
      onRun(await approvePlan(run.id, structuredPlans.current.version, csrfToken));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "计划批准失败");
    } finally {
      setBusy(false);
    }
  }

  async function revise() {
    if (!run || !structuredPlans?.current || busy || draft.some((step) => !step.title.trim())) return;
    setBusy(true);
    try {
      const next = await revisePlan(
        run.id,
        structuredPlans.current.version,
        draft.map((step) => ({ ...step, title: step.title.trim() })),
        csrfToken,
      );
      setStructuredPlans({
        current: next,
        history: [...(structuredPlans.history ?? []), next],
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "计划调整保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function stopStep(stepId: string) {
    if (!run || busy) return;
    setBusy(true);
    try {
      onRun(await cancelStep(run.id, stepId, csrfToken));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "步骤取消失败");
    } finally {
      setBusy(false);
    }
  }

  function renderStructuredPlan() {
    const structured = structuredPlans?.current;
    if (!run || !structured) return null;
    return (
      <>
        <section className="card plan-card">
          <div className="panel-toolbar">
            <div>
              <span className="eyebrow">当前执行计划</span>
              <h3>{structured.summary || "执行步骤"}</h3>
            </div>
            {structured.status !== "approved" && (
              <button className="button button-primary" type="button" disabled={busy} onClick={() => void approve()}>
                {busy ? "正在…" : "批准计划"}
              </button>
            )}
          </div>
          <ol className="step-list">
            {structured.steps.map((step) => (
              <li className={`step-row step-${step.status}`} key={step.id}>
                <span className="step-number">{step.position + 1}</span>
                <div>
                  <strong>{step.title}</strong>
                  <span>{localizedCode(step.status)}</span>
                </div>
                {step.status === "pending" && (
                  <button className="button button-quiet" type="button" disabled={busy} onClick={() => void stopStep(step.id)}>
                    {busy ? "正在…" : "取消步骤"}
                  </button>
                )}
              </li>
            ))}
          </ol>
        </section>
        <section className="card">
          <div className="panel-toolbar">
            <div>
              <span className="eyebrow">计划调整</span>
              <h3>调整未完成步骤</h3>
            </div>
            <button className="button button-quiet" type="button" onClick={() => setDraft([...draft, { id: `step-${Date.now()}`, title: "" }])}>
              新增步骤
            </button>
          </div>
          <div className="draft-list">
            {draft.map((step, index) => (
              <div className="draft-row" key={step.id}>
                <span>{index + 1}</span>
                <input aria-label={`步骤 ${index + 1}`} value={step.title} onChange={(event) => setDraft(draft.map((item) => (item.id === step.id ? { ...item, title: event.target.value } : item)))} />
                <button className="button button-quiet" type="button" onClick={() => setDraft(draft.filter((item) => item.id !== step.id))}>
                  移除
                </button>
              </div>
            ))}
          </div>
          <div className="form-footer">
            <span className="muted">当前版本 v{structured.version}</span>
            <button className="button button-primary" type="button" disabled={busy || draft.length === 0 || draft.some((step) => !step.title.trim())} onClick={() => void revise()}>
              {busy ? "正在…" : "保存新版本"}
            </button>
          </div>
        </section>
      </>
    );
  }

  const shellProps = {
    embedded,
    plans: planSummaries,
    selectedPlanId: activePlanId,
    busy,
    onSelect: selectPlan,
  };

  if (document && !current) {
    return (
      <PlanShell {...shellProps}>
        <div className="page-stack plan-document-page">
          <section className="plan-document-hero">
            <div>
              <span className="eyebrow">计划文档 · 准备中</span>
              {!embedded&&<h2>{document.title}</h2>}
              <p>
                计划编号 · {document.id} · {document.file_path}
              </p>
            </div>
            <span className={`version-badge file-status-${document.file_status}`}>准备中</span>
          </section>
          {error && (
            <p className="error-message" role="alert">
              {error}
            </p>
          )}
          <section className="card plan-pending-card" role="status">
            <span className="eyebrow">文档生成</span>
            <h3>计划文档还没有可用版本</h3>
            <p>计划地址已经保留。文件写入失败或仍在准备中，可以从这里重试，不需要重新调用模型。</p>
            <button className="button button-primary" disabled={busy} type="button" onClick={() => void retryProjection()}>
              {busy ? "正在重试…" : "重试写入计划文件"}
            </button>
          </section>
        </div>
      </PlanShell>
    );
  }

  if (document && current) {
    const workspaceHref = `/workspace/${encodeURIComponent(document.id)}`;
    const executionHref = linkedProgram ? todayPath(linkedProgram.id) : "#plan-execution-settings";
    return (
      <PlanShell {...shellProps}>
        <div className="page-stack plan-document-page">
          {!embedded&&<nav className="goal-breadcrumb" aria-label="目标路径"><a href={workspaceHref}><ArrowLeft size={16} aria-hidden="true" />目标概览</a></nav>}
          <section className={embedded?"plan-document-controls":"plan-document-hero"} aria-label="计划操作">
            <div>
              {!embedded&&<><span className="eyebrow">计划文档</span><h2>{document.title}</h2></>}
              <p>计划 v{current.version} · {planStatusLabel(document.file_status)}</p>
            </div>
            <div className="plan-hero-actions">
              {editing ? (
                <div className="plan-hero-edit-actions">
                  <span className="plan-hero-action-label">正在编辑</span>
                  <div className="button-row">
                    <button className="button button-secondary" disabled={busy} type="button" onClick={cancelEditing}>
                      取消编辑
                    </button>
                    <button className="button button-primary" disabled={busy || !markdown.trim()} type="button" onClick={() => void saveDocument()}>
                      保存计划
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="plan-hero-statuses" aria-label="计划状态">
                    <span><i className="status-dot status-ready" aria-hidden="true" />计划已保存</span>
                    {linkedProgram && <span className={executionStatusClass(linkedProgram)}><i className="status-dot" aria-hidden="true" />{executionStatusLabel(linkedProgram)}</span>}
                  </div>
                  <p className="plan-hero-action-copy">{linkedProgram ? executionStatusDescription(linkedProgram) : "生成执行预览后，确认的行动才会进入今日行动。"}</p>
                  {linkedProgram ? (
                    <>
                      {linkedProgram.status === "DRAFT" && linkedProgram.compile_status === "READY" && (
                        <button className="button button-primary plan-hero-primary-action" disabled={busy} type="button" onClick={() => void activateProgram()}>
                          {busy ? "正在确认…" : "确认并开始执行"}
                        </button>
                      )}
                      {linkedProgram.status === "DRAFT" && linkedProgram.compile_status === "FAILED" && (
                        <button className="button button-primary plan-hero-primary-action" disabled={busy} type="button" onClick={() => void retryProgramCompile()}>
                          {busy ? "正在重新生成…" : "重新生成执行预览"}
                        </button>
                      )}
                      {linkedProgram.status !== "DRAFT" && <a className="button button-primary plan-hero-primary-action" href={executionHref} onClick={event => { if (onOpenToday && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); onOpenToday(linkedProgram.id); } }}>查看执行<ArrowUpRight size={16} aria-hidden="true" /></a>}
                    </>
                  ) : (
                    <button className="button button-primary plan-hero-primary-action" disabled={busy} type="button" aria-expanded={showExecution} aria-controls="plan-execution-settings" onClick={openExecution}>
                      {busy ? "正在处理…" : "开始执行"}
                    </button>
                  )}
                  <div className="plan-hero-utilities">
                    <button className="button button-quiet" disabled={busy} type="button" onClick={() => setEditing(true)}>
                      编辑计划
                    </button>
                    <button className="button button-quiet button-quiet-danger" disabled={busy} type="button" onClick={() => setDeleteOpen(true)}>
                      删除计划
                    </button>
                  </div>
                </>
              )}
            </div>
          </section>
          {!embedded&&!editing && <nav className="goal-section-nav" aria-label="目标页面"><a href={workspaceHref}>概览</a><a aria-current="page" href={`/plans/${encodeURIComponent(document.id)}`}>计划</a>{linkedProgram ? <a href={executionHref}>执行</a> : <button type="button" onClick={openExecution}>执行</button>}<a href={`${workspaceHref}#goal-review`}>复盘</a><a className="goal-conversation-link" href={`/threads/${encodeURIComponent(document.thread_id)}`}><MessageSquare size={16} aria-hidden="true" />继续对话</a></nav>}
          {showExecution && (
            <section ref={executionPanelRef} id="plan-execution-settings" tabIndex={-1} className="card program-preview-card" aria-label="执行设置" aria-busy={busy}>
              <div className="panel-toolbar">
                <div>
                  <span className="eyebrow">执行计划</span>
                  <h3>{program?.status === "ACTIVE" ? "当前执行版本" : "生成执行预览"}</h3>
                </div>
                {program && (
                  <span className="version-badge">
                    {localizedCode(program.status)} · v{program.version}
                  </span>
                )}
              </div>
              {!program ? (
                <div className="program-preview-form">
                  <label>
                    开始日期
                    <input aria-label="执行开始日期" type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
                  </label>
                  <label>
                    结束日期（可选）
                    <input aria-label="执行结束日期" min={startDate} max={latestEndDate} type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
                  </label>
                  <label>
                    时区
                    <input aria-label="执行时区" value={timezone} onChange={(event) => setTimezone(event.target.value)} />
                  </label>
                  <label>
                    每日分钟
                    <input aria-label="每日可用分钟" min="5" max="1440" type="number" value={dailyMinutes} onChange={(event) => setDailyMinutes(Number(event.target.value))} />
                  </label>
                  <fieldset className="program-weekdays"><legend>可用学习日</legend>{["周一","周二","周三","周四","周五","周六","周日"].map((label,index)=><label key={label}><input type="checkbox" checked={availableWeekdays.includes(index)} onChange={event=>setAvailableWeekdays(days=>event.target.checked?[...days,index].sort():days.filter(day=>day!==index))}/>{label}</label>)}</fieldset>
                  <div className="program-excluded-dates"><label>额外休息日期<input aria-label="额外休息日期" type="date" min={startDate} max={endDate||latestEndDate} value={excludedDate} onChange={event=>setExcludedDate(event.target.value)}/></label><button type="button" className="button button-secondary" disabled={!excludedDate||excludedDates.includes(excludedDate)} onClick={()=>{setExcludedDates(dates=>[...dates,excludedDate].sort());setExcludedDate("");}}>添加休息日</button>{excludedDates.map(day=><label key={day}><input type="checkbox" checked onChange={()=>setExcludedDates(dates=>dates.filter(value=>value!==day))}/>{day}</label>)}</div>
                  <button className="button button-primary" disabled={busy || !startDate || !availableWeekdays.length || Boolean(endDate && (endDate < startDate || endDate > latestEndDate))} type="button" onClick={() => void createProgramPreview()}>
                    {busy ? "正在编译…" : "生成预览"}
                  </button>
                </div>
              ) : (
                <>
                  <p>
                    来源文档 v{current.version} · {program.source_plan_content_hash}
                  </p>
                  <p>
                    {program.start_date} — {program.end_date} · {program.timezone} · 每日预算 {program.daily_minutes} 分钟
                  </p>
                  {program.calendar&&<p>{program.calendar.natural_days} 个自然日 · {program.calendar.study_days} 个可用学习日 · {program.calendar.rest_dates.length} 个休息日</p>}
                  {program.compile_status === "FAILED" ? (
                    <div className="error-message" role="alert">
                      <span>
                        执行预览生成失败（
                        {program.compile_error_code || "COMPILE_FAILED"}）
                      </span>
                      <button className="button button-secondary" disabled={busy} type="button" onClick={() => void retryProgramCompile()}>
                        {busy ? "正在重试…" : "重新生成预览"}
                      </button>
                    </div>
                  ) : (
                    <>
                      {program.structure?.assumptions.length ? (
                        <ul>
                          {program.structure.assumptions.map((item) => (
                            <li key={item}>{item}</li>
                          ))}
                        </ul>
                      ) : null}
                      <div className="program-calendar">
                        {program.structure?.actions.map((action) => (
                          <article key={action.logical_key}>
                            <span>{action.scheduled_date}</span>
                            <strong>{action.title}</strong>
                            <small>{action.estimated_minutes} 分钟</small>
                          </article>
                        ))}
                      </div>
                      {program.status === "DRAFT" && program.compile_status === "READY" && (
                        <button className="button button-primary" disabled={busy} type="button" onClick={() => void activateProgram()}>
                          {busy ? "正在激活…" : "确认并激活"}
                        </button>
                      )}
                    </>
                  )}
                  <p className="muted">原计划文档与执行版本相互独立；继续编辑正文不会静默改变已激活安排。</p>
                </>
              )}
            </section>
          )}
          {linkedProgram && (
            <section className="plan-linked-program">
              <div>
                <span className="eyebrow">关联执行计划</span>
                <strong>{linkedProgram.objective_title}</strong>
                <p>
                  必做进度 {linkedProgram.progress.required_completed}/{linkedProgram.progress.required_total} · {linkedProgram.start_date} 至 {linkedProgram.end_date}
                </p>
              </div>
              <span>{localizedCode(linkedProgram.status)}</span>
            </section>
          )}
          {error && (
            <div className="error-message" role="alert">
              <span>{error}</span>
              {document.file_status === "failed" && (
                <button className="button button-danger" type="button" onClick={() => void retryProjection()}>
                  重试写入计划文件
                </button>
              )}
            </div>
          )}
          {conflict && (
            <section className="plan-conflict" role="status">
              <strong>检测到更新的服务端版本</strong>
              <p>你的草稿已保留。请比较服务端内容后重新加载或手动合并。</p>
              <div className="plan-conflict-preview">
                <MarkdownMessage content={String(conflict.markdown ?? "")} />
              </div>
            </section>
          )}
          {editing ? (
            <section className="card plan-editor-card">
              <PlanVisualEditor title={title} onTitleChange={setTitle} markdown={markdown} onChange={setMarkdown} disabled={busy} />
            </section>
          ) : (
            <section className="plan-readable-card" aria-label="计划正文">
              <MarkdownMessage content={markdown} />
            </section>
          )}
          <details className="plan-history-card">
            <summary>版本历史 · {history.length} 个版本</summary>
            <div className="panel-toolbar">
              <div className="button-row">
                <button className="button button-secondary" disabled={busy} type="button" onClick={() => void syncFile()}>
                  同步计划文件
                </button>
                <button className="button button-quiet" disabled={busy} type="button" onClick={() => void retryProjection()}>
                  重试文件写入
                </button>
              </div>
            </div>
            <div className="history-list">
              {history.map((version) => (
                <div className={selectedVersion === version.version ? "history-row history-row-selected" : "history-row"} key={version.id}>
                  <button className="history-version" disabled={busy} type="button" onClick={() => void selectHistoryVersion(version)}>
                    v{version.version}
                  </button>
                  <span>{localizedCode(version.actor, "系统")}</span>
                  <span>{version.change_summary || "无变更摘要"}</span>
                  <span>{version.content_hash.slice(0, 18)}</span>
                  {version.status === "committed" && version.version !== current.version && (
                    <button className="button button-quiet" disabled={busy} type="button" aria-label={`恢复版本 ${version.version}`} onClick={() => void restore(version)}>
                      恢复
                    </button>
                  )}
                </div>
              ))}
            </div>
          </details>
          {renderStructuredPlan()}
          <ConfirmDialog open={deleteOpen} title="删除计划？" description={`确定删除“${document.title}”吗？计划内容及其历史版本将无法恢复。`} busy={busy} onCancel={() => setDeleteOpen(false)} onConfirm={() => void deleteDocument()} />
          {notice && <AppToast message={notice.message} tone={notice.tone} action={notice.action} durationMs={notice.durationMs} onDismiss={() => setNotice(null)} />}
        </div>
      </PlanShell>
    );
  }

  if (showLibrary || (!run && !threadId && !activePlanId))
    return (
      <>
        <PlanShell {...shellProps}>
          <section className="empty-panel plan-empty-state" aria-label="计划详情占位区域">
            <span className="eyebrow">计划库</span>
            <h2>{planSummaries.length ? "选择一个计划" : "还没有已保存计划"}</h2>
            <p>{planSummaries.length ? "从左侧选择计划名称，查看计划内容和可编辑详情。" : "保存计划后，它们会显示在左侧列表中。"}</p>
          </section>
        </PlanShell>
        {notice && <AppToast message={notice.message} tone={notice.tone} action={notice.action} durationMs={notice.durationMs} onDismiss={() => setNotice(null)} />}
      </>
    );
  if (!document && threadId)
    return (
      <PlanShell {...shellProps}>
        <section className="empty-panel">
          <span className="eyebrow">计划文档</span>
          <h2>当前对话还没有计划</h2>
          <p>{error || "模型明确保存计划后，文档会出现在这里。"}</p>
        </section>
      </PlanShell>
    );

  const structured = structuredPlans?.current;
  const sourceVersionId = structured?.source_document_version_id ?? run?.source_plan_document_version_id ?? null;
  const sourceVersion = sourceVersionId ? (sourceDocument?.versions.find((version) => version.id === sourceVersionId) ?? null) : null;
  return (
    <PlanShell {...shellProps}>
      <div className="page-stack">
        {sourceVersion && (
          <p className="plan-source-reference">
            执行来源：{sourceVersion.title} · 文档 v{sourceVersion.version}
            {sourceDocument?.current && sourceDocument.current.version !== sourceVersion.version ? ` · 当前文档 v${sourceDocument.current.version}` : ""}
          </p>
        )}
        {error && (
          <p className="error-message" role="alert">
            {error}
          </p>
        )}
        {!structured ? <p className="empty-state">等待运行时生成结构化执行计划。</p> : renderStructuredPlan()}
      </div>
    </PlanShell>
  );
}
