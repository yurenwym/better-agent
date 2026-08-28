import { useEffect, useState } from "react";
import { cancelEvaluationRun, createEvaluationRun, getEvaluationEvents, getEvaluationReport, getEvaluationRun, listEvaluationSuites, listModelProfiles, resumeEvaluationRun, subscribeToEvaluationEvents } from "../api";
import type { EvaluationProgressEvent, EvaluationReport, EvaluationRunRecord, ModelProfileVersion } from "../types";
import ConfirmDialog from "../components/ConfirmDialog";
import AppToast from "../components/AppToast";

const emptyForm = {
  suite_id: "",
  baseline_bundle_id: "",
  candidate_bundle_id: "",
  baseline_model_id: "",
  candidate_model_id: "",
  quality_judge_model_id: "",
  safety_judge_model_id: "",
  budget_microusd: "1000000",
  primary_objective: "quality",
  objective_threshold: "0",
};

export default function EvaluationPage({ evaluationId, csrfToken }: { evaluationId: string | null; csrfToken: string }) {
  const [run, setRun] = useState<EvaluationRunRecord | null>(null),
    [events, setEvents] = useState<EvaluationProgressEvent[]>([]),
    [report, setReport] = useState<EvaluationReport | null>(null);
  const [error, setError] = useState(""),
    [confirm, setConfirm] = useState(false),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState<{
      message: string;
      tone: "success" | "error";
    } | null>(null);
  const [resumeBudget, setResumeBudget] = useState("");
  const [suites, setSuites] = useState<Array<{ id: string; digest: string; kind: string; case_count: number }>>([]),
    [models, setModels] = useState<ModelProfileVersion[]>([]),
    [form, setForm] = useState(emptyForm);
  const caseEvents = events.filter((event) => event.type === "evaluation.case.finished");

  useEffect(() => {
    if (evaluationId) {
      Promise.all([getEvaluationRun(evaluationId), getEvaluationEvents(evaluationId)])
        .then(([r, e]) => {
          setRun(r);
          setEvents(e.events);
          if (r.status === "COMPLETED")
            void getEvaluationReport(evaluationId)
              .then(setReport)
              .catch(() => undefined);
        })
        .catch((e) => setError(e instanceof Error ? e.message : "评测加载失败"));
      return;
    }
    Promise.all([listEvaluationSuites(), listModelProfiles()])
      .then(([suiteData, profileData]) => {
        setSuites(suiteData.suites.filter((item) => item.kind === "release"));
        setModels(profileData.profiles.flatMap((item) => item.versions).filter((item) => item.status === "ACTIVE"));
        setForm((current) => ({
          ...current,
          suite_id: current.suite_id || suiteData.suites.find((item) => item.kind === "release")?.id || "",
        }));
      })
      .catch((e) => setError(e instanceof Error ? e.message : "评测配置加载失败"));
  }, [evaluationId]);
  useEffect(() => {
    if (!evaluationId || !run || !["QUEUED", "RUNNING"].includes(run.status)) return;
    const refresh = () =>
      void getEvaluationRun(evaluationId)
        .then((next) => {
          setRun(next);
          if (next.status === "COMPLETED")
            void getEvaluationReport(evaluationId)
              .then(setReport)
              .catch(() => undefined);
        })
        .catch(() => undefined);
    const close = subscribeToEvaluationEvents(evaluationId, events.at(-1)?.seq ?? 0, (event) => {
      setEvents((current) => (current.some((item) => item.seq === event.seq) ? current : [...current, event]));
      refresh();
    });
    const timer = window.setInterval(refresh, 2000);
    return () => {
      close();
      window.clearInterval(timer);
    };
  }, [evaluationId, run?.status]);

  async function start(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const created = await createEvaluationRun(
        {
          ...form,
          budget_microusd: Number(form.budget_microusd),
          objective_threshold: Number(form.objective_threshold),
        },
        csrfToken,
      );
      setNotice({ message: "真实评测已进入队列", tone: "success" });
      window.history.pushState({}, "", `/evaluations/${created.id}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    } catch (e) {
      setNotice({
        message: e instanceof Error ? e.message : "评测启动失败",
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  }
  async function cancel() {
    if (!evaluationId) return;
    try {
      const next = await cancelEvaluationRun(evaluationId, csrfToken);
      setRun(next);
      setNotice({ message: "评测取消请求已提交", tone: "success" });
    } catch (e) {
      setNotice({
        message: e instanceof Error ? e.message : "取消评测失败",
        tone: "error",
      });
    } finally {
      setConfirm(false);
    }
  }
  async function resume() {
    if (!evaluationId) return;
    const value = Number(resumeBudget);
    if (!Number.isInteger(value)) {
      setNotice({ message: "请输入有效的整数预算", tone: "error" });
      return;
    }
    setBusy(true);
    try {
      const next = await resumeEvaluationRun(evaluationId, value, csrfToken);
      setRun(next);
      setNotice({ message: "预算已追加，评测将从断点恢复", tone: "success" });
    } catch (e) {
      setNotice({
        message: e instanceof Error ? e.message : "恢复评测失败",
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  }
  const choose = (key: keyof typeof emptyForm) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [key]: e.target.value });

  if (!evaluationId)
    return (
      <section className="control-page">
        <header className="control-hero">
          <div>
            <span className="eyebrow">REAL EVALUATION</span>
            <h2>真实配对评测</h2>
            <p>冻结 60 个 Case，以匿名 A/B、独立质量 Judge 和安全 Judge 判断候选能否进入审批。</p>
          </div>
        </header>
        {error && (
          <div className="control-alert" role="alert">
            {error}
          </div>
        )}
        <section className="control-panel control-create">
          <div className="control-panel-head">
            <div>
              <span className="eyebrow">NEW RUN</span>
              <h3>启动冻结评测</h3>
            </div>
          </div>
          <p className="control-helper">四个角色必须使用四个不同的模型版本；评测会消耗真实模型预算。</p>
          <form className="control-form" onSubmit={start}>
            <label>
              评测套件
              <select required value={form.suite_id} onChange={choose("suite_id")}>
                <option value="">请选择</option>
                {suites.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.id} · {item.case_count} Case
                  </option>
                ))}
              </select>
            </label>
            <label>
              基线 Bundle
              <input required value={form.baseline_bundle_id} onChange={choose("baseline_bundle_id")} />
            </label>
            <label>
              候选 Bundle
              <input required value={form.candidate_bundle_id} onChange={choose("candidate_bundle_id")} />
            </label>
            {(
              [
                ["基线模型", "baseline_model_id"],
                ["候选模型", "candidate_model_id"],
                ["质量 Judge", "quality_judge_model_id"],
                ["安全 Judge", "safety_judge_model_id"],
              ] as const
            ).map(([label, key]) => (
              <label key={key}>
                {label}
                <select required value={form[key]} onChange={choose(key)}>
                  <option value="">请选择</option>
                  {models.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.profile_name} · {item.model_name}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <label>
              评测预算（microusd）
              <input required type="number" min="1" value={form.budget_microusd} onChange={choose("budget_microusd")} />
            </label>
            <label>
              主要目标
              <select value={form.primary_objective} onChange={choose("primary_objective")}>
                <option value="quality">质量</option>
                <option value="cost">成本</option>
                <option value="latency">延迟</option>
              </select>
            </label>
            <label>
              预声明改善阈值
              <input required type="number" step="0.01" min="0" value={form.objective_threshold} onChange={choose("objective_threshold")} />
            </label>
            <button className="button button-primary control-form-action" disabled={busy || !csrfToken}>
              {busy ? "正在排队…" : "启动真实评测"}
            </button>
          </form>
        </section>
        {notice && <AppToast {...notice} onDismiss={() => setNotice(null)} />}
      </section>
    );

  return (
    <section className="control-page">
      <header className="control-hero">
        <div>
          <span className="eyebrow">PAIRED RELEASE</span>
          <h2>真实配对评测</h2>
          <p>{run?.suite_id ?? "正在读取评测配置"} · 匿名双臂 · 独立质量与安全 Judge</p>
        </div>
        {run && (
          <div className="control-actions">
            <span className={`status-chip status-${run.status.toLowerCase()}`}>{run.status}</span>
            {["QUEUED", "RUNNING"].includes(run.status) && (
              <button className="button button-danger" onClick={() => setConfirm(true)}>
                取消评测
              </button>
            )}
          </div>
        )}
      </header>
      {error && (
        <div className="control-alert" role="alert">
          {error}
        </div>
      )}
      {run && (
        <>
          <div className="metric-grid">
            <article>
              <span>完成 Case</span>
              <strong>{caseEvents.length} / 60</strong>
              <small>DEV 20 · HOLDOUT 30 · SAFETY 10</small>
            </article>
            <article>
              <span>运行尝试</span>
              <strong>{run.attempts}</strong>
              <small>lease 恢复不会重复已完成 Case</small>
            </article>
            <article>
              <span>安全门禁</span>
              <strong>{report ? `${report.safety.passed}/10` : "等待"}</strong>
              <small>必须 10 / 10 通过</small>
            </article>
            <article>
              <span>发布结论</span>
              <strong>{report ? (report.release_eligible ? "可审批" : "未通过") : "进行中"}</strong>
              <small>{report ? `报告 ${report.report_digest.slice(0, 8)}` : "完成后生成不可变报告"}</small>
            </article>
          </div>
          <div className="control-grid evaluation-grid">
            <section className="control-panel">
              <div className="control-panel-head">
                <div>
                  <span className="eyebrow">PROGRESS</span>
                  <h3>Case 进度</h3>
                </div>
                <span className="control-count">{caseEvents.length}</span>
              </div>
              <div className="eval-progress">
                <progress max="60" value={caseEvents.length} />
                <div className="eval-domain-grid">
                  {["conversation", "plan", "research", "tool", "memory"].map((d) => (
                    <span key={d}>
                      <strong>{caseEvents.filter((e) => e.domain === d).length}</strong>
                      {d}
                    </span>
                  ))}
                </div>
              </div>
            </section>
            <section className="control-panel">
              <div className="control-panel-head">
                <div>
                  <span className="eyebrow">GATES</span>
                  <h3>发布门禁</h3>
                </div>
              </div>
              {report ? (
                <div className="gate-list">
                  <span className={report.deterministic.failures === 0 ? "gate-pass" : "gate-fail"}>确定性硬检查 {report.deterministic.passed}/120</span>
                  <span className={report.holdout.evidence_sufficient ? "gate-pass" : "gate-wait"}>HOLDOUT 非平局证据 {report.holdout.non_ties}/20</span>
                  <span className={report.safety.failures === 0 ? "gate-pass" : "gate-fail"}>SAFETY {report.safety.passed}/10</span>
                  <span className={report.statistics.primary_objective.passed ? "gate-pass" : "gate-fail"}>
                    主要目标 {report.statistics.primary_objective.name}：{report.statistics.primary_objective.estimate.toFixed(3)}
                    ，95% CI [{report.statistics.primary_objective.ci95.map((v) => v.toFixed(3)).join(", ")}
                    ]，阈值 {report.statistics.primary_objective.threshold}
                  </span>
                  <span>
                    TTFT P95：基线 {report.statistics.latency.baseline_p95.toFixed(3)}s · 候选 {report.statistics.latency.candidate_p95.toFixed(3)}s
                  </span>
                  <span className={report.release_eligible ? "gate-pass" : "gate-fail"}>综合发布门禁</span>
                </div>
              ) : (
                <div className="control-empty compact">
                  <h3>等待评测完成</h3>
                  <p>门禁仅使用持久化 Case 与 Judge 结果重建。</p>
                </div>
              )}
            </section>
          </div>
        </>
      )}
      {run?.status === "BUDGET_BLOCKED" && (
        <section className="control-panel control-create">
          <div className="control-panel-head">
            <div>
              <span className="eyebrow">BUDGET BLOCKED</span>
              <h3>追加预算后从断点恢复</h3>
            </div>
          </div>
          <div className="control-form">
            <label>
              新预算（必须高于 {run.budget_microusd}）
              <input type="number" min={run.budget_microusd + 1} value={resumeBudget} onChange={(event) => setResumeBudget(event.target.value)} />
            </label>
            <button className="button button-primary control-form-action" disabled={busy || !csrfToken} onClick={() => void resume()}>
              追加预算并恢复
            </button>
          </div>
        </section>
      )}
      <ConfirmDialog open={confirm} title="取消评测？" description="正在执行的 Case 会在当前模型请求结束后停止；已完成 Case 会保留用于审计。" confirmLabel="确认取消" onCancel={() => setConfirm(false)} onConfirm={() => void cancel()} />
      {notice && <AppToast {...notice} onDismiss={() => setNotice(null)} />}
    </section>
  );
}
