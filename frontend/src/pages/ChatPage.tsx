import { useEffect, useState } from "react";
import ApprovalCard from "../components/ApprovalCard";
import ActivityRail from "../components/ActivityRail";
import ConversationThread from "../components/ConversationThread";
import { addBudget, approvePlan, cancelRun, continueOutcome, createGoal, getPlans, getRun, getSkills, grantApproval, rejectApproval, resumeRun, sendMessage } from "../api";
import { useRunTelemetry } from "../hooks/useRunTelemetry";
import type { Run, SkillDefinition } from "../types";

interface ChatPageProps {
  csrfToken: string;
  run: Run | null;
  onRun: (run: Run) => void;
  onOpenTrajectory: () => void;
  onOpenPlan: () => void;
}

function isReactBudgetBlocked(run: Run): boolean {
  return run.state === "BLOCKED"
    && run.budget.blocked_reason === "react iteration budget exhausted";
}

export default function ChatPage({ csrfToken, run, onRun, onOpenTrajectory, onOpenPlan }: ChatPageProps) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [skills, setSkills] = useState<SkillDefinition[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const telemetry = useRunTelemetry(run?.id ?? null, run?.version ?? 0);

  useEffect(() => {
    let active = true;
    getSkills()
      .then((result) => { if (active) setSkills(result.skills); })
      .catch(() => { if (active) setSkills([]); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!run) {
      setSelectedSkills([]);
    } else if (run.skill_names && run.skill_names.length > 0) {
      setSelectedSkills(run.skill_names);
    }
  }, [run?.id]);

  function toggleSkill(name: string) {
    setSelectedSkills((current) => current.includes(name)
      ? current.filter((item) => item !== name)
      : [...current, name]);
  }

  async function submitContent(content: string): Promise<boolean> {
    setError("");
    setBusy(true);
    try {
      if (!run) {
        const firstLine = content.split(/\r?\n/)[0].trim();
        const created = await createGoal({ title: firstLine.slice(0, 80) || "新的工作目标", description: content }, csrfToken);
        onRun(await getRun(created.run_id));
        onRun(await sendMessage(created.id, content, csrfToken, selectedSkills));
      } else {
        onRun(await sendMessage(run.goal_id, content, csrfToken, selectedSkills));
      }
      return true;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "发送失败，请检查模型连接后重试");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function runAction(action: () => Promise<Run>) {
    setError("");
    setActionBusy(true);
    try {
      onRun(await action());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请稍后重试");
    } finally {
      setActionBusy(false);
    }
  }

  async function approveCurrentPlan(currentRun: Run): Promise<Run> {
    const plans = await getPlans(currentRun.id);
    if (!plans.current) throw new Error("当前没有可批准的计划");
    return approvePlan(currentRun.id, plans.current.version, csrfToken);
  }

  async function recoverFromReactBudget(currentRun: Run): Promise<Run> {
    await addBudget(currentRun.id, 1, csrfToken);
    return resumeRun(currentRun.id, csrfToken);
  }

  const approvalRun = run?.state === "AWAITING_APPROVAL" ? run : null;
  const decision = approvalRun ? {
    title: "计划已经准备好",
    description: "批准后开始执行；需要调整步骤可以先修改计划。",
    primaryLabel: "批准计划并继续",
    secondaryLabel: "修改计划",
    busy: actionBusy,
    onPrimary: () => void runAction(() => approveCurrentPlan(approvalRun)),
    onSecondary: onOpenPlan,
  } : undefined;

  return (
    <div className={run ? "chat-workspace" : "chat-workspace chat-workspace-empty chat-workspace-empty-wide"}>
      <div className="chat-main-column">
        <ConversationThread
          messages={telemetry.messages}
          busy={busy || actionBusy || telemetry.loading}
          title={run ? "推动当前目标" : "从一个目标开始"}
          description={run ? "模型的每次返回都会留在这里，你可以直接根据它继续补充或调整。" : "先写下你要达成的结果，模型会在这条对话中澄清、规划并等待你的确认。"}
          composerDisabled={Boolean(approvalRun)}
          skills={skills}
          selectedSkills={selectedSkills}
          onToggleSkill={toggleSkill}
          decision={decision}
          onSubmit={submitContent}
        />

        {run && run.pending_approvals.length > 0 && (
          <section className="approval-stack" aria-label="待审批操作">
            <div className="section-heading"><span className="eyebrow">CONTROL GATE</span><h3>需要你的决定</h3><p>写入类操作会在这里暂停，批准后才会产生副作用。</p></div>
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

        {run && (
          <div className="action-bar">
            {isReactBudgetBlocked(run) && <p className="budget-guard-message" role="status">Agent 已达到当前步骤的安全保护阈值</p>}
            {run.state === "BLOCKED" && !isReactBudgetBlocked(run) && <button className="button button-primary" type="button" onClick={() => void runAction(() => resumeRun(run.id, csrfToken))}>继续执行</button>}
            {run && isReactBudgetBlocked(run) && <button className="button button-primary" type="button" onClick={() => void runAction(() => recoverFromReactBudget(run))}>继续执行一次</button>}
            {run.state === "AWAITING_OUTCOME" && <button className="button button-primary" type="button" onClick={() => void runAction(() => continueOutcome(run.id, true, csrfToken))}>目标已完成</button>}
            {run.state === "AWAITING_OUTCOME" && <button className="button button-quiet" type="button" onClick={() => void runAction(() => continueOutcome(run.id, false, csrfToken))}>继续观察</button>}
            {!['COMPLETED', 'CANCELLED', 'FAILED'].includes(run.state) && <button className="button button-danger" type="button" onClick={() => void runAction(() => cancelRun(run.id, csrfToken))}>取消 Run</button>}
          </div>
        )}
        {error && <p className="error-message" role="alert">{error}</p>}
      </div>

      {run && <ActivityRail run={run} events={telemetry.events} stats={telemetry.stats} loading={telemetry.loading} onOpenTrajectory={onOpenTrajectory} />}
    </div>
  );
}
