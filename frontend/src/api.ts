import type {
  AskAnswer,
  PendingAsk,
  Bootstrap,
  EventRecord,
  GoalResponse,
  MessageRecord,
  MemoryRecord,
  MemoryEntry,
  MemoryProposal,
  MemoryEpisode,
  PlanDocument,
  PlanDocumentSummary,
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
  ResearchJob,
  ResearchSchedule,
  NotificationChannel,
  GoalProgram,
  GoalWorkspace,
  GoalDailyReview,
  TodayResponse,
  GoalAdjustmentProposal,
  GoalAction,
  TodayProgramGroup,
  AgentArtifact,
  AgentEvent,
  AgentRun,
  AgentTask,
  EvolutionCandidate,
  GrowthProfile,
  ModelProfileRecord, RoutingPolicy, CostSummary, EvaluationRunRecord, EvaluationProgressEvent,
  EvaluationReport, SkillInstallPreview, SkillVersionRecord, TrustedConnectorRecord,
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

function idempotentHeaders(csrfToken:string):HeadersInit { return {...mutationHeaders(csrfToken),"Idempotency-Key":crypto.randomUUID()}; }

export async function listModelProfiles(fetcher:Fetcher=fetch):Promise<{profiles:ModelProfileRecord[]}>{return json(await fetcher("/api/model-profiles"));}
export async function listRoutingPolicies(fetcher:Fetcher=fetch):Promise<{policies:RoutingPolicy[]}>{return json(await fetcher("/api/model-routing-policies"));}
export async function createRoutingPolicy(payload:Record<string,unknown>,csrf:string,fetcher:Fetcher=fetch):Promise<RoutingPolicy>{return json(await fetcher("/api/model-routing-policies",{method:"POST",headers:idempotentHeaders(csrf),body:JSON.stringify(payload)}));}
export async function createModelProfile(payload:Record<string,unknown>,csrf:string,fetcher:Fetcher=fetch):Promise<ModelProfileRecord>{return json(await fetcher("/api/model-profiles",{method:"POST",headers:idempotentHeaders(csrf),body:JSON.stringify(payload)}));}
export async function verifyModelVersion(id:string,csrf:string,fetcher:Fetcher=fetch){return json(await fetcher(`/api/model-profile-versions/${id}/verify`,{method:"POST",headers:idempotentHeaders(csrf),body:"{}"}));}
export async function getCostSummary(periodKind="DAILY",periodKey=new Date().toISOString().slice(0,10),fetcher:Fetcher=fetch):Promise<CostSummary>{return json(await fetcher(`/api/cost/summary?period_kind=${encodeURIComponent(periodKind)}&period_key=${encodeURIComponent(periodKey)}`));}
export async function getUsageSummary(fetcher:Fetcher=fetch):Promise<{groups:import("./types").UsageGroup[]}>{return json(await fetcher("/api/usage/summary"));}
export async function setCostBudget(limit_microusd:number,csrf:string,periodKind="DAILY",periodKey=new Date().toISOString().slice(0,10),fetcher:Fetcher=fetch):Promise<CostSummary>{return json(await fetcher("/api/cost/budgets",{method:"PUT",headers:idempotentHeaders(csrf),body:JSON.stringify({period_kind:periodKind,period_key:periodKey,limit_microusd})}));}
export async function createEvaluationRun(payload:Record<string,unknown>,csrf:string,fetcher:Fetcher=fetch):Promise<EvaluationRunRecord>{return json(await fetcher("/api/evaluation-runs",{method:"POST",headers:idempotentHeaders(csrf),body:JSON.stringify(payload)}));}
export async function listEvaluationSuites(fetcher:Fetcher=fetch):Promise<{suites:Array<{id:string;digest:string;kind:string;case_count:number}>}>{return json(await fetcher("/api/evaluation-suites"));}
export async function getEvaluationRun(id:string,fetcher:Fetcher=fetch):Promise<EvaluationRunRecord>{return json(await fetcher(`/api/evaluation-runs/${id}`));}
export async function getEvaluationEvents(id:string,after=0,fetcher:Fetcher=fetch):Promise<{events:EvaluationProgressEvent[]}>{return json(await fetcher(`/api/evaluation-runs/${id}/events?after_seq=${after}`));}
export async function getEvaluationReport(id:string,fetcher:Fetcher=fetch):Promise<EvaluationReport>{return json(await fetcher(`/api/evaluation-runs/${id}/report`));}
export async function cancelEvaluationRun(id:string,csrf:string,fetcher:Fetcher=fetch):Promise<EvaluationRunRecord>{return json(await fetcher(`/api/evaluation-runs/${id}/cancel`,{method:"POST",headers:idempotentHeaders(csrf),body:"{}"}));}
export function subscribeToEvaluationEvents(id:string,after:number,onEvent:(event:EvaluationProgressEvent)=>void):()=>void{let cursor=after;const source=new EventSource(`/api/evaluation-runs/${id}/events/stream?after_seq=${encodeURIComponent(after)}`);const handler=(raw:Event)=>{try{const message=raw as MessageEvent<string>,event=JSON.parse(message.data) as EvaluationProgressEvent;if(event.seq<=cursor)return;cursor=event.seq;onEvent(event);}catch{/* malformed SSE is ignored */}};source.addEventListener("evaluation",handler);source.onerror=()=>undefined;return()=>source.close();}
export async function listSkillVersions(id:string,fetcher:Fetcher=fetch):Promise<{versions:SkillVersionRecord[]}>{return json(await fetcher(`/api/skills/${id}/versions`));}
export async function listTrustedConnectors(fetcher:Fetcher=fetch):Promise<{connectors:TrustedConnectorRecord[]}>{return json(await fetcher("/api/trusted-connectors"));}
export async function setSkillVersionEnabled(id:string,enabled:boolean,csrf:string,fetcher:Fetcher=fetch):Promise<SkillVersionRecord>{return json(await fetcher(`/api/skill-versions/${id}/${enabled?"enable":"disable"}`,{method:"POST",headers:idempotentHeaders(csrf),body:"{}"}));}
export async function uninstallSkill(id:string,csrf:string,fetcher:Fetcher=fetch):Promise<void>{const response=await fetcher(`/api/skills/${id}`,{method:"DELETE",headers:idempotentHeaders(csrf),body:"{}"});if(!response.ok)throw new Error("卸载 Skill 失败");}
export async function previewSkillInstall(file:File,csrf:string,fetcher:Fetcher=fetch):Promise<SkillInstallPreview>{return json(await fetcher("/api/skills/install",{method:"POST",headers:{"Content-Type":"application/zip","X-CSRF-Token":csrf},body:file}));}
export async function confirmSkillInstall(token:string,tools:string[],csrf:string,fetcher:Fetcher=fetch):Promise<SkillVersionRecord>{return json(await fetcher("/api/skills/confirm-install",{method:"POST",headers:idempotentHeaders(csrf),body:JSON.stringify({install_token:token,granted_tools:tools})}));}
export async function listInstalledSkills(fetcher:Fetcher=fetch):Promise<{skills:SkillDefinition[]}>{return json(await fetcher("/api/skills?include_disabled=true"));}

export async function getGoalWorkspace(resourceId: string, fetcher: Fetcher = fetch): Promise<GoalWorkspace> {
  return json<GoalWorkspace>(await fetcher(`/api/workspaces/${encodeURIComponent(resourceId)}`));
}

export async function getGrowthProfile(fetcher: Fetcher = fetch): Promise<GrowthProfile> {
  return json<GrowthProfile>(await fetcher("/api/growth/profile"));
}

function goalMutationHeaders(csrfToken: string, idempotencyKey: string): HeadersInit {
  return { ...mutationHeaders(csrfToken), "Idempotency-Key": idempotencyKey };
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

export async function listThreads(fetcher: Fetcher = fetch): Promise<{ threads: Thread[] }> {
  return json<{ threads: Thread[] }>(await fetcher("/api/threads"));
}

export async function deleteThread(threadId: string, csrfToken: string, fetcher: Fetcher = fetch): Promise<void> {
  const response = await fetcher(`/api/threads/${threadId}`, {
    method: "DELETE",
    headers: mutationHeaders(csrfToken),
    body: "{}",
  });
  if (!response.ok) throw new Error("删除会话失败");
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

export async function createExpertRun(threadId: string, objective: string, idempotencyKey: string, csrfToken: string, fetcher: Fetcher = fetch): Promise<AgentRun> {
  return json<AgentRun>(await fetcher(`/api/threads/${threadId}/expert-runs`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: JSON.stringify({ objective, idempotency_key: idempotencyKey }),
  }));
}

export async function getAgentRun(runId: string, fetcher: Fetcher = fetch): Promise<AgentRun> {
  return json<AgentRun>(await fetcher(`/api/agent-runs/${runId}`));
}

export async function getLatestExpertRun(threadId: string, fetcher: Fetcher = fetch): Promise<AgentRun | null> {
  const response = await fetcher(`/api/threads/${threadId}/expert-runs/latest`);
  if (response.status === 404) return null;
  return json<AgentRun>(response);
}

export async function getAgentTasks(runId: string, fetcher: Fetcher = fetch): Promise<{ tasks: AgentTask[] }> {
  return json<{ tasks: AgentTask[] }>(await fetcher(`/api/agent-runs/${runId}/tasks`));
}

export async function getAgentArtifacts(runId: string, fetcher: Fetcher = fetch): Promise<{ artifacts: AgentArtifact[] }> {
  return json<{ artifacts: AgentArtifact[] }>(await fetcher(`/api/agent-runs/${runId}/artifacts`));
}

export async function getAgentEvents(runId: string, afterSeq = 0, fetcher: Fetcher = fetch): Promise<{ events: AgentEvent[] }> {
  return json<{ events: AgentEvent[] }>(await fetcher(`/api/agent-runs/${runId}/events?after_seq=${afterSeq}`));
}

export async function cancelAgentRun(runId: string, csrfToken: string, fetcher: Fetcher = fetch): Promise<AgentRun> {
  return json<AgentRun>(await fetcher(`/api/agent-runs/${runId}/cancel`, {
    method: "POST", headers: mutationHeaders(csrfToken), body: JSON.stringify({ reason: "用户取消" }),
  }));
}

export async function listEvolutionCandidates(fetcher: Fetcher = fetch): Promise<{ candidates: EvolutionCandidate[] }> {
  const result = await json<{ candidates: Array<Record<string, unknown>> }>(await fetcher("/api/evolution/candidates"));
  return { candidates: result.candidates.map(evolutionCandidate) };
}

function stringList(value: unknown): string[] { return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []; }

function evolutionCandidate(raw: Record<string, unknown>): EvolutionCandidate {
  const permission = raw.permission_diff && typeof raw.permission_diff === "object" ? raw.permission_diff as Record<string, unknown> : {};
  const evaluation = raw.evaluation && typeof raw.evaluation === "object" ? raw.evaluation as Record<string, unknown> : null;
  const evaluationMetrics = evaluation?.metrics && typeof evaluation.metrics === "object" ? evaluation.metrics as Record<string, unknown> : {};
  const kind = String(raw.kind ?? raw.candidate_type ?? "policy") as EvolutionCandidate["kind"];
  return {
    id: String(raw.id), kind,
    title: String(raw.title ?? `${{ memory: "记忆", skill: "技能", policy: "策略", prompt: "提示词", code: "代码" }[kind] ?? "能力"}候选`),
    summary: String(raw.summary ?? raw.reason ?? "等待查看候选详情"),
    status: String(raw.status ?? "DRAFT") as EvolutionCandidate["status"],
    version: Number(raw.version ?? 0), risk_level: String(raw.risk_level ?? (kind === "code" ? "high" : "medium")),
    evidence_count: Number(raw.evidence_count ?? (Array.isArray(raw.experience_ids) ? raw.experience_ids.length : 0)),
    evidence_refs: stringList(raw.evidence_refs ?? raw.experience_ids),
    evidence_source_kinds: stringList(raw.evidence_source_kinds),
    record_origin: ["demo", "manual", "observed"].includes(String(raw.record_origin)) ? raw.record_origin as EvolutionCandidate["record_origin"] : undefined,
    reason: typeof raw.reason === "string" ? raw.reason : undefined,
    proposed_content: raw.proposed_content && typeof raw.proposed_content === "object" ? raw.proposed_content as Record<string, unknown> : undefined,
    evaluation: evaluation ? {
      id: typeof evaluation.id === "string" ? evaluation.id : undefined,
      report_digest: typeof evaluation.report_digest === "string" ? evaluation.report_digest : undefined,
      status: String(evaluation.status ?? "COMPLETED"),
      deterministic_pass: typeof evaluation.deterministic_pass === "boolean" ? evaluation.deterministic_pass : null,
      score_delta: typeof evaluation.score_delta === "number" ? evaluation.score_delta
        : evaluation.metrics && typeof evaluation.metrics === "object" && typeof (evaluation.metrics as Record<string, unknown>).score_delta === "number"
          ? (evaluation.metrics as Record<string, number>).score_delta : null,
      regressions: stringList(evaluation.regressions).length ? stringList(evaluation.regressions)
        : evaluation.checks && typeof evaluation.checks === "object"
          ? Object.entries(evaluation.checks as Record<string, unknown>).filter(([, passed]) => passed !== true).map(([name]) => name)
          : [],
      passed: evaluation.metrics && typeof evaluation.metrics === "object" && typeof (evaluation.metrics as Record<string, unknown>).passed === "number" ? Number((evaluation.metrics as Record<string, unknown>).passed) : null,
      total: evaluation.metrics && typeof evaluation.metrics === "object" && typeof (evaluation.metrics as Record<string, unknown>).total === "number" ? Number((evaluation.metrics as Record<string, unknown>).total) : null,
      baseline_correct: typeof evaluationMetrics.baseline_correct === "number" ? evaluationMetrics.baseline_correct : null,
      candidate_correct: typeof evaluationMetrics.candidate_correct === "number" ? evaluationMetrics.candidate_correct : null,
      quality_delta: typeof evaluationMetrics.quality_delta === "number" ? evaluationMetrics.quality_delta : null,
      safety_violations: typeof evaluationMetrics.safety_violations === "number" ? evaluationMetrics.safety_violations : null,
    } : null,
    permission_diff: { added: stringList(permission.added), removed: stringList(permission.removed), unchanged: stringList(permission.unchanged) },
    canary: raw.canary && typeof raw.canary === "object" ? raw.canary as EvolutionCandidate["canary"] : null,
    rollback: raw.rollback && typeof raw.rollback === "object" ? raw.rollback as EvolutionCandidate["rollback"] : null,
    approval_id: typeof raw.approval_id === "string" ? raw.approval_id : null,
    created_at: String(raw.created_at ?? ""), updated_at: String(raw.updated_at ?? raw.created_at ?? ""),
  };
}

export async function getEvolutionCandidate(candidateId: string, fetcher: Fetcher = fetch): Promise<EvolutionCandidate> {
  return evolutionCandidate(await json<Record<string, unknown>>(await fetcher(`/api/evolution/candidates/${candidateId}`)));
}

async function evolve(candidateId: string, operation: "evaluate" | "approve" | "reject" | "start-canary" | "promote" | "rollback", expectedVersion: number, csrfToken: string, fetcher: Fetcher): Promise<unknown> {
  const idempotencyKey = clientRequestId();
  return json<unknown>(await fetcher(`/api/evolution/candidates/${candidateId}/${operation}`, {
    method: "POST",
    headers: goalMutationHeaders(csrfToken, idempotencyKey),
    body: JSON.stringify({ expected_version: expectedVersion, actor: "local-user" }),
  }));
}

function clientRequestId(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `request-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export const evaluateEvolutionCandidate = (id: string, version: number, csrf: string, fetcher: Fetcher = fetch) => evolve(id, "evaluate", version, csrf, fetcher);
export const approveEvolutionCandidate = (id: string, version: number, csrf: string, fetcher: Fetcher = fetch) => evolve(id, "approve", version, csrf, fetcher);
export const rejectEvolutionCandidate = (id: string, version: number, csrf: string, fetcher: Fetcher = fetch) => evolve(id, "reject", version, csrf, fetcher);
export const startEvolutionCanary = (id: string, version: number, csrf: string, fetcher: Fetcher = fetch) => evolve(id, "start-canary", version, csrf, fetcher);
export const promoteEvolutionCandidate = (id: string, version: number, csrf: string, fetcher: Fetcher = fetch) => evolve(id, "promote", version, csrf, fetcher);
export const rollbackEvolutionCandidate = (id: string, version: number, csrf: string, fetcher: Fetcher = fetch) => evolve(id, "rollback", version, csrf, fetcher);

export async function listPlanDocuments(fetcher: Fetcher = fetch): Promise<{ plans: PlanDocumentSummary[] }> {
  return planJson<{ plans: PlanDocumentSummary[] }>(await fetcher("/api/plans"));
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

export async function deletePlanDocument(
  planDocumentId: string,
  payload: { expected_version: number; expected_content_hash: string },
  csrfToken: string,
  fetcher: Fetcher = fetch,
): Promise<void> {
  const response = await fetcher(`/api/plans/${planDocumentId}`, {
    method: "DELETE",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify(payload),
  });
  if (!response.ok) await planJson<never>(response);
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

export async function previewGoalProgram(planDocumentId: string, payload: { start_date: string; requested_end_date?: string; timezone: string; daily_minutes: number }, key: string, csrf: string, fetcher: Fetcher = fetch): Promise<GoalProgram> {
  return planJson(await fetcher(`/api/plans/${planDocumentId}/program-preview`, { method:"POST", headers:goalMutationHeaders(csrf,key), body:JSON.stringify(payload) }));
}
export async function activateGoalProgram(programId: string, expectedVersion: number, key: string, csrf: string, fetcher: Fetcher = fetch): Promise<GoalProgram> {
  return planJson(await fetcher(`/api/programs/${programId}/activate`, { method:"POST", headers:goalMutationHeaders(csrf,key), body:JSON.stringify({expected_version:expectedVersion}) }));
}
export async function retryGoalProgramCompile(programId: string, expectedVersion: number, key: string, csrf: string, fetcher: Fetcher = fetch): Promise<GoalProgram> {
  return planJson(await fetcher(`/api/programs/${programId}/compile-retry`, { method:"POST", headers:goalMutationHeaders(csrf,key), body:JSON.stringify({expected_version:expectedVersion}) }));
}
export async function getGoalProgram(programId: string, fetcher: Fetcher = fetch): Promise<GoalProgram> { return planJson(await fetcher(`/api/programs/${programId}`)); }
export async function listGoalPrograms(fetcher: Fetcher = fetch): Promise<{programs:GoalProgram[]}> { return planJson(await fetcher("/api/programs")); }
export async function deleteGoalProgram(programId:string,expectedVersion:number,key:string,csrf:string,fetcher:Fetcher=fetch):Promise<void>{const response=await fetcher(`/api/programs/${programId}`,{method:"DELETE",headers:goalMutationHeaders(csrf,key),body:JSON.stringify({expected_version:expectedVersion})});if(!response.ok)await planJson(response);}
export async function getGoalAction(actionId:string,fetcher:Fetcher=fetch):Promise<{action:GoalAction;program:TodayProgramGroup["program"]}>{return planJson(await fetcher(`/api/actions/${actionId}`));}
export async function getToday(date?: string, fetcher: Fetcher = fetch): Promise<TodayResponse> { return planJson(await fetcher(`/api/today${date ? `?date=${encodeURIComponent(date)}` : ""}`)); }
export async function getGoalReview(programId:string,localDate:string,fetcher:Fetcher=fetch):Promise<GoalDailyReview|null>{const response=await fetcher(`/api/programs/${programId}/reviews/${localDate}`);if(response.status===204)return null;return planJson(response);}
export async function retryGoalReview(reviewId:string,key:string,csrf:string,fetcher:Fetcher=fetch):Promise<GoalDailyReview>{return planJson(await fetcher(`/api/reviews/${reviewId}/retry`,{method:"POST",headers:goalMutationHeaders(csrf,key),body:"{}"}));}
export async function mutateGoalAction(actionId: string, operation: "complete"|"skip"|"defer"|"feedback", payload:Record<string,unknown>, key:string, csrf:string, fetcher:Fetcher=fetch):Promise<unknown>{
  return planJson(await fetcher(`/api/actions/${actionId}/${operation}`,{method:"POST",headers:goalMutationHeaders(csrf,key),body:JSON.stringify(payload)}));
}
export async function transitionGoalProgram(programId:string,operation:"pause"|"resume"|"complete"|"cancel",expectedVersion:number,key:string,csrf:string,fetcher:Fetcher=fetch):Promise<GoalProgram>{
  return planJson(await fetcher(`/api/programs/${programId}/${operation}`,{method:"POST",headers:goalMutationHeaders(csrf,key),body:JSON.stringify({expected_version:expectedVersion})}));
}
export async function requestGoalActionHelp(actionId:string,content:string,expectedVersion:number,key:string,csrf:string,fetcher:Fetcher=fetch):Promise<TurnSubmission & {action_id:string}>{
  return planJson(await fetcher(`/api/actions/${actionId}/request-help`,{method:"POST",headers:goalMutationHeaders(csrf,key),body:JSON.stringify({content,expected_version:expectedVersion})}));
}
export async function proposeGoalAdjustment(programId:string,reason:string,expectedVersion:number,key:string,csrf:string,fetcher:Fetcher=fetch):Promise<GoalAdjustmentProposal>{return planJson(await fetcher(`/api/programs/${programId}/adjustments`,{method:"POST",headers:goalMutationHeaders(csrf,key),body:JSON.stringify({reason,expected_version:expectedVersion})}));}
export async function decideGoalAdjustment(proposalId:string,decision:"accept"|"reject",expectedVersion:number,key:string,csrf:string,fetcher:Fetcher=fetch):Promise<{proposal:GoalAdjustmentProposal;program:GoalProgram}|GoalAdjustmentProposal>{return planJson(await fetcher(`/api/adjustments/${proposalId}/${decision}`,{method:"POST",headers:goalMutationHeaders(csrf,key),body:JSON.stringify({expected_version:expectedVersion})}));}
export async function syncGoalAdjustment(proposalId:string,expectedVersion:number,key:string,csrf:string,rebaseToCurrent=false,fetcher:Fetcher=fetch):Promise<{proposal:GoalAdjustmentProposal;plan_document_version_id:string}>{return planJson(await fetcher(`/api/adjustments/${proposalId}/sync-plan-document`,{method:"POST",headers:goalMutationHeaders(csrf,key),body:JSON.stringify({expected_version:expectedVersion,rebase_to_current:rebaseToCurrent})}));}

export async function getThreadMessages(threadId: string, fetcher: Fetcher = fetch): Promise<{ messages: ThreadMessage[] }> {
  return json<{ messages: ThreadMessage[] }>(await fetcher(`/api/threads/${threadId}/messages`));
}

export async function getThreadEvents(threadId: string, afterSeq = 0, fetcher: Fetcher = fetch): Promise<{ events: ThreadEvent[] }> {
  return json<{ events: ThreadEvent[] }>(await fetcher(`/api/threads/${threadId}/events?after_seq=${afterSeq}`));
}

export async function getResearchJobs(threadId?: string, fetcher: Fetcher = fetch): Promise<{jobs:ResearchJob[]}> { return json(await fetcher(`/api/research/jobs${threadId ? `?thread_id=${encodeURIComponent(threadId)}` : ""}`)); }
export async function createResearch(threadId:string,payload:{topic:string;client_request_id:string;source_scopes:string[]},csrf:string,fetcher:Fetcher=fetch):Promise<{job_id:string;status:string;event_cursor:number}>{return json(await fetcher(`/api/threads/${threadId}/research`,{method:"POST",headers:mutationHeaders(csrf),body:JSON.stringify(payload)}));}
export async function getResearchJob(id:string,fetcher:Fetcher=fetch):Promise<ResearchJob>{return json(await fetcher(`/api/research/jobs/${id}`));}
export async function cancelResearch(id:string,csrf:string,fetcher:Fetcher=fetch):Promise<ResearchJob>{return json(await fetcher(`/api/research/jobs/${id}/cancel`,{method:"POST",headers:mutationHeaders(csrf),body:"{}"}));}
export async function deleteResearch(id:string,csrf:string,fetcher:Fetcher=fetch):Promise<void>{const response=await fetcher(`/api/research/jobs/${id}`,{method:"DELETE",headers:mutationHeaders(csrf),body:"{}"});if(!response.ok)throw new Error("删除研究失败");}
export async function retryResearch(id:string,csrf:string,fetcher:Fetcher=fetch):Promise<{job_id:string;status:string}>{return json(await fetcher(`/api/research/jobs/${id}/retry`,{method:"POST",headers:mutationHeaders(csrf),body:JSON.stringify({client_request_id:crypto.randomUUID()})}));}
export async function getResearchReport(id:string,fetcher:Fetcher=fetch):Promise<{job_id:string;title:string;markdown:string}>{return json(await fetcher(`/api/research/jobs/${id}/report`));}
export async function getSchedules(fetcher:Fetcher=fetch):Promise<{schedules:ResearchSchedule[]}>{return json(await fetcher("/api/research/schedules"));}
export async function createSchedule(payload:Record<string,unknown>,csrf:string,fetcher:Fetcher=fetch):Promise<ResearchSchedule>{return json(await fetcher("/api/research/schedules",{method:"POST",headers:mutationHeaders(csrf),body:JSON.stringify(payload)}));}
export async function updateSchedule(id:string,payload:Record<string,unknown>,csrf:string,fetcher:Fetcher=fetch):Promise<ResearchSchedule>{return json(await fetcher(`/api/research/schedules/${id}`,{method:"PUT",headers:mutationHeaders(csrf),body:JSON.stringify(payload)}));}
export async function deleteSchedule(id:string,csrf:string,fetcher:Fetcher=fetch):Promise<void>{const response=await fetcher(`/api/research/schedules/${id}`,{method:"DELETE",headers:mutationHeaders(csrf),body:"{}"});if(!response.ok)throw new Error("删除定时任务失败");}
export async function runSchedule(id:string,key:string,csrf:string,fetcher:Fetcher=fetch):Promise<{job_id:string;status:string}>{return json(await fetcher(`/api/research/schedules/${id}/run`,{method:"POST",headers:mutationHeaders(csrf),body:JSON.stringify({client_request_id:key})}));}
export async function getNotificationChannels(fetcher:Fetcher=fetch):Promise<{channels:NotificationChannel[]}>{return json(await fetcher("/api/notification/channels"));}
export async function createNotificationChannel(payload:Record<string,unknown>,csrf:string,fetcher:Fetcher=fetch):Promise<NotificationChannel>{return json(await fetcher("/api/notification/channels",{method:"POST",headers:mutationHeaders(csrf),body:JSON.stringify(payload)}));}
export async function deleteNotificationChannel(id:string,csrf:string,fetcher:Fetcher=fetch):Promise<void>{const response=await fetcher(`/api/notification/channels/${id}`,{method:"DELETE",headers:mutationHeaders(csrf),body:"{}"});if(!response.ok)throw new Error("删除通知渠道失败");}
export async function setHumanMode(enabled:boolean,csrf:string,fetcher:Fetcher=fetch):Promise<{human_mode:boolean}>{return json(await fetcher("/api/settings/human-mode",{method:"PUT",headers:mutationHeaders(csrf),body:JSON.stringify({enabled})}));}

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
export async function getMemoryOverview(fetcher:Fetcher=fetch):Promise<{entries:MemoryEntry[];proposals:MemoryProposal[];episodes:MemoryEpisode[]}>{return json(await fetcher("/api/memories"));}
export async function createMemoryEntry(payload:{kind:string;scope_type:string;scope_id:string;content:string;idempotency_key:string},csrf:string,fetcher:Fetcher=fetch):Promise<MemoryEntry>{return json(await fetcher("/api/memory/entries",{method:"POST",headers:mutationHeaders(csrf),body:JSON.stringify(payload)}));}
export async function updateMemoryEntry(id:string,content:string,base_revision_id:string,csrf:string,fetcher:Fetcher=fetch):Promise<MemoryEntry>{return json(await fetcher(`/api/memory/entries/${id}`,{method:"PATCH",headers:mutationHeaders(csrf),body:JSON.stringify({content,base_revision_id})}));}
export async function archiveMemoryEntry(id:string,csrf:string,fetcher:Fetcher=fetch):Promise<MemoryEntry>{return json(await fetcher(`/api/memory/entries/${id}/archive`,{method:"POST",headers:mutationHeaders(csrf),body:"{}"}));}
export async function purgeMemoryEntry(id:string,csrf:string,fetcher:Fetcher=fetch):Promise<void>{const response=await fetcher(`/api/memory/entries/${id}`,{method:"DELETE",headers:mutationHeaders(csrf),body:"{}"});if(!response.ok)throw new Error("删除失败");}
export async function decideMemoryProposal(id:string,accept:boolean,csrf:string,fetcher:Fetcher=fetch):Promise<MemoryProposal>{return json(await fetcher(`/api/memory/proposals/${id}/decision`,{method:"POST",headers:mutationHeaders(csrf),body:JSON.stringify({accept,idempotency_key:crypto.randomUUID()})}));}
export async function updateMemoryEpisode(id:string,summary:string,retrieval_policy:string,csrf:string,fetcher:Fetcher=fetch):Promise<MemoryEpisode>{return json(await fetcher(`/api/memory/episodes/${id}`,{method:"PATCH",headers:mutationHeaders(csrf),body:JSON.stringify({summary,retrieval_policy})}));}
export async function deleteMemoryEpisode(id:string,csrf:string,fetcher:Fetcher=fetch):Promise<void>{const response=await fetcher(`/api/memory/episodes/${id}`,{method:"DELETE",headers:mutationHeaders(csrf),body:"{}"});if(!response.ok)throw new Error("删除经历失败");}

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
