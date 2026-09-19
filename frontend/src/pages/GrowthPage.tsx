import { useEffect, useState } from "react";
import {
  approveEvolutionCandidate, evaluateEvolutionCandidate, listEvolutionCandidates,
  promoteEvolutionCandidate, rejectEvolutionCandidate, rollbackEvolutionCandidate, startEvolutionCanary, getGrowthProfile,
} from "../api";
import EvolutionCandidateCard from "../components/EvolutionCandidateCard";
import LearningPanel from "../components/LearningPanel";
import type { EvolutionCandidate, GrowthProfile } from "../types";
import { localizedCode } from "../localization";

interface GrowthPageProps {
  csrfToken: string;
  view?: "personal" | "agent";
  onViewChange?: (view: "personal" | "agent") => void;
}

export default function GrowthPage({ csrfToken, view, onViewChange }: GrowthPageProps) {
  const [internalView, setInternalView] = useState<"personal" | "agent">("personal");
  const currentView = view ?? internalView;
  const [candidates, setCandidates] = useState<EvolutionCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [profile, setProfile] = useState<GrowthProfile | null>(null);
  const [profileLoading, setProfileLoading] = useState(true);
  const [profileError, setProfileError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const stages = ["经验", "候选", "评测", "审批", "Canary", "晋升"];
  const stageIndex = (status: EvolutionCandidate["status"]) => ({
    DRAFT: 0, READY_FOR_EVAL: 1, EVALUATING: 2, EVALUATED: 2, PENDING_APPROVAL: 3,
    APPROVED: 3, CANARY: 4, PROMOTED: 5, FAILED: 2, REJECTED: 3, ROLLED_BACK: 4,
  }[status] ?? 0);
  const furthestStage = candidates.reduce((current, candidate) => Math.max(current, stageIndex(candidate.status)), 0);
  const currentStage = candidates.reduce<number | null>((current, candidate) =>
    ["REJECTED", "ROLLED_BACK", "FAILED"].includes(candidate.status) ? current : Math.max(current ?? 0, stageIndex(candidate.status)), null);
  const rolledBackCount = candidates.filter(candidate => candidate.status === "ROLLED_BACK").length;

  useEffect(() => {
    let active = true;
    let pending = false;
    setLoadError("");
    setProfileError("");
    if (currentView === "personal") setProfileLoading(true);
    else setLoading(true);
    async function load() {
      if (pending) return;
      pending = true;
      try {
        if (currentView === "personal") {
          const nextProfile = await getGrowthProfile();
          if (active) { setProfile(nextProfile); setProfileError(""); }
        } else {
          const evolution = await listEvolutionCandidates();
          if (active) { setCandidates(evolution.candidates); setLoadError(""); }
        }
      } catch (caught) {
        if (active) {
          const message = caught instanceof Error ? caught.message : "记录暂时无法加载，请重试。";
          if (currentView === "personal") setProfileError(message);
          else setLoadError(message);
        }
      } finally {
        pending = false;
        if (active) {
          if (currentView === "personal") setProfileLoading(false);
          else setLoading(false);
        }
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [currentView, refresh]);

  function selectView(next: "personal" | "agent") {
    setInternalView(next);
    setError("");
    setNotice("");
    onViewChange?.(next);
  }

  async function act(candidate: EvolutionCandidate, action: "evaluate" | "approve" | "reject" | "canary" | "promote" | "rollback") {
    const operations = {
      evaluate: evaluateEvolutionCandidate, approve: approveEvolutionCandidate, reject: rejectEvolutionCandidate,
      canary: startEvolutionCanary, promote: promoteEvolutionCandidate, rollback: rollbackEvolutionCandidate,
    };
    setBusyId(candidate.id); setError(""); setNotice("");
    try {
      await operations[action](candidate.id, candidate.version, csrfToken);
      const result = await listEvolutionCandidates();
      setCandidates(result.candidates);
      setNotice("操作已记录，候选状态已更新。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请刷新候选后重试");
    } finally { setBusyId(null); }
  }

  return <div className="growth-page page-stack">
    <fieldset className="growth-view-switch">
      <legend className="sr-only">成长视图</legend>
      <label className={currentView === "personal" ? "active" : ""}><input type="radio" name="growth-view" value="personal" checked={currentView === "personal"} onChange={() => selectView("personal")} />我的成长档案</label>
      <label className={currentView === "agent" ? "active" : ""}><input type="radio" name="growth-view" value="agent" checked={currentView === "agent"} onChange={() => selectView("agent")} />Agent 改进</label>
    </fieldset>
    {currentView === "personal" ? <section className="growth-profile growth-personal-panel" aria-label="我的成长档案">
      <div className="section-heading"><h2>我的成长档案</h2></div>
      {profileLoading && !profile && <p role="status">正在加载成长档案…</p>}
      {profileError && <p className="error-message" role="alert">{profileError} <button className="button button-secondary" type="button" onClick={() => setRefresh(value => value + 1)}>重新加载档案</button></p>}
      {profile && <>
        <div className="growth-profile-metrics">{[["已完成目标", `${profile.metrics.completed_programs}/${profile.metrics.total_programs}`], ["完成行动", profile.metrics.completed_actions], ["平均难度", profile.metrics.average_difficulty ?? "—"], ["平均实际用时", profile.metrics.average_actual_minutes == null ? "—" : `${profile.metrics.average_actual_minutes} 分钟`], ["接受调整", profile.metrics.accepted_adjustments]].map(([label, value]) => <div key={String(label)}><span>{label}</span><strong>{value}</strong></div>)}</div>
        {profile.programs.length === 0 ? <div className="empty-state"><strong>还没有目标执行记录</strong><a className="button button-secondary" href="/workspace">查看目标</a></div>
          : profile.programs.map(program => <article className="growth-profile-program" key={program.id}><div><a href={`/workspace/${encodeURIComponent(program.source_plan_document_id)}`}><strong>{program.objective_title}</strong></a><span>{localizedCode(program.status)} · {program.start_date} 至 {program.end_date}</span>{program.completion_summary && <p>{program.completion_summary}</p>}</div><span>{program.progress.required_completed}/{program.progress.required_total} 必做行动</span></article>)}
      </>}
    </section> : <div className="growth-agent-panel page-stack">
    <header className="growth-agent-heading"><h2>Agent 改进</h2><span className="version-badge">{candidates.filter(item => !["REJECTED", "ROLLED_BACK", "FAILED", "PROMOTED"].includes(item.status)).length} 个进行中</span></header>
    <LearningPanel csrfToken={csrfToken} />
    <section className="evolution-pipeline" aria-label="自进化阶段">
      <div className="section-heading"><h3>评测与发布</h3><p>评测后由你批准，Canary 样本达标后仍需你确认晋升。</p></div>
      <ol>{stages.map((stage, index) => <li className={index <= furthestStage ? "is-reached" : ""} key={stage} aria-current={index === currentStage ? "step" : undefined}><span>{index + 1}</span><strong>{stage}</strong></li>)}</ol>
      {rolledBackCount > 0 && <p className="evolution-pipeline-outcome"><strong>回滚分支</strong><span>{rolledBackCount} 个候选曾进入验证，随后已恢复原版本；详情见下方记录。</span></p>}
    </section>
    {notice && <p className="growth-notice" role="status">{notice}</p>}
    {(error || loadError) && <div className="error-message" role="alert"><span>{error || loadError}</span><button className="button button-secondary" type="button" onClick={() => { setError(""); setRefresh(value => value + 1); }}>重新加载候选</button></div>}
    <section className="growth-list" aria-label="成长候选列表">
      <div className="section-heading"><h3>改进记录</h3></div>
      {loading ? <p className="muted" role="status">正在加载候选…</p> : candidates.length === 0 && !error && !loadError ? <div className="empty-state"><strong>暂无成长候选</strong></div> : candidates.map((candidate) => <EvolutionCandidateCard key={candidate.id} candidate={candidate} busy={busyId === candidate.id} onAction={(action) => void act(candidate, action)} />)}
    </section>
    </div>}
  </div>;
}
