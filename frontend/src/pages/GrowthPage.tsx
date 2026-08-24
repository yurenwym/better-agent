import { useEffect, useState } from "react";
import {
  approveEvolutionCandidate, evaluateEvolutionCandidate, listEvolutionCandidates,
  promoteEvolutionCandidate, rejectEvolutionCandidate, rollbackEvolutionCandidate, startEvolutionCanary,
} from "../api";
import EvolutionCandidateCard from "../components/EvolutionCandidateCard";
import type { EvolutionCandidate } from "../types";

interface GrowthPageProps { csrfToken: string; }

export default function GrowthPage({ csrfToken }: GrowthPageProps) {
  const [candidates, setCandidates] = useState<EvolutionCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let active = true;
    const load = () => listEvolutionCandidates()
      .then(({ candidates: items }) => { if (active) setCandidates(items); })
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
      const result = await listEvolutionCandidates();
      setCandidates(result.candidates);
      setNotice("操作已记录，候选状态已更新。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请刷新候选后重试");
    } finally { setBusyId(null); }
  }

  return <div className="growth-page page-stack">
    <section className="hero-panel growth-hero">
      <div><span className="eyebrow">CONTROLLED EVOLUTION</span><h2>受控成长</h2><p>系统只从证据中提出候选。每项改变都经过独立评测、权限核对和人工审批，再以 Canary 小范围验证；不会自行改写核心安全规则。</p></div>
      <span className="version-badge">{candidates.length} 个候选</span>
    </section>
    <section className="growth-principles" aria-label="成长安全边界">
      <article><span>01</span><div><strong>证据驱动</strong><p>一次失败不会直接改变系统行为。</p></div></article>
      <article><span>02</span><div><strong>权限透明</strong><p>新增与移除权限逐项展示。</p></div></article>
      <article><span>03</span><div><strong>随时回滚</strong><p>Canary 和已启用版本保留回退路径。</p></div></article>
    </section>
    {notice && <p className="growth-notice" role="status">{notice}</p>}
    {error && <div className="error-message" role="alert"><span>{error}</span></div>}
    <section className="growth-list" aria-label="成长候选列表">
      <div className="section-heading"><span className="eyebrow">CANDIDATES</span><h3>能力候选</h3><p>按钮只在候选状态允许时出现，所有变更均携带当前版本。</p></div>
      {loading ? <p className="muted" role="status">正在加载候选…</p> : candidates.length === 0 ? <div className="empty-state"><strong>暂无成长候选</strong><p>积累足够的独立证据后，候选会出现在这里。</p></div> : candidates.map((candidate) => <EvolutionCandidateCard key={candidate.id} candidate={candidate} busy={busyId === candidate.id} onAction={(action) => void act(candidate, action)} />)}
    </section>
  </div>;
}
