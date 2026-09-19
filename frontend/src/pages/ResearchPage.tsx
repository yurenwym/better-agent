import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Clock3, Plus, Trash2 } from "lucide-react";
import {
  cancelResearch, createResearch, createThread, deleteResearch, getResearchJob,
  getResearchJobs, getResearchReport, retryResearch,
} from "../api";
import type { ResearchJob } from "../types";
import MarkdownMessage from "../components/MarkdownMessage";
import ResearchSourcesPanel from "../components/ResearchSourcesPanel";
import ResearchProgressCard from "../components/ResearchProgressCard";
import ConfirmDialog from "../components/ConfirmDialog";
import AppToast from "../components/AppToast";
const statusLabels: Record<ResearchJob["status"], string> = {
  QUEUED: "等待开始", RUNNING: "研究中", COMPLETED: "已完成", PARTIAL: "部分完成", FAILED: "失败", CANCELLED: "已取消",
};
const suggestions = [
  "调研 AI Agent 开发岗秋招：在招公司、岗位要求与投递渠道",
  "梳理大模型应用开发岗位面试高频考点与准备路径",
  "对比主流向量数据库的选型要点与适用场景",
];

interface ResearchPageProps {
  csrfToken: string;
  jobId?: string | null;
  onSelectJob?: (id: string | null) => void;
  onOpenSchedules?: () => void;
}

export default function ResearchPage({ csrfToken, jobId, onSelectJob, onOpenSchedules }: ResearchPageProps) {
  const [jobs, setJobs] = useState<ResearchJob[]>([]);
  const [internalId, setInternalId] = useState<string | null>(null);
  const selectedId = jobId === undefined ? internalId : jobId;
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const [selected, setSelected] = useState<ResearchJob | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [listError, setListError] = useState("");
  const [selectionError, setSelectionError] = useState("");
  const [selectionLoading, setSelectionLoading] = useState(false);
  const [actionError, setActionError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [selectionRefresh, setSelectionRefresh] = useState(0);
  const [report, setReport] = useState<{ jobId: string; markdown: string } | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState("");
  const [reportRefresh, setReportRefresh] = useState(0);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [researchToDelete, setResearchToDelete] = useState<ResearchJob | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState<{ message: string; tone: "success" | "error" } | null>(null);
  const [starting, setStarting] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [topic, setTopic] = useState("");
  const [includeNotes, setIncludeNotes] = useState(false);

  useEffect(() => {
    let active = true;
    let pending = false;
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const result = await getResearchJobs();
        if (active) { setJobs(result.jobs); setListError(""); }
      } catch {
        if (active) setListError("暂时无法加载研究记录，请稍后重试。");
      } finally {
        pending = false;
        if (active) setLoaded(true);
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [refresh]);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    setSelected(null);
    setSelectionError("");
    setActionError("");
    setSourcesOpen(false);
    setSelectionLoading(Boolean(selectedId));
    if (!selectedId) return;
    const id = selectedId;
    async function load() {
      try {
        const next = await getResearchJob(id);
        if (!active) return;
        setSelected(next);
        setSelectionError("");
        if (next.status === "QUEUED" || next.status === "RUNNING") timer = window.setTimeout(() => void load(), 2000);
      } catch {
        if (active) setSelectionError("这份研究暂时无法加载，请重试或返回研究记录。");
      } finally {
        if (active) setSelectionLoading(false);
      }
    }
    void load();
    return () => { active = false; window.clearTimeout(timer); };
  }, [selectedId, selectionRefresh]);

  const selectedJobId = selected?.id;
  const selectedStatus = selected?.status;
  useEffect(() => {
    let active = true;
    setReport(null);
    setReportError("");
    const ready = selectedStatus === "COMPLETED" || selectedStatus === "PARTIAL";
    setReportLoading(Boolean(selectedJobId && ready));
    if (!selectedJobId || !ready) return;
    const id = selectedJobId;
    void getResearchReport(id)
      .then(result => { if (active) setReport({ jobId: id, markdown: result.markdown }); })
      .catch(() => { if (active) setReportError("报告暂时无法加载，请稍后重试。"); })
      .finally(() => { if (active) setReportLoading(false); });
    return () => { active = false; };
  }, [selectedJobId, selectedStatus, reportRefresh]);

  function selectJob(id: string | null) {
    setInternalId(id);
    setActionError("");
    onSelectJob?.(id);
  }

  async function start() {
    const value = topic.trim();
    if (!value || starting) return;
    setStarting(true);
    setActionError("");
    try {
      const thread = await createThread({ title: value.slice(0, 80) }, csrfToken);
      const created = await createResearch(thread.id, {
        topic: value, client_request_id: crypto.randomUUID(), source_scopes: includeNotes ? ["web", "local_note"] : ["web"],
      }, csrfToken);
      selectJob(created.job_id);
      setRefresh(value => value + 1);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "研究创建失败，请稍后重试。");
    } finally { setStarting(false); }
  }

  async function retry(job: ResearchJob) {
    if (retrying) return;
    setRetrying(true);
    setActionError("");
    try {
      const created = await retryResearch(job.id, csrfToken);
      if (selectedIdRef.current === job.id) selectJob(created.job_id);
      setRefresh(value => value + 1);
    } catch {
      if (selectedIdRef.current === job.id) setActionError("重新研究失败，请稍后再试。");
    } finally { setRetrying(false); }
  }

  async function cancel(job: ResearchJob) {
    if (cancelling) return;
    setCancelling(true);
    setActionError("");
    try {
      const next = await cancelResearch(job.id, csrfToken);
      setSelected(current => current?.id === job.id ? next : current);
      setRefresh(value => value + 1);
    } catch {
      if (selectedIdRef.current === job.id) setActionError("取消研究失败，请稍后重试。");
    } finally { setCancelling(false); }
  }

  async function remove(job: ResearchJob) {
    setDeleting(true);
    try {
      await deleteResearch(job.id, csrfToken);
      setJobs(items => items.filter(item => item.id !== job.id));
      if (selectedIdRef.current === job.id) selectJob(null);
      setNotice({ message: "研究已删除", tone: "success" });
    } catch {
      setNotice({ message: "研究删除失败。正在运行的研究需要先取消。", tone: "error" });
    } finally { setDeleting(false); setResearchToDelete(null); }
  }

  const current = selected?.id === selectedId ? selected : null;
  const visibleJobs = current
    ? jobs.some(job => job.id === current.id) ? jobs.map(job => job.id === current.id ? current : job) : [current, ...jobs]
    : jobs;
  const hasReport = current?.status === "COMPLETED" || current?.status === "PARTIAL";
  const progress = current && <ResearchProgressCard
    job={current}
    onCancel={!current.cancel_requested_at && !cancelling ? () => void cancel(current) : undefined}
    onRetry={!retrying ? () => void retry(current) : undefined}
    onDelete={!hasReport ? () => setResearchToDelete(current) : undefined}
    deleteBusy={deleting}
  />;

  return <div className={`research-page${selectedId ? " research-has-selection" : ""}`}>
    <aside className="research-list" aria-label="研究历史">
      <header className="research-list-head">
        <div><h2>研究记录</h2><span className="muted">{visibleJobs.length} 份研究</span></div>
        <button className="button button-secondary" type="button" onClick={() => selectJob(null)}><Plus size={16} aria-hidden="true" />新研究</button>
      </header>
      <div className="research-list-actions">
        {onOpenSchedules
          ? <button className="button button-quiet" type="button" onClick={onOpenSchedules}><Clock3 size={16} aria-hidden="true" />定时研究</button>
          : <a className="button button-quiet" href="/schedules"><Clock3 size={16} aria-hidden="true" />定时研究</a>}
      </div>
      {listError && <p className="research-page-error" role="alert">{listError} <button className="button button-quiet" type="button" onClick={() => setRefresh(value => value + 1)}>重新加载记录</button></p>}
      <div className="research-list-scroll">
        {!loaded ? <p className="muted" role="status" aria-busy="true">正在加载研究记录…</p>
          : visibleJobs.length === 0 && !listError ? <div className="research-list-empty"><strong>还没有研究记录</strong></div>
          : visibleJobs.map(job => <button className={`research-list-item${selectedId === job.id ? " active" : ""}`} aria-pressed={selectedId === job.id} key={job.id} onClick={() => selectJob(job.id)} type="button">
            <strong title={job.title || job.topic}>{job.title || job.topic}</strong>
            <span className="research-record-meta"><span className={`research-list-status status-${job.status.toLowerCase()}`}>{statusLabels[job.status]}</span><time dateTime={job.created_at} title={new Date(job.created_at).toLocaleString("zh-CN")}>{new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(job.created_at))}</time></span>
            <span className="research-record-evidence">{job.source_count} 个来源 · {job.evidence_count} 条证据</span>
          </button>)}
      </div>
    </aside>
    <section className="research-detail" aria-label="研究详情">
      <div className="research-detail-content">
      {selectedId && <div className="research-detail-toolbar">
        <button className="button button-quiet research-view-back" type="button" onClick={() => selectJob(null)}><ArrowLeft size={16} aria-hidden="true" />返回研究记录</button>
        {hasReport && current && <button className="button button-quiet" type="button" aria-label="删除研究" title="删除研究" disabled={deleting} onClick={() => setResearchToDelete(current)}><Trash2 size={16} aria-hidden="true" /></button>}
      </div>}
      {actionError && <p className="research-page-error" role="alert">{actionError}</p>}
      {selectedId ? <>
        {selectionLoading && <p className="research-report-loading" role="status">正在加载研究…</p>}
        {selectionError && <p className="research-page-error" role="alert">{selectionError} <button className="button button-secondary" type="button" onClick={() => setSelectionRefresh(value => value + 1)}>重新加载研究</button></p>}
        {current && <>
          {hasReport ? <>
            <header className="research-result-heading"><strong>{current.title || current.topic}</strong><span className={`state-pill state-${current.status === "PARTIAL" ? "blocked" : "completed"}`}>{statusLabels[current.status]}</span></header>
            {current.status === "PARTIAL" && <div className="research-failure" role="status"><strong>研究已部分完成</strong><p>已支持的结论可以查看；缺少证据的要求没有被冒充为完整结果。</p>{Boolean(current.missing_requirements?.length) && <p>仍缺少：{current.missing_requirements?.join("、")}</p>}</div>}
            {reportLoading && <p className="research-report-loading" role="status">正在加载报告…</p>}
            {reportError && <p className="research-page-error" role="alert">{reportError} <button className="button button-secondary" type="button" onClick={() => setReportRefresh(value => value + 1)}>重新加载报告</button></p>}
            {report?.jobId === current.id && <article className="research-report" aria-label="研究报告">{report.markdown.trim() ? <MarkdownMessage content={report.markdown} /> : <p role="status">这份研究尚无可显示的报告正文。</p>}</article>}
          </> : progress}
          {retrying && <p role="status">正在重新研究…</p>}
          {(cancelling || current.cancel_requested_at && ["QUEUED", "RUNNING"].includes(current.status)) && <p role="status">正在取消研究…</p>}
          <details key={current.id} className="research-source-disclosure" onToggle={event => setSourcesOpen(event.currentTarget.open)}>
            <summary>来源与证据 <span>{current.source_count} 个来源 · {current.evidence_count} 条证据</span></summary>
            {sourcesOpen && <ResearchSourcesPanel jobId={current.id} traceability={current.traceability} />}
          </details>
          {hasReport && <details className="research-process-disclosure"><summary>运行详情</summary>{progress}</details>}
        </>}
      </> : <div className="research-launcher">
        <div className="research-launcher-heading"><h2>新研究</h2></div>
        <form className="research-launcher-form" onSubmit={event => { event.preventDefault(); void start(); }}>
          <label htmlFor="research-topic">研究主题</label>
          <textarea id="research-topic" maxLength={2000} placeholder="例如：调研 AI Agent 开发岗秋招的在招公司、岗位要求与投递链接" value={topic} onChange={event => setTopic(event.target.value)} />
          <label className="research-source-toggle"><input type="checkbox" checked={includeNotes} onChange={event => setIncludeNotes(event.target.checked)} /><span>同时检索本地资料</span></label>
          <button className="button button-primary research-start" disabled={starting || !topic.trim()} type="submit">{starting ? "正在创建研究…" : "开始研究"}</button>
        </form>
        <div className="research-suggestions"><span>常用主题</span>{suggestions.map(item => <button key={item} type="button" onClick={() => setTopic(item)}>{item}</button>)}</div>
      </div>}
      </div>
    </section>
    <ConfirmDialog open={Boolean(researchToDelete)} title="删除研究？" description={`确定删除“${researchToDelete?.title || researchToDelete?.topic || ""}”吗？研究报告、来源和证据将一并删除。`} busy={deleting} onCancel={() => setResearchToDelete(null)} onConfirm={() => { if (researchToDelete) void remove(researchToDelete); }} />
    {notice && <AppToast message={notice.message} tone={notice.tone} onDismiss={() => setNotice(null)} />}
  </div>;
}
