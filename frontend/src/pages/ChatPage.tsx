import { useState } from "react";
import ApprovalCard from "../components/ApprovalCard";
import ActivityRail from "../components/ActivityRail";
import { addBudget, cancelRun, continueOutcome, createGoal, grantApproval, rejectApproval, resumeRun, sendMessage } from "../api";
import { useRunTelemetry } from "../hooks/useRunTelemetry";
import type { Run } from "../types";

interface ChatPageProps {
  csrfToken: string;
  run: Run | null;
  onRun: (run: Run) => void;
  onOpenTrajectory: () => void;
}

export default function ChatPage({ csrfToken, run, onRun, onOpenTrajectory }: ChatPageProps) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const telemetry = useRunTelemetry(run?.id ?? null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (!run) {
        const created = await createGoal({ title: title.trim() || "未命名目标", description: description.trim() }, csrfToken);
        const next = await sendMessage(created.id, description.trim() || title.trim(), csrfToken);
        onRun(next);
        setTitle("");
        setDescription("");
      } else if (feedback.trim()) {
        onRun(await sendMessage(run.goal_id, feedback.trim(), csrfToken));
        setFeedback("");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "请求失败");
    } finally {
      setBusy(false);
    }
  }

  async function runAction(action: () => Promise<Run>) {
    setError("");
    try {
      onRun(await action());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "请求失败");
    }
  }

  return (
    <div className="page-stack">
      <section className="hero-panel">
        <div>
          <span className="eyebrow">GOAL / INTERACTION</span>
          <h2>{run ? "继续推动当前目标" : "目标对话"}</h2>
          <p>{run ? "用反馈、调整指令或新的上下文推进同一个 Run。" : "把目标交给本地 Runtime，先澄清，再生成可批准的计划。"}</p>
        </div>
        <span className={`state-pill state-${(run?.state ?? "RECEIVED").toLowerCase()}`}>{run?.state ?? "RECEIVED"}</span>
      </section>

      {!run ? (
        <form className="form-panel" onSubmit={(event) => void submit(event)}>
          <label htmlFor="goal-title">目标标题</label>
          <input id="goal-title" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：整理本周发布计划" />
          <label htmlFor="goal-description">目标描述</label>
          <textarea id="goal-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="描述结果、约束和你希望 Agent 先处理的部分" rows={5} />
          <div className="form-footer">
            <span className="muted">本地单用户 · 顺序执行 · 可审计</span>
            <button className="button button-primary" disabled={busy} type="submit">发送目标</button>
          </div>
        </form>
      ) : (
        <div className="chat-layout">
          <div className="chat-column">
          <form className="form-panel" onSubmit={(event) => void submit(event)}>
            <label htmlFor="feedback">反馈或调整指令</label>
            <textarea id="feedback" value={feedback} onChange={(event) => setFeedback(event.target.value)} placeholder="例如：保留第二步，先完成本地草稿" rows={4} />
            <div className="form-footer">
              <span className="muted">预算剩余：{String(run.budget.react_iterations_remaining ?? "不可用")} 轮</span>
              <button className="button button-primary" disabled={busy || !feedback.trim()} type="submit">发送反馈</button>
            </div>
          </form>

          {run.pending_approvals.length > 0 && (
            <section className="approval-stack" aria-label="待审批操作">
              <div className="section-heading"><span className="eyebrow">CONTROL GATE</span><h3>需要你的决定</h3></div>
              {run.pending_approvals.map((approvalId) => (
                <ApprovalCard
                  key={approvalId}
                  approvalId={approvalId}
                  onGrant={async () => onRun(await grantApproval(approvalId, csrfToken))}
                  onReject={async () => onRun(await rejectApproval(approvalId, csrfToken))}
                />
              ))}
            </section>
          )}

          <div className="action-bar">
            {run.state === "BLOCKED" && <button className="button button-primary" type="button" onClick={() => void runAction(() => resumeRun(run.id, csrfToken))}>继续执行</button>}
            {run.state === "AWAITING_OUTCOME" && <button className="button button-primary" type="button" onClick={() => void runAction(() => continueOutcome(run.id, true, csrfToken))}>目标已完成</button>}
            {run.state === "AWAITING_OUTCOME" && <button className="button button-quiet" type="button" onClick={() => void runAction(() => continueOutcome(run.id, false, csrfToken))}>继续观察</button>}
            {(run.state === "BLOCKED" || run.state === "EXECUTING" || run.state === "AWAITING_OUTCOME") && <button className="button button-quiet" type="button" onClick={() => void runAction(() => addBudget(run.id, 1, csrfToken))}>追加 1 轮预算</button>}
            {!['COMPLETED', 'CANCELLED', 'FAILED'].includes(run.state) && <button className="button button-danger" type="button" onClick={() => void runAction(() => cancelRun(run.id, csrfToken))}>取消 Run</button>}
          </div>
          </div>
          <ActivityRail
            run={run}
            events={telemetry.events}
            stats={telemetry.stats}
            loading={telemetry.loading}
            onOpenTrajectory={onOpenTrajectory}
          />
        </div>
      )}
      {error && <p className="error-message" role="alert">{error}</p>}
      {run && <p className="empty-state">Run {run.id} · 事件和统计在轨迹页持续更新。</p>}
      {!run && <p className="empty-state">等待本地 Run</p>}
    </div>
  );
}
