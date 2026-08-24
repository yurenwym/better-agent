import type { EvolutionCandidate } from "../types";

interface Props {
  candidate: EvolutionCandidate;
  busy?: boolean;
  onAction: (action: "evaluate" | "approve" | "reject" | "canary" | "promote" | "rollback") => void;
}

const statuses: Record<string, string> = {
  DRAFT: "草稿", READY_FOR_EVAL: "等待评测", EVALUATING: "评测中", EVALUATED: "评测完成",
  PENDING_APPROVAL: "等待审批", APPROVED: "已批准", CANARY: "灰度中", PROMOTED: "已启用",
  FAILED: "评测失败", REJECTED: "已拒绝", ROLLED_BACK: "已回滚",
};
const kinds: Record<string, string> = { memory: "记忆", skill: "技能", policy: "策略", prompt: "提示词", code: "代码" };

export default function EvolutionCandidateCard({ candidate, busy = false, onAction }: Props) {
  const evaluationPassed = candidate.evaluation?.deterministic_pass === true && candidate.evaluation.regressions.length === 0;
  return <article className="evolution-card">
    <header className="evolution-card-heading"><div><span className="eyebrow">{kinds[candidate.kind] ?? candidate.kind} · v{candidate.version}</span><h3>{candidate.title}</h3><p>{candidate.summary}</p></div><span className={`evolution-status status-${candidate.status.toLowerCase()}`}>{statuses[candidate.status] ?? candidate.status}</span></header>
    <div className="evolution-facts">
      <div><span>证据</span><strong>{candidate.evidence_count} 条证据</strong></div>
      <div><span>独立评测</span><strong>{candidate.evaluation ? (evaluationPassed ? "评测已通过" : "存在回归") : "尚未评测"}</strong></div>
      <div><span>权限变化</span><strong>{candidate.permission_diff.added.length ? `新增权限 ${candidate.permission_diff.added.length} 项` : "没有新增权限"}</strong></div>
      <div><span>风险</span><strong>{candidate.risk_level === "high" ? "高" : candidate.risk_level === "medium" ? "中" : "低"}</strong></div>
    </div>
    {(candidate.permission_diff.added.length > 0 || candidate.permission_diff.removed.length > 0) && <div className="permission-diff" aria-label="权限变化明细">
      {candidate.permission_diff.added.map((item) => <span className="permission-added" key={`add-${item}`}>+ {item}</span>)}
      {candidate.permission_diff.removed.map((item) => <span className="permission-removed" key={`remove-${item}`}>− {item}</span>)}
    </div>}
    {candidate.evaluation?.regressions.length ? <div className="evolution-warning"><strong>回归项</strong><span>{candidate.evaluation.regressions.join("；")}</span></div> : null}
    <footer className="evolution-actions">
      {candidate.status === "READY_FOR_EVAL" && <button className="button button-primary" disabled={busy} type="button" onClick={() => onAction("evaluate")}>开始评测</button>}
      {["EVALUATED", "PENDING_APPROVAL"].includes(candidate.status) && <><button className="button button-quiet" disabled={busy} type="button" onClick={() => onAction("reject")}>拒绝候选</button>{evaluationPassed && <button className="button button-primary" disabled={busy} type="button" onClick={() => onAction("approve")}>批准候选</button>}</>}
      {candidate.status === "APPROVED" && <button className="button button-primary" disabled={busy} type="button" onClick={() => onAction("canary")}>开始 Canary</button>}
      {candidate.status === "CANARY" && <><button className="button button-danger" disabled={busy} type="button" onClick={() => onAction("rollback")}>回滚 Canary</button>{(candidate.canary?.sample_size ?? 0) >= 3 ? <button className="button button-primary" disabled={busy} type="button" onClick={() => onAction("promote")}>正式启用</button> : <span className="canary-gate-note">还需 {3 - (candidate.canary?.sample_size ?? 0)} 个挑战组样本</span>}</>}
      {candidate.status === "PROMOTED" && <button className="button button-danger" disabled={busy} type="button" onClick={() => onAction("rollback")}>回滚版本</button>}
    </footer>
  </article>;
}
