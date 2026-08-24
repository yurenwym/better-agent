import type { AgentArtifact, AgentRun, AgentTask } from "../types";

interface ExpertRunCardProps {
  run: AgentRun;
  tasks: AgentTask[];
  artifacts: AgentArtifact[];
  busy?: boolean;
  onCancel?: () => void;
}

const runLabels: Record<AgentRun["status"], string> = {
  QUEUED: "专家任务已排队", RUNNING: "专家正在协作", WAITING: "等待专家汇合",
  SUCCEEDED: "专家协作已完成", FAILED: "专家协作未完成", CANCELLED: "专家任务已取消",
};
const taskLabels: Record<AgentTask["status"], string> = {
  QUEUED: "等待开始", RUNNING: "正在处理", WAITING_CHILDREN: "等待其他专家",
  SUCCEEDED: "已完成", FAILED: "未完成", CANCELLED: "已取消",
};
const roleLabels: Record<string, string> = {
  coordinator: "协调专家", researcher: "研究专家", planner: "规划专家", critic: "审阅专家", expert: "领域专家",
};

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (typeof item === "string") return [item];
    if (item && typeof item === "object") {
      const record = item as Record<string, unknown>;
      const text = record.text ?? record.summary ?? record.title ?? record.finding;
      return typeof text === "string" ? [text] : [];
    }
    return [];
  });
}

export default function ExpertRunCard({ run, tasks, artifacts, busy = false, onCancel }: ExpertRunCardProps) {
  const terminal = ["SUCCEEDED", "FAILED", "CANCELLED"].includes(run.status);
  return (
    <section className="expert-run-card" aria-busy={!terminal} aria-label="专家协同任务">
      <header className="expert-run-heading">
        <div><span className="eyebrow">EXPERT COLLABORATION</span><h3>{runLabels[run.status]}</h3><p>{run.objective}</p></div>
        <span className={`expert-status status-${run.status.toLowerCase()}`} role="status">{runLabels[run.status]}</span>
      </header>
      <div className="expert-task-list" aria-label="专家任务进度">
        {tasks.length === 0 ? <p className="muted">协调专家正在拆解任务…</p> : tasks.map((task) => {
          const artifact = artifacts.find((item) => item.id === task.result_artifact_id);
          const summary = artifact && typeof artifact.content.summary === "string" ? artifact.content.summary : "";
          const findings = artifact ? strings(artifact.content.findings) : [];
          const evidenceCount = artifact ? new Set([
            ...artifact.source_refs,
            ...(Array.isArray(artifact.content.findings) ? artifact.content.findings.flatMap((finding) =>
              finding && typeof finding === "object" && Array.isArray((finding as Record<string, unknown>).source_refs)
                ? ((finding as Record<string, unknown>).source_refs as unknown[]).filter((ref): ref is string => typeof ref === "string") : []) : []),
          ]).size : 0;
          const risks = artifact ? strings(artifact.content.risks) : [];
          const questions = artifact ? strings(artifact.content.open_questions) : [];
          return <article className="expert-task" key={task.id}>
            <div className="expert-task-head"><div><strong>{roleLabels[task.role] ?? task.role}</strong><span>{task.objective}</span></div><span className={`expert-task-status status-${task.status.toLowerCase()}`} aria-live="polite">{taskLabels[task.status]}</span></div>
            {artifact && <div className="expert-result">
              <div className="expert-result-meta"><span>结论</span><span>{evidenceCount} 条证据</span></div>
              {summary && <p>{summary}</p>}
              {findings.length > 0 && <ul>{findings.map((item) => <li key={item}>{item}</li>)}</ul>}
              {risks.length > 0 && <div className="expert-result-note"><strong>风险</strong><span>{risks.join("；")}</span></div>}
              {questions.length > 0 && <div className="expert-result-note"><strong>待确认</strong><span>{questions.join("；")}</span></div>}
            </div>}
          </article>;
        })}
      </div>
      {!terminal && onCancel && <div className="expert-run-actions"><span>取消只停止未完成任务，已产出的结论会保留。</span><button className="button button-danger" disabled={busy} type="button" onClick={onCancel}>{busy ? "正在取消…" : "取消专家任务"}</button></div>}
    </section>
  );
}
