import { useCallback, useEffect, useState } from "react";
import ApprovalCard from "../components/ApprovalCard";
import ActivityRail from "../components/ActivityRail";
import ConversationThread from "../components/ConversationThread";
import {
  addBudget,
  approvePlan,
  answerAsk,
  cancelRun,
  cancelTurn as cancelConversationTurn,
  continueOutcome,
  createGoal,
  createThread,
  getPlans,
  getRun,
  getSkills,
  grantApproval,
  rejectApproval,
  resumeRun,
  selectDirection,
  sendMessage,
  submitTurn,
} from "../api";
import { useRunTelemetry } from "../hooks/useRunTelemetry";
import { useThreadTelemetry } from "../hooks/useThreadTelemetry";
import type { AskAnswer, Run, SkillDefinition, ThreadEvent } from "../types";

interface ChatPageProps {
  csrfToken: string;
  run: Run | null;
  threadId?: string | null;
  onThread?: (threadId: string) => void;
  onRun: (run: Run) => void;
  onOpenTrajectory: () => void;
  onOpenPlan: (planDocumentId?: string) => void;
}

function isReactBudgetBlocked(run: Run): boolean {
  return run.state === "BLOCKED"
    && run.budget.blocked_reason === "react iteration budget exhausted";
}

function clientTurnId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `client-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export interface PlanReference {
  planDocumentId: string;
  versionId: string;
  version: number;
  messageId: string;
  status: "ready" | "failed" | "conflict";
}

const planReferenceEventTypes = new Set(["plan.document_ready", "plan.document_failed", "plan.document_conflict"]);

export function latestPlanReference(events: ThreadEvent[]): PlanReference | null {
  const event = [...events].reverse().find((candidate) => {
    if (!planReferenceEventTypes.has(candidate.type)) return false;
    return typeof candidate.data.plan_document_id === "string"
      && candidate.data.plan_document_id.trim().length > 0
      && typeof candidate.data.version_id === "string"
      && candidate.data.version_id.trim().length > 0
      && typeof candidate.data.version === "number"
      && Number.isInteger(candidate.data.version)
      && candidate.data.version > 0
      && typeof candidate.data.content_hash === "string"
      && /^sha256:[0-9a-f]{64}$/.test(candidate.data.content_hash)
      && typeof candidate.data.source_message_id === "string"
      && candidate.data.source_message_id.trim().length > 0
      && typeof candidate.data.actor === "string"
      && candidate.data.actor.trim().length > 0;
  });
  if (!event) return null;
  return {
    planDocumentId: event.data.plan_document_id as string,
    versionId: event.data.version_id as string,
    version: event.data.version as number,
    messageId: event.data.source_message_id as string,
    status: event.type === "plan.document_failed"
      ? "failed"
      : event.type === "plan.document_conflict" ? "conflict" : "ready",
  };
}

const turnBusyStates = new Set(["ACCEPTED", "ROUTING", "STREAMING", "MATERIALIZING"]);
const pendingAskConflictText = "当前对话正在等待你的回答，请先回答上方问题；如果想开始新的目标，请先停止询问。";

export default function ChatPage({ csrfToken, run, threadId = null, onThread, onRun, onOpenTrajectory, onOpenPlan }: ChatPageProps) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [askBusy, setAskBusy] = useState(false);
  const [pendingAskConflict, setPendingAskConflict] = useState(false);
  const [localThreadId, setLocalThreadId] = useState<string | null>(threadId);
  const [skills, setSkills] = useState<SkillDefinition[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const conversationId = threadId ?? localThreadId;
  const telemetry = useRunTelemetry(run?.id ?? null, run?.version ?? 0);
  const onMaterialized = useCallback((runId: string) => {
    void getRun(runId).then(onRun).catch(() => undefined);
  }, [onRun]);
  const threadTelemetry = useThreadTelemetry(conversationId, onMaterialized);

  useEffect(() => {
    setLocalThreadId(threadId ?? null);
  }, [threadId]);

  useEffect(() => {
    let active = true;
    getSkills()
      .then((result) => { if (active) setSkills(result.skills); })
      .catch(() => { if (active) setSkills([]); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!run) setSelectedSkills([]);
    else if (run.skill_names && run.skill_names.length > 0) setSelectedSkills(run.skill_names);
  }, [run?.id]);

  function toggleSkill(name: string) {
    setSelectedSkills((current) => current.includes(name)
      ? current.filter((item) => item !== name)
      : [...current, name]);
  }

  function clearError() {
    setError("");
    setPendingAskConflict(false);
  }

  function showOperationError(caught: unknown, fallback: string) {
    const message = caught instanceof Error ? caught.message : "";
    const isPendingAskConflict = message.includes("answer the pending ask before sending another message");
    setPendingAskConflict(isPendingAskConflict);
    setError(isPendingAskConflict ? pendingAskConflictText : message || fallback);
  }

  async function submitContent(content: string): Promise<boolean> {
    clearError();
    setBusy(true);
    try {
      if (onThread && typeof createThread === "function" && typeof submitTurn === "function" && (!run || conversationId)) {
        const firstLine = content.split(/\r?\n/)[0].trim();
        let id = conversationId;
        if (!id) {
          const created = await createThread({ title: firstLine.slice(0, 80) || "新的对话" }, csrfToken);
          id = created.id;
          setLocalThreadId(id);
          onThread?.(id);
        }
        await submitTurn(id, {
          client_turn_id: clientTurnId(),
          content,
          skill_names: selectedSkills,
        }, csrfToken);
        return true;
      }
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
      showOperationError(caught, "发送失败，请检查模型连接后重试");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function runAction(action: () => Promise<Run>) {
    clearError();
    setActionBusy(true);
    try {
      onRun(await action());
    } catch (caught) {
      showOperationError(caught, "操作失败，请稍后重试");
    } finally {
      setActionBusy(false);
    }
  }

  async function chooseDirection(action: "continue_execution" | "modify_plan") {
    const turn = threadTelemetry.activeTurn;
    if (!turn) return;
    clearError();
    setActionBusy(true);
    try {
      const result = await selectDirection(turn.id, {
        action,
        expected_version: turn.version,
        idempotency_key: clientTurnId(),
      }, csrfToken);
      if (result.run) onRun(result.run);
    } catch (caught) {
      showOperationError(caught, "操作失败，请稍后重试");
    } finally {
      setActionBusy(false);
    }
  }

  async function cancelCurrentTurn(turnId: string) {
    setCancelBusy(true);
    try {
      await cancelConversationTurn(turnId, csrfToken);
    } catch (caught) {
      showOperationError(caught, "停止生成失败，请稍后重试");
    } finally {
      setCancelBusy(false);
    }
  }

  async function submitAskAnswers(answers: AskAnswer[]) {
    const turn = threadTelemetry.activeTurn;
    if (!turn || !threadTelemetry.pendingAsk) return;
    clearError();
    setAskBusy(true);
    try {
      await answerAsk(turn.id, {
        expected_version: turn.version,
        idempotency_key: clientTurnId(),
        answers,
      }, csrfToken);
    } catch (caught) {
      showOperationError(caught, "回答提交失败，请稍后重试");
    } finally {
      setAskBusy(false);
    }
  }

  async function stopPendingAskAndKeepDraft() {
    const turn = threadTelemetry.activeTurn;
    if (!turn) return;
    clearError();
    await cancelCurrentTurn(turn.id);
  }

  async function cancelCurrentRun(runId: string): Promise<Run> {
    setCancelBusy(true);
    try {
      return await cancelRun(runId, csrfToken);
    } finally {
      setCancelBusy(false);
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
  const directionTurn = threadTelemetry.activeTurn?.status === "AWAITING_DIRECTION"
    && threadTelemetry.activeTurn.policy === "propose_execution"
    ? threadTelemetry.activeTurn
    : null;
  const directionContext = directionTurn
    ? [...threadTelemetry.events].reverse().find((event) => event.turn_id === directionTurn.id && event.type === "plan.context_loaded")
    : null;
  const directionSource = directionContext
    && typeof directionContext.data.title === "string"
    && typeof directionContext.data.version === "number"
    ? `${directionContext.data.title} · v${directionContext.data.version}`
    : null;
  const decision = approvalRun ? {
    title: "计划已经准备好",
    description: "批准后开始执行；需要调整步骤可以先修改计划。",
    primaryLabel: "批准计划并继续",
    secondaryLabel: "修改计划",
    busy: actionBusy,
    onPrimary: () => void runAction(() => approveCurrentPlan(approvalRun)),
    onSecondary: onOpenPlan,
  } : directionTurn ? {
    title: "这项请求需要确认",
    description: directionSource
      ? `确认后将基于 ${directionSource} 创建执行任务，并进入计划、审批和轨迹流程。`
      : "确认后才会创建执行任务并进入计划、审批和轨迹流程。",
    primaryLabel: "继续执行",
    secondaryLabel: "修改方案",
    busy: actionBusy,
    onPrimary: () => void chooseDirection("continue_execution"),
    onSecondary: () => void chooseDirection("modify_plan"),
  } : undefined;
  const activeTurn = threadTelemetry.activeTurn;
  const threadBusy = Boolean(activeTurn && turnBusyStates.has(activeTurn.status));
  const threadCanCancel = Boolean(conversationId && activeTurn && (threadBusy || threadTelemetry.pendingAsk));
  const runCanCancel = Boolean(run && !["COMPLETED", "CANCELLED", "FAILED"].includes(run.state) && !threadCanCancel);
  const messages = conversationId
    ? [
      ...threadTelemetry.messages,
      ...telemetry.messages.filter((message) => !(message.role === "user" && threadTelemetry.messages.some((item) => item.role === "user" && item.content === message.content))),
    ]
    : telemetry.messages;
  const planReference = latestPlanReference(threadTelemetry.events);

  return (
    <div className={run || conversationId ? "chat-workspace" : "chat-workspace chat-workspace-empty chat-workspace-empty-wide"}>
      <div className="chat-main-column">
        <ConversationThread
          messages={messages}
          busy={busy || actionBusy || telemetry.loading || threadTelemetry.loading || threadBusy}
          title={run || conversationId ? "当前目标对话" : "从一个目标开始"}
          description={run || conversationId ? "模型的每次返回都会留在这里，你可以直接根据它继续补充或调整。" : "描述你想达成的结果，先从一段可用回答开始。"}
          cancelBusy={cancelBusy}
          cancelLabel={threadTelemetry.pendingAsk ? "停止询问" : threadCanCancel ? "停止生成" : "取消任务"}
          onCancel={threadCanCancel ? () => void cancelCurrentTurn(activeTurn!.id) : runCanCancel && run ? () => void runAction(() => cancelCurrentRun(run.id)) : undefined}
          pendingAsk={threadTelemetry.pendingAsk}
          askBusy={askBusy}
          onAskAnswer={submitAskAnswers}
          onAskCancel={() => { if (activeTurn) void cancelCurrentTurn(activeTurn.id); }}
          planReference={planReference}
          onOpenPlan={onOpenPlan}
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
            {isReactBudgetBlocked(run) && <button className="button button-primary" type="button" onClick={() => void runAction(() => recoverFromReactBudget(run))}>继续执行一次</button>}
            {run.state === "AWAITING_OUTCOME" && <button className="button button-primary" type="button" onClick={() => void runAction(() => continueOutcome(run.id, true, csrfToken))}>目标已完成</button>}
            {run.state === "AWAITING_OUTCOME" && <button className="button button-quiet" type="button" onClick={() => void runAction(() => continueOutcome(run.id, false, csrfToken))}>继续观察</button>}
          </div>
        )}
        {error && (
          <div className="error-message" role="alert">
            <span>{error}</span>
            {pendingAskConflict && activeTurn && (
              <button className="button button-danger error-action" type="button" onClick={() => void stopPendingAskAndKeepDraft()}>
                停止询问，保留当前输入
              </button>
            )}
          </div>
        )}
        {threadTelemetry.error && <p className="error-message" role="alert">{threadTelemetry.error}</p>}
      </div>

      {(run || conversationId) && (
        <ActivityRail
          run={run}
          thread={threadTelemetry.thread}
          events={telemetry.events}
          threadEvents={threadTelemetry.events}
          stats={telemetry.stats}
          loading={run ? telemetry.loading : threadTelemetry.loading}
          onOpenTrajectory={onOpenTrajectory}
        />
      )}
    </div>
  );
}
