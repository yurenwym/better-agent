import type { AgentArtifact, AgentRun, AgentTask } from "../types";
import MarkdownMessage from "./MarkdownMessage";
import AnswerDuration from "./AnswerDuration";

interface ExpertRunCardProps {
  run: AgentRun;
  tasks: AgentTask[];
  artifacts: AgentArtifact[];
  busy?: boolean;
  onCancel?: () => void;
  onOpenModels?: () => void;
}

const runLabels: Record<AgentRun["status"], string> = {
  QUEUED: "专家任务已排队", RUNNING: "专家正在协作", WAITING: "等待专家汇合",
  SUCCEEDED: "专家结果已生成", FAILED: "专家协作未完成", CANCELLED: "专家任务已取消",
};
const taskLabels: Record<AgentTask["status"], string> = {
  QUEUED: "等待开始", RUNNING: "正在处理", WAITING_CHILDREN: "等待其他专家",
  SUCCEEDED: "已返回", FAILED: "未完成", CANCELLED: "已取消",
};
const roleLabels: Record<string, string> = {
  coordinator: "协调专家", researcher: "研究专家", planner: "规划专家", critic: "审阅专家", expert: "领域专家",
};

const failureMessages: Record<string, string> = {
  EXPERT_OUTPUT_TRUNCATED: "模型达到输出上限，未生成完整答案。请检查推理模式和输出额度配置。",
  EXPERT_STRUCTURE: "模型未返回有效的结构化结果。",
  EXPERT_BUDGET: "费用配置或预算限制阻止了专家调用。",
  EXPERT_PAYMENT: "模型供应商余额不足或需要付款。",
  MODEL_NOT_CONFIGURED: "尚未配置可用模型。请先到模型页面配置并启用模型，然后重新提交。",
  ALL_EXPERTS_FAILED: "所有专家任务都未能完成，请查看各任务的失败原因后重试。",
  CHILD_FAILED: "至少一个必要的专家任务未完成，因此无法生成最终回答。",
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

export default function ExpertRunCard({ run, tasks, artifacts, busy = false, onCancel, onOpenModels }: ExpertRunCardProps) {
  const terminal = ["SUCCEEDED", "FAILED", "CANCELLED"].includes(run.status);
  const errorCodes = tasks.flatMap((task) => task.error_code ? [task.error_code] : []);
  const primaryError = errorCodes.includes("MODEL_NOT_CONFIGURED")
    ? "MODEL_NOT_CONFIGURED"
    : errorCodes.find((code) => failureMessages[code]) ?? errorCodes[0];
  return (
    <section className="expert-run-card" aria-busy={!terminal} aria-label="专家协同任务">
      <header className="expert-run-heading">
        <div><span className="eyebrow">专家协同</span><h3>{runLabels[run.status]}</h3><p>{run.objective}</p></div>
        <span className={`expert-status status-${run.status.toLowerCase()}`} role="status">{runLabels[run.status]}</span>
      </header>
      {terminal && run.finished_at && <AnswerDuration milliseconds={Date.parse(run.finished_at) - Date.parse(run.created_at)} />}
      <div className="expert-task-list" aria-label="专家任务进度">
        {tasks.length === 0 ? <p className="muted">{terminal ? "没有产生可用的专家结果。" : "协调专家正在拆解任务…"}</p> : tasks.map((task) => {
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
            <div className="expert-task-head"><div><strong>{roleLabels[task.role] ?? task.role}</strong><span>{run.objective}</span></div><span className={`expert-task-status status-${task.status.toLowerCase()}`} aria-live="polite">{taskLabels[task.status]}</span></div>
            {task.status === "FAILED" && task.error_code && task.error_code !== primaryError && <p>{failureMessages[task.error_code] ?? "该专家调用失败。"}</p>}
            {artifact && <div className="expert-result">
              <div className="expert-result-meta"><span>结论</span><span>{evidenceCount} 条证据</span></div>
              {summary && <MarkdownMessage content={summary} />}
              {findings.length > 0 && <ul>{findings.map((item) => <li key={item}>{item}</li>)}</ul>}
              {risks.length > 0 && <div className="expert-result-note"><strong>风险</strong><span>{risks.join("；")}</span></div>}
              {questions.length > 0 && <div className="expert-result-note"><strong>待确认</strong><span>{questions.join("；")}</span></div>}
            </div>}
          </article>;
        })}
      </div>
      {run.status === "FAILED" && (
        <div className="expert-run-failure" role="alert">
          <div><strong>这次没有生成回答</strong><p>{failureMessages[primaryError ?? ""] ?? "专家协同执行失败，请稍后重试。"}</p></div>
          {primaryError === "MODEL_NOT_CONFIGURED" && onOpenModels && <button className="button button-secondary" type="button" onClick={onOpenModels}>前往模型配置</button>}
        </div>
      )}
      {!terminal && onCancel && <div className="expert-run-actions"><span>取消只停止未完成任务，已产出的结论会保留。</span><button className="button button-danger" disabled={busy} type="button" onClick={onCancel}>{busy ? "正在取消…" : "取消专家任务"}</button></div>}
    </section>
  );
}
