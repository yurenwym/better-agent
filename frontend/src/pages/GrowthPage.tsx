import { useEffect, useState } from "react";
import {
  approveEvolutionCandidate, evaluateEvolutionCandidate, listEvolutionCandidates,
  promoteEvolutionCandidate, rejectEvolutionCandidate, rollbackEvolutionCandidate, startEvolutionCanary, getGrowthProfile,
} from "../api";
import EvolutionCandidateCard from "../components/EvolutionCandidateCard";
import type { EvolutionCandidate, GrowthProfile } from "../types";

interface GrowthPageProps { csrfToken: string; }

export default function GrowthPage({ csrfToken }: GrowthPageProps) {
  const [candidates, setCandidates] = useState<EvolutionCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [profile, setProfile] = useState<GrowthProfile | null>(null);
  const stages = ["经验", "候选", "评测", "审批", "Canary", "晋升"];
  const stageIndex = (status: EvolutionCandidate["status"]) => ({
    DRAFT: 0, READY_FOR_EVAL: 1, EVALUATING: 2, EVALUATED: 2, PENDING_APPROVAL: 3,
    APPROVED: 3, CANARY: 4, PROMOTED: 5, FAILED: 2, REJECTED: 3, ROLLED_BACK: 4,
  }[status] ?? 0);
  const furthestStage = candidates.reduce((current, candidate) => Math.max(current, stageIndex(candidate.status)), 0);

  useEffect(() => {
    let active = true;
    const load = () => Promise.all([listEvolutionCandidates(), getGrowthProfile()])
      .then(([evolution, nextProfile]) => { if (active) { setCandidates(evolution.candidates); setProfile(nextProfile); } })
      .catch((caught: unknown) => { if (active) setError(caught instanceof Error ? caught.message : "成长候选加载失败"); })
      .finally(() => { if (active) setLoading(false); });
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  async function act(candidate: EvolutionCandidate, action: "evaluate" | "approve" | "reject" | "canary" | "promote" | "rollback") {
    const operations = {
      evaluate: evaluateEvolutionCandidate, approve: approveEvolutionCandidate, reject: rejectEvolutionCandidate,
      canary: startEvolutionCanary, promote: promoteEvolutionCandidate, rollback: rollbackEvolutionCandidate,
    };
    setBusyId(candidate.id); setError(""); setNotice("");
    try {
      await operations[action](candidate.id, candidate.version, csrfToken);
      const [result, nextProfile] = await Promise.all([listEvolutionCandidates(), getGrowthProfile()]);
      setCandidates(result.candidates); setProfile(nextProfile);
      setNotice("操作已记录，候选状态已更新。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请刷新候选后重试");
    } finally { setBusyId(null); }
  }

  return <div className="growth-page page-stack">
    <section className="growth-profile" aria-label="我的成长档案">
      <div className="section-heading"><span className="eyebrow">MY GROWTH</span><h3>我的成长档案</h3><p>只统计你实际完成、反馈和调整过的目标，不把 Agent 的候选改进混进来。</p></div>
      <div className="growth-profile-metrics">{[["已完成目标", profile?.metrics.completed_programs ?? 0], ["完成行动", profile?.metrics.completed_actions ?? 0], ["平均难度", profile?.metrics.average_difficulty ?? "—"], ["接受调整", profile?.metrics.accepted_adjustments ?? 0]].map(([label, value]) => <div key={String(label)}><span>{label}</span><strong>{value}</strong></div>)}</div>
      {profile?.programs.slice(0, 3).map(program => <article className="growth-profile-program" key={program.id}><div><strong>{program.objective_title}</strong><span>{program.status} · {program.start_date} 至 {program.end_date}</span></div><span>{program.progress.required_completed}/{program.progress.required_total} 必做行动</span></article>)}
    </section>
    <section className="hero-panel growth-hero">
      <div><span className="eyebrow">CONTROLLED EVOLUTION</span><h2>Agent 正在怎样变得更好</h2><p>这里展示每次自进化的原因、具体改变和验证结果。任何改变都要经过你的批准，并先在少量任务中验证。</p></div>
      <span className="version-badge">{candidates.filter(item=>!["REJECTED","ROLLED_BACK"].includes(item.status)).length} 个进行中</span>
    </section>
    <section className="growth-principles" aria-label="自进化流程">
      <article><span>01</span><div><strong>从重复问题中学习</strong><p>至少三条独立经验才会提出改变。</p></div></article>
      <article><span>02</span><div><strong>先验证，再让你决定</strong><p>通过确定性测试后才允许批准。</p></div></article>
      <article><span>03</span><div><strong>小范围试用，可回滚</strong><p>Canary 安全后才会正式启用。</p></div></article>
    </section>
    <section className="evolution-pipeline" aria-label="自进化阶段">
      <div className="section-heading"><span className="eyebrow">RELEASE PIPELINE</span><h3>可信改进闭环</h3><p>系统自动收集经验和生成候选；评测后由你批准，Canary 样本达标后仍需你确认晋升。</p></div>
      <ol>{stages.map((stage, index) => <li className={index <= furthestStage ? "is-reached" : ""} key={stage} aria-current={index === furthestStage ? "step" : undefined}><span>{index + 1}</span><strong>{stage}</strong></li>)}</ol>
    </section>
    {notice && <p className="growth-notice" role="status">{notice}</p>}
    {error && <div className="error-message" role="alert"><span>{error}</span></div>}
    <section className="growth-list" aria-label="成长候选列表">
      <div className="section-heading"><span className="eyebrow">EVOLUTION LOG</span><h3>进化记录</h3><p>每张卡片都回答四个问题：发现了什么、准备改什么、验证是否通过、现在需要做什么。</p></div>
      {loading ? <p className="muted" role="status">正在加载候选…</p> : candidates.length === 0 ? <div className="empty-state"><strong>暂无成长候选</strong><p>积累足够的独立证据后，候选会出现在这里。</p></div> : candidates.map((candidate) => <EvolutionCandidateCard key={candidate.id} candidate={candidate} busy={busyId === candidate.id} onAction={(action) => void act(candidate, action)} />)}
    </section>
  </div>;
}
