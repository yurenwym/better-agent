import type {
  AskAnswer,
  PendingAsk,
  Bootstrap,
  EventRecord,
  GoalResponse,
  MessageRecord,
  MemoryRecord,
  PlanDocument,
  PlanDocumentVersion,
  PlanResponse,
  PlanVersion,
  Run,
  SkillDefinition,
  Stats,
  Thread,
  ThreadEvent,
  ThreadMessage,
  ThreadPlanResponse,
  Turn,
} from "./types";

export type Fetcher = typeof fetch;

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

function readableError(raw: string, fallback: string): string {
  if (!raw) return fallback;
  try {
    const payload = JSON.parse(raw) as { detail?: unknown };
    if (typeof payload.detail === "string" && payload.detail.trim()) return payload.detail;
    return fallback;
  } catch {
    return raw;
  }
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(readableError(detail, `请求失败（${response.status}）`));
  }
  return response.json() as Promise<T>;
}

async function planJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text();
    let payload: unknown = detail;
    try { payload = JSON.parse(detail) as unknown; } catch { /* keep text */ }
    const message = payload && typeof payload === "object" && typeof (payload as Record<string, unknown>).detail === "string"
      ? String((payload as Record<string, unknown>).detail)
      : `Plan request failed (${response.status})`;
    throw new ApiError(message, response.status, payload);
  }
  return response.json() as Promise<T>;
}

function mutationHeaders(csrfToken: string): HeadersInit {
  return { "Content-Type": "application/json", "X-CSRF-Token": csrfToken };
}

export async function getBootstrap(fetcher: Fetcher = fetch): Promise<Bootstrap> {
  return json<Bootstrap>(await fetcher("/api/bootstrap"));
}

export async function getSkills(fetcher: Fetcher = fetch): Promise<{ skills: SkillDefinition[] }> {
  return json<{ skills: SkillDefinition[] }>(await fetcher("/api/skills"));
}

export async function createThread(
  payload: { title?: string },
  csrfToken: string,
  fetcher: Fetcher = fetch,
): Promise<Thread> {
  return json<Thread>(await fetcher("/api/threads", {
    method: "POST",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify(payload),
  }));
}

export interface TurnSubmission {
  thread_id: string;
  turn_id: string;
  status: string;
  version: number;
  event_cursor: number;
}

export async function submitTurn(
  threadId: string,
  payload: { client_turn_id: string; content: string; skill_names: string[] },
  csrfToken: string,
  fetcher: Fetcher = fetch,
): Promise<TurnSubmission> {
  return json<TurnSubmission>(await fetcher(`/api/threads/${threadId}/turns`, {
    method: "POST",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify(payload),
  }));
}

export async function getThread(threadId: string, fetcher: Fetcher = fetch): Promise<Thread> {
  return json<Thread>(await fetcher(`/api/threads/${threadId}`));
}

export async function getThreadPlan(threadId: string, fetcher: Fetcher = fetch): Promise<ThreadPlanResponse> {
  return planJson<ThreadPlanResponse>(await fetcher(`/api/threads/${threadId}/plan`));
}

export async function getPlanDocument(planDocumentId: string, fetcher: Fetcher = fetch): Promise<PlanDocument> {
  return planJson<PlanDocument>(await fetcher(`/api/plans/${planDocumentId}`));
}

export async function getPlanVersions(planDocumentId: string, fetcher: Fetcher = fetch): Promise<{ versions: PlanDocumentVersion[] }> {
  return planJson<{ versions: PlanDocumentVersion[] }>(await fetcher(`/api/plans/${planDocumentId}/versions`));
}

export async function getPlanVersion(planDocumentId: string, version: number, fetcher: Fetcher = fetch): Promise<PlanDocumentVersion> {
  return planJson<PlanDocumentVersion>(await fetcher(`/api/plans/${planDocumentId}/versions/${version}`));
}

export async function getPlanFile(planDocumentId: string, fetcher: Fetcher = fetch): Promise<string> {
  const response = await fetcher(`/api/plans/${planDocumentId}/file`);
  if (!response.ok) throw new Error(`plan file request failed (${response.status})`);
  return response.text();
}

export interface PlanDocumentWritePayload {
  expected_version: number;
  expected_content_hash: string;
  title: string;
  markdown: string;
  change_summary?: string;
}

export async function putPlanDocument(
  planDocumentId: string,
  payload: PlanDocumentWritePayload,
  csrfToken: string,
  fetcher: Fetcher = fetch,
): Promise<PlanDocumentVersion> {
  return planJson<PlanDocumentVersion>(await fetcher(`/api/plans/${planDocumentId}`, {
    method: "PUT",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify(payload),
  }));
}

export async function restorePlanDocument(
  planDocumentId: string,
  payload: { version: number; expected_version: number; expected_content_hash: string },
  csrfToken: string,
  fetcher: Fetcher = fetch,
): Promise<PlanDocumentVersion> {
  return planJson<PlanDocumentVersion>(await fetcher(`/api/plans/${planDocumentId}/restore`, {
    method: "POST",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify(payload),
  }));
}

export async function syncPlanFile(planDocumentId: string, csrfToken: string, fetcher: Fetcher = fetch): Promise<PlanDocumentVersion> {
  return planJson<PlanDocumentVersion>(await fetcher(`/api/plans/${planDocumentId}/sync-file`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: "{}",
  }));
}

export async function retryPlanProjection(planDocumentId: string, csrfToken: string, fetcher: Fetcher = fetch): Promise<PlanDocument> {
  return planJson<PlanDocument>(await fetcher(`/api/plans/${planDocumentId}/retry-projection`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: "{}",
  }));
}

export async function getThreadMessages(threadId: string, fetcher: Fetcher = fetch): Promise<{ messages: ThreadMessage[] }> {
  return json<{ messages: ThreadMessage[] }>(await fetcher(`/api/threads/${threadId}/messages`));
}

export async function getThreadEvents(threadId: string, afterSeq = 0, fetcher: Fetcher = fetch): Promise<{ events: ThreadEvent[] }> {
  return json<{ events: ThreadEvent[] }>(await fetcher(`/api/threads/${threadId}/events?after_seq=${afterSeq}`));
}

export async function cancelTurn(turnId: string, csrfToken: string, fetcher: Fetcher = fetch): Promise<Turn> {
  return json<Turn>(await fetcher(`/api/turns/${turnId}/cancel`, {
    method: "POST",
    headers: mutationHeaders(csrfToken),
    body: "{}",
  }));
}

export async function getPendingAsk(turnId: string, fetcher: Fetcher = fetch): Promise<PendingAsk> {
  return json<PendingAsk>(await fetcher(`/api/turns/${turnId}/ask`));
}

export async function answerAsk(
  turnId: string,
  payload: { expected_version: number; idempotency_key: string; answers: AskAnswer[] },
  csrfToken: string,
  fetcher: Fetcher = fetch,
): Promise<{ ask_id: string; turn: Turn }> {
  return json<{ ask_id: string; turn: Turn }>(await fetcher(`/api/turns/${turnId}/ask/answer`, {
    method: "POST",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify(payload),
  }));
}

export async function selectDirection(
  turnId: string,
  payload: { action: "continue_execution" | "modify_plan"; expected_version: number; idempotency_key: string },
  csrfToken: string,
  fetcher: Fetcher = fetch,
): Promise<{ turn: Turn; run?: Run }> {
  return json<{ turn: Turn; run?: Run }>(await fetcher(`/api/turns/${turnId}/direction`, {
    method: "POST",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify(payload),
  }));
}

export async function createGoal(
  payload: { title: string; description: string },
  csrfToken: string,
  fetcher: Fetcher = fetch,
): Promise<GoalResponse> {
  return json<GoalResponse>(await fetcher("/api/goals", {
    method: "POST",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify(payload),
  }));
}

export async function sendMessage(
  goalId: string,
  content: string,
  csrfToken: string,
  skillNames: string[] = [],
  fetcher: Fetcher = fetch,
): Promise<Run> {
  return json<Run>(await fetcher(`/api/goals/${goalId}/messages`, {
    method: "POST",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify({ content, skill_names: skillNames }),
  }));
}

export async function getRun(runId: string): Promise<Run> {
  return json<Run>(await fetch(`/api/runs/${runId}`));
}

export async function resumeRun(runId: string, csrfToken: string): Promise<Run> {
  return json<Run>(await fetch(`/api/runs/${runId}/resume`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: "{}",
  }));
}

export async function continueOutcome(runId: string, finished: boolean, csrfToken: string): Promise<Run> {
  return json<Run>(await fetch(`/api/runs/${runId}/outcome`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: JSON.stringify({ finished }),
  }));
}

export async function cancelRun(runId: string, csrfToken: string): Promise<Run> {
  return json<Run>(await fetch(`/api/runs/${runId}/cancel`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: "{}",
  }));
}

export async function addBudget(runId: string, amount: number, csrfToken: string): Promise<Run> {
  return json<Run>(await fetch(`/api/runs/${runId}/budget`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: JSON.stringify({ amount }),
  }));
}

export async function getPlans(runId: string): Promise<PlanResponse> {
  return json<PlanResponse>(await fetch(`/api/runs/${runId}/plans`));
}

export async function approvePlan(runId: string, version: number, csrfToken: string): Promise<Run> {
  return json<Run>(await fetch(`/api/runs/${runId}/plans/${version}/approve`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: "{}",
  }));
}

export async function revisePlan(
  runId: string,
  expectedVersion: number,
  steps: Array<{ id: string; title: string }>,
  csrfToken: string,
): Promise<PlanVersion> {
  return json<PlanVersion>(await fetch(`/api/runs/${runId}/plans/revise`, {
    method: "POST",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify({ expected_version: expectedVersion, steps }),
  }));
}

export async function cancelStep(runId: string, stepId: string, csrfToken: string): Promise<Run> {
  return json<Run>(await fetch(`/api/runs/${runId}/steps/${stepId}/cancel`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: "{}",
  }));
}

export async function grantApproval(approvalId: string, csrfToken: string): Promise<Run> {
  return json<Run>(await fetch(`/api/approvals/${approvalId}/grant`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: "{}",
  }));
}

export async function rejectApproval(approvalId: string, csrfToken: string): Promise<Run> {
  return json<Run>(await fetch(`/api/approvals/${approvalId}/reject`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: "{}",
  }));
}

export async function getEvents(runId: string, afterSeq = 0): Promise<{ events: EventRecord[] }> {
  return json<{ events: EventRecord[] }>(await fetch(`/api/runs/${runId}/events?after_seq=${afterSeq}`));
}

export async function getMessages(runId: string): Promise<{ messages: MessageRecord[] }> {
  return json<{ messages: MessageRecord[] }>(await fetch(`/api/runs/${runId}/messages`));
}

export async function getStats(runId: string): Promise<Stats> {
  return json<Stats>(await fetch(`/api/runs/${runId}/stats`));
}

export async function getMemories(): Promise<{ memories: MemoryRecord[] }> {
  return json<{ memories: MemoryRecord[] }>(await fetch("/api/memories"));
}

export async function confirmMemory(memoryId: string, csrfToken: string, content?: string): Promise<MemoryRecord> {
  return json<MemoryRecord>(await fetch(`/api/memories/${memoryId}/confirm`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: JSON.stringify(content ? { content } : {}),
  }));
}

export async function rejectMemory(memoryId: string, csrfToken: string): Promise<MemoryRecord> {
  return json<MemoryRecord>(await fetch(`/api/memories/${memoryId}/reject`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: "{}",
  }));
}

export async function disableMemory(memoryId: string, csrfToken: string): Promise<MemoryRecord> {
  return json<MemoryRecord>(await fetch(`/api/memories/${memoryId}/disable`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: "{}",
  }));
}

export async function editMemory(memoryId: string, content: string, csrfToken: string): Promise<MemoryRecord> {
  return json<MemoryRecord>(await fetch(`/api/memories/${memoryId}`, {
    method: "PATCH", headers: mutationHeaders(csrfToken), body: JSON.stringify({ content }),
  }));
}

export async function rollbackMemory(memoryId: string, version: number, csrfToken: string): Promise<MemoryRecord> {
  return json<MemoryRecord>(await fetch(`/api/memories/${memoryId}/rollback`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: JSON.stringify({ version }),
  }));
}

export function subscribeToEvents(
  runId: string,
  lastEventId: number,
  onEvent: (event: EventRecord) => void,
): () => void {
  let cursor = lastEventId;
  const source = new EventSource(`/api/runs/${runId}/events/stream?after_seq=${encodeURIComponent(lastEventId)}&follow=1`, { withCredentials: false });
  const handler = (raw: Event) => {
    const message = raw as MessageEvent<string>;
    try {
      const event = JSON.parse(message.data) as EventRecord;
      const eventSeq = event.seq || Number(message.lastEventId || 0);
      if (eventSeq <= cursor) return;
      cursor = eventSeq;
      onEvent(event);
      if (["run.completed", "run.failed", "run.cancelled"].includes(event.type)) {
        source.close();
      }
    } catch { /* malformed SSE is ignored until reconnect */ }
  };
  source.addEventListener("trajectory", handler);
  source.onerror = () => { /* native EventSource reconnects and carries Last-Event-ID */ };
  return () => source.close();
}

export function subscribeToThreadEvents(
  threadId: string,
  lastEventId: number,
  onEvent: (event: ThreadEvent) => void,
): () => void {
  let cursor = lastEventId;
  const source = new EventSource(
    `/api/threads/${threadId}/events/stream?after_seq=${encodeURIComponent(lastEventId)}&follow=1`,
    { withCredentials: false },
  );
  const handler = (raw: Event) => {
    const message = raw as MessageEvent<string>;
    try {
      const event = JSON.parse(message.data) as ThreadEvent;
      if (event.seq <= cursor) return;
      cursor = event.seq;
      onEvent(event);
      const continuationId = event.data.continuation_turn_id;
      const hasContinuation = typeof continuationId === "string" && continuationId.length > 0;
      if ((event.type === "turn.completed" && !hasContinuation)
        || ["turn.failed", "turn.cancelled"].includes(event.type)) {
        source.close();
      }
    } catch {
      // Native EventSource reconnects with Last-Event-ID after malformed input.
    }
  };
  source.addEventListener("conversation", handler);
  source.onerror = () => undefined;
  return () => source.close();
}
