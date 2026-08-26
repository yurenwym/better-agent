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

const changeLabels: Record<string, string> = { prompts: "提示词策略", prompt: "提示词策略", skills: "技能", policy: "行为策略", memory: "记忆规则", code: "运行代码" };
const statusNext: Record<string, string> = {
  READY_FOR_EVAL: "下一步：运行独立评测，确认改变没有破坏已有能力。",
  EVALUATED: "下一步：由你决定是否批准进入小范围验证。",
  PENDING_APPROVAL: "下一步：由你决定是否批准进入小范围验证。",
  APPROVED: "下一步：开始 Canary，只让少量任务使用新版本。",
  CANARY: "正在小范围验证；样本充足且安全后，才能正式启用。",
  PROMOTED: "新版本已正式启用，仍可随时回滚。",
  REJECTED: "候选已拒绝，不会改变当前 Agent。",
  ROLLED_BACK: "新版本已回滚，Agent 已恢复到原有行为。",
};

function changeValue(value: unknown): string {
  if (typeof value === "string" && value.includes("research-scope-bounded")) return "限制研究范围，只生成用户明确要求的内容";
  if (typeof value === "string") return value;
  if (value === null) return "移除该能力";
  return JSON.stringify(value, null, 2);
}

function candidateProblem(candidate: EvolutionCandidate): string {
  if (candidate.reason && !/^\?+$/.test(candidate.reason.replace(/\s/g,""))) return candidate.reason;
  if (Object.values(candidate.proposed_content ?? {}).some(value=>typeof value==="string"&&value.includes("research-scope-bounded"))) return "研究任务曾多次扩大用户没有要求的范围。";
  return candidate.summary;
}

function readableSummary(candidate: EvolutionCandidate): string {
  if (!candidate.summary || /^\?+$/.test(candidate.summary.replace(/\s/g,""))) return candidateProblem(candidate);
  return candidate.summary;
}

function rollbackTitle(candidate: EvolutionCandidate): string {
  if (candidate.rollback?.kind === "safety_auto") return "安全机制自动回滚";
  if (candidate.rollback?.actor === "user" || candidate.rollback?.reason === "user rollback") return "用户手动回滚";
  return "管理员手动回滚";
}

function rollbackReason(candidate: EvolutionCandidate): string {
  if (candidate.rollback?.kind === "safety_auto") return "Canary 任务出现明确的安全失败，系统已恢复原版本。";
  if (candidate.rollback?.reason === "user rollback") return "用户主动终止了这次改进。";
  return candidate.rollback?.reason || "已手动恢复原版本。";
}

export default function EvolutionCandidateCard({ candidate, busy = false, onAction }: Props) {
  const evaluationPassed = candidate.evaluation?.deterministic_pass === true && candidate.evaluation.regressions.length === 0;
  const evaluation = candidate.evaluation;
  const hasBehaviorMetrics = evaluation != null && [evaluation.baseline_correct, evaluation.candidate_correct, evaluation.quality_delta, evaluation.safety_violations].every(value => typeof value === "number");
  const changes = Object.entries(candidate.proposed_content ?? {});
  const evaluationProgress = evaluation?.total != null
    ? `确定性检查 ${evaluation.passed ?? 0} / ${evaluation.total} 通过`
    : evaluationPassed ? "全部确定性检查通过" : "等待评测";
  const canaryProgress = candidate.status === "CANARY" ? `挑战组 ${candidate.canary?.challenger_sample_size ?? candidate.canary?.sample_size ?? 0}/${candidate.canary?.required_samples ?? 20} · 对照组 ${candidate.canary?.champion_sample_size ?? 0}/${candidate.canary?.required_samples ?? 20}` : null;
  const canaryMissing = candidate.status === "CANARY" ? `还需 ${Math.max((candidate.canary?.required_samples ?? 20) - (candidate.canary?.challenger_sample_size ?? candidate.canary?.sample_size ?? 0), 0)} 个挑战组样本、${Math.max((candidate.canary?.required_samples ?? 20) - (candidate.canary?.champion_sample_size ?? 0), 0)} 个对照组样本` : null;
  return <article className="evolution-card">
    <header className="evolution-card-heading"><div><span className="eyebrow">{kinds[candidate.kind] ?? candidate.kind} · v{candidate.version}{candidate.record_origin === "demo" ? " · 演示记录" : candidate.record_origin === "manual" ? " · 人工证据" : ""}</span><h3>{candidate.title}</h3><p>{readableSummary(candidate)}</p></div><span className={`evolution-status status-${candidate.status.toLowerCase()}`}>{statuses[candidate.status] ?? candidate.status}</span></header>
    {candidate.record_origin === "demo" && <p className="evolution-demo-note">这是一条演示数据，用于验证进化流程，不代表 Agent 从真实任务中自动学习的结果。</p>}
    <div className="evolution-facts">
      <div><span>证据</span><strong>{candidate.evidence_count} 条证据</strong></div>
      <div><span>独立评测</span><strong>{candidate.evaluation ? (evaluationPassed ? "评测已通过" : "存在回归") : "尚未评测"}</strong></div>
      <div><span>权限变化</span><strong>{candidate.permission_diff.added.length ? `新增权限 ${candidate.permission_diff.added.length} 项` : "没有新增权限"}</strong></div>
      <div><span>风险</span><strong>{candidate.risk_level === "high" ? "高" : candidate.risk_level === "medium" ? "中" : "低"}</strong></div>
    </div>
    <div className="evolution-story">
      <section><span className="evolution-step">01</span><div><h4>发现的问题</h4><p>{candidateProblem(candidate)}</p><small>来自 {candidate.evidence_count} 条独立经验，单次异常不会触发进化。</small></div></section>
      <section><span className="evolution-step">02</span><div><h4>准备怎样改变</h4>{changes.length ? <dl className="evolution-change-list">{changes.map(([name,value])=><div key={name}><dt>{changeLabels[name] ?? name}</dt><dd>{changeValue(value)}</dd></div>)}</dl> : <p>{candidate.summary}</p>}</div></section>
      <section><span className="evolution-step">03</span><div><h4>验证结果</h4><p className={evaluationPassed ? "evolution-pass" : ""}>{evaluationProgress}</p><small>{candidate.permission_diff.added.length ? `涉及 ${candidate.permission_diff.added.length} 项新增权限，需谨慎确认。` : "没有新增权限，核心安全边界保持不变。"}</small></div></section>
      <section className="evolution-current-step"><span className="evolution-step">04</span><div><h4>现在进行到哪</h4><p>{statusNext[candidate.status] ?? "等待系统更新候选状态。"}</p>{canaryProgress&&<strong>{canaryProgress}</strong>}</div></section>
    </div>
    {candidate.status === "ROLLED_BACK" && candidate.rollback && <div className={`evolution-rollback ${candidate.rollback.kind === "safety_auto" ? "is-automatic" : ""}`} role="status">
      <div><strong>{rollbackTitle(candidate)}</strong><span>{rollbackReason(candidate)}</span></div>
      <time dateTime={candidate.rollback.occurred_at}>{new Date(candidate.rollback.occurred_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })}</time>
    </div>}
    {evaluation && hasBehaviorMetrics && <div className="evolution-metrics" aria-label="真实行为评测">
      <div><span>基线正确</span><strong>{evaluation.baseline_correct ?? "—"}</strong></div>
      <div><span>候选正确</span><strong>{evaluation.candidate_correct ?? "—"}</strong></div>
      <div><span>质量变化</span><strong>{typeof evaluation.quality_delta === "number" ? `${evaluation.quality_delta >= 0 ? "+" : ""}${evaluation.quality_delta}` : "—"}</strong></div>
      <div><span>安全失败</span><strong>{evaluation.safety_violations ?? "—"}</strong></div>
    </div>}
    {evaluation && !hasBehaviorMetrics && <div className="evolution-metrics-unavailable"><strong>旧版评测未记录真实行为指标</strong><span>当前只能确认确定性检查结果，不能据此判断候选质量或 Canary 安全性。</span></div>}
    {(candidate.permission_diff.added.length > 0 || candidate.permission_diff.removed.length > 0) && <div className="permission-diff" aria-label="权限变化明细">
      {candidate.permission_diff.added.map((item) => <span className="permission-added" key={`add-${item}`}>+ {item}</span>)}
      {candidate.permission_diff.removed.map((item) => <span className="permission-removed" key={`remove-${item}`}>− {item}</span>)}
    </div>}
    {candidate.evaluation?.regressions.length ? <div className="evolution-warning"><strong>回归项</strong><span>{candidate.evaluation.regressions.join("；")}</span></div> : null}
    <footer className="evolution-actions">
      {candidate.status === "READY_FOR_EVAL" && <button className="button button-primary" disabled={busy} type="button" onClick={() => onAction("evaluate")}>开始评测</button>}
      {["EVALUATED", "PENDING_APPROVAL"].includes(candidate.status) && <><button className="button button-quiet" disabled={busy} type="button" onClick={() => onAction("reject")}>拒绝候选</button>{evaluationPassed && <button className="button button-primary" disabled={busy} type="button" onClick={() => onAction("approve")}>批准候选</button>}</>}
      {candidate.status === "APPROVED" && (candidate.kind === "prompt" ? <button className="button button-primary" disabled={busy} type="button" onClick={() => onAction("canary")}>开始 Canary</button> : <span className="canary-gate-note">该类型尚未配置在线运行适配器</span>)}
      {candidate.status === "CANARY" && <><button className="button button-danger" disabled={busy} type="button" onClick={() => onAction("rollback")}>回滚 Canary</button>{candidate.canary?.promotable ? <button className="button button-primary" disabled={busy} type="button" onClick={() => onAction("promote")}>正式启用</button> : <span className="canary-gate-note">{canaryMissing}{(candidate.canary?.safety_failures ?? 0) > 0 ? " · 存在安全失败，已禁止晋升" : ""}</span>}</>}
      {candidate.status === "PROMOTED" && <button className="button button-danger" disabled={busy} type="button" onClick={() => onAction("rollback")}>回滚版本</button>}
    </footer>
  </article>;
}
