export type AgentState =
  | "RECEIVED"
  | "CLARIFYING"
  | "PLANNING"
  | "AWAITING_APPROVAL"
  | "EXECUTING"
  | "AWAITING_OUTCOME"
  | "REFLECTING"
  | "COMPLETED"
  | "BLOCKED"
  | "FAILED"
  | "CANCELLED";

export interface Bootstrap {
  csrf_token: string;
  version: string;
  api_key_env: string;
  api_key_configured: boolean;
  human_mode?: boolean;
}

export interface GoalWorkspace {
  resource_id: string;
  thread_id: string;
  plan_document_id: string;
  phase: "DISCOVERING" | "PLANNING" | "DELIVERED" | "READY_TO_START" | "EXECUTING" | "REVIEWING" | "ADJUSTING" | "COMPLETED" | "PAUSED" | "CANCELLED";
  next_action: { kind: string; label: string; href: string; resource_id: string; reason: string } | null;
  sources: Array<{ kind: string; id: string; label: string }>;
  plan: { id: string; title: string; version: number; file_status: string };
  program: { id: string; objective_title: string; status: string; start_date: string; end_date: string; version: number } | null;
  today: { program_id: string; date: string; action_count: number; estimated_minutes: number } | null;
  review: GoalDailyReview | null;
  research: Array<{ id: string; topic: string; status: string; phase: string }>;
  growth: { episode_id: string | null; episode_count: number; latest_summary: string | null };
}

export interface GrowthProfile {
  owner_id: string;
  metrics: { total_programs: number; completed_programs: number; completed_actions: number; skipped_actions: number; deferred_actions: number; average_difficulty: number | null; average_actual_minutes: number | null; accepted_adjustments: number };
  programs: Array<{ id: string; objective_title: string; objective_summary: string; status: string; start_date: string; end_date: string; version: number; progress: GoalProgress; completion_summary: string | null; completion_episode_id: string | null; source_plan_document_id: string }>;
}

export interface SkillDefinition {
  skill_id?: string;
  name: string;
  title: string;
  description: string;
  enabled: boolean;
  version_id?: string;
  package_digest?: string;
}

export interface ModelProfileVersion {
  id:string;profile_id:string;profile_name:string;version:number;
  provider_protocol:"openai_compatible"|"anthropic"|"gemini";provider_name:string;base_url:string;model_name:string;
  credential_env_ref:string;credential_configured:boolean;capabilities:Record<string,boolean>;
  context_window:number;max_output_tokens:number;timeout_seconds:number;max_attempts:number;
  config_digest:string;status:"ACTIVE"|"DISABLED";
  verified_at:string|null;verification_status:"UNVERIFIED"|"VERIFIED"|"FAILED";verification_error_kind:string|null;
  created_at:string;
  working_window_mode?: "auto"|"manual"|null;
  model_context_limit?: number|null;
  model_max_output_limit?: number|null;
  capacity_status?: "verified"|"unverified"|"manual"|"legacy"|null;
  capacity_source?: string|null;
  counter_mode?: "estimate"|"verified"|null;
  counter_id?: string|null;
  counter_version?: string|null;
  admitted_context_limit?: number|null;
  soft_context_limit?: number|null;
  context_window_verified?: boolean;
  validation_tier?: string|null;
  capacity?: ModelCapacityRecord|null;
}
export interface ModelCapacityRecord {
  mode?: "auto"|"manual";
  status?: "verified"|"unverified"|"manual"|"legacy";
  source?: string;
  effective_context_limit?: number|null;
  model_context_limit?: number|null;
  model_max_output_limit?: number|null;
  counter_id?: string|null;
  counter_version?: string|null;
  counter_mode?: "estimate"|"verified"|null;
  reason?: string;
  catalog_version?: string|null;
}
export interface ModelCapacityResolution {
  capacity: ModelCapacityRecord & {effective_context_limit:number|null;reason?:string};
  evidence: ModelCapacityRecord;
  entry: {
    model_id:string;model_version:string|null;context_limit:number|null;max_output_limit:number|null;
    context_verified:boolean;counter_verified:boolean;verified_at:string;
    evidence:{urls:string[];fetched_at:string;snapshot_sha256:string[];notes:string};
  }|null;
}
export interface ModelProfileRecord {id:string;name:string;status:string;created_at:string;updated_at:string;versions:ModelProfileVersion[];}
export interface RoutingPolicy {id:string;name:string;version:number;roles:Record<string,{primary:string;fallback:string[]}>;policy_digest:string;created_at:string;}
export interface CostSummary {limit_microusd:number;reserved_microusd:number;charged_microusd:number;}
export interface UsageGroup {role:string;provider:string;profile_version_id:string;attempts:number;succeeded:number;fallbacks:number;cost_microusd:number;unknown_cost_attempts:number;success_rate:number;fallback_rate:number;ttft_seconds:number|null;tps:number|null;p95_latency_seconds:number|null;}
export interface EvaluationRunRecord {id:string;suite_id:string;baseline_bundle_id:string;candidate_bundle_id:string;status:"QUEUED"|"RUNNING"|"BUDGET_BLOCKED"|"COMPLETED"|"FAILED"|"CANCELLED";budget_microusd:number;attempts:number;created_at:string;updated_at:string|null;finished_at:string|null;cancel_requested_at:string|null;}
export type EvaluationProgressEvent =
  | {seq:number;type:"evaluation.run.started"|"evaluation.run.finished";status:string;attempt?:number}
  | {seq:number;type:"evaluation.case.finished";case_id:string;partition:"DEV"|"HOLDOUT"|"SAFETY";domain:string;execution_order:string};
export interface EvaluationReport {release_eligible:boolean;report_digest:string;cost_microusd:number;holdout:{wins:number;ties:number;losses:number;non_ties:number;evidence_sufficient:boolean};safety:{passed:number;failures:number};deterministic:{passed:number;failures:number};statistics:{quality_difference:{estimate:number;ci95:number[]};latency:{baseline_median:number;candidate_median:number;baseline_p95:number;candidate_p95:number;paired_improvement:number;ci95:number[]};cost:{paired_improvement_microusd:number;ci95:number[]};primary_objective:{name:string;estimate:number;threshold:number;ci95:number[];passed:boolean}};execution_orders:{baseline_first:number;candidate_first:number};records:Array<{case_id:string;partition:string;domain:string;winner:string;candidate_safe:boolean}>;}
export interface SkillVersionRecord {skill_id:string;version_id:string;name:string;version:string;title:string;description:string;content:string;package_digest:string;manifest_digest:string;requested_tools:string[];granted_tools:string[];connectors:string[];phases:string[];grant_digest:string;status:string;}
export interface SkillInstallPreview {install_token:string;name:string;version:string;title:string;description:string;requested_tools:string[];connectors:string[];phases:string[];manifest_digest:string;package_digest:string;}
export interface TrustedConnectorRecord {connector_id:string;version_id:string;name:string;version:number;base_url:string;methods:string[];paths:string[];request_schema:Record<string,unknown>;credential_env_ref:string|null;timeout_seconds:number;max_response_bytes:number;risk:string;config_digest:string;status:string;verified_at?:string|null;}

export interface Run {
  id: string;
  goal_id: string;
  session_id: string;
  state: AgentState;
  resume_state: AgentState | null;
  current_plan_version_id: string | null;
  current_step_id: string | null;
  version: number;
  budget: Record<string, unknown>;
  pending_approvals: string[];
  approval_details?: Record<string, GoalActivationApproval>;
  skill_names?: string[];
  source_plan_document_id?: string | null;
  source_plan_document_version_id?: string | null;
  source_plan_content_hash?: string | null;
}

export interface MessageRecord {
  turn_id?: string;
  total_ms?: number | null;
  id: string;
  run_id: string;
  interaction_id: string | null;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  streaming?: boolean;
  generation?: number;
  status?: string;
  plan_document_version_id?: string | null;
  presentation?: "standard" | "human_bubbles";
  origin?: "history" | "live";
  research_job_id?: string | null;
}

export type TurnStatus =
  | "ACCEPTED"
  | "ROUTING"
  | "STREAMING"
  | "COMPLETED"
  | "AWAITING_INPUT"
  | "AWAITING_DIRECTION"
  | "AWAITING_TOOL_APPROVAL"
  | "MATERIALIZING"
  | "FAILED"
  | "CANCELLED";

export interface Turn {
  id: string;
  thread_id: string;
  client_turn_id: string;
  parent_turn_id: string | null;
  status: TurnStatus;
  policy: "answer" | "propose_execution" | "clarify" | null;
  content_shape: string | null;
  reason_code: string | null;
  artifact_kind?: string | null;
  artifact_operation?: string | null;
  artifact_title?: string | null;
  goal_action_id?: string | null;
  version: number;
  skill_names: string[];
  materialized_goal_id: string | null;
  materialized_run_id: string | null;
  direction_action: string | null;
  direction_idempotency_key: string | null;
  metrics?: TurnMetrics;
  created_at: string;
  updated_at: string;
}

export interface TurnMetrics {
  queue_wait_ms: number | null;
  context_ms: number | null;
  model_ttft_ms: number | null;
  stream_ms: number | null;
  answer_wait_ms: number | null;
  total_ms: number | null;
  model_attempt_count: number;
}

export interface AskOption {
  label: string;
  description: string;
}

export interface AskQuestion {
  id: string;
  header: string;
  question: string;
  options: AskOption[];
  multi_select: boolean;
  allow_free_text: boolean;
}

export interface PendingAsk {
  id: string;
  turn_id: string;
  questions: AskQuestion[];
  status: "PENDING";
  continuation_turn_id: string | null;
  created_at: string;
  answered_at: string | null;
}

export interface AskAnswer {
  question_id: string;
  selected_options: string[];
  free_text: string;
}

export interface Thread {
  id: string;
  title: string;
  version: number;
  active_turn_id: string | null;
  next_event_seq: number;
  created_at: string;
  updated_at: string;
  turns?: Turn[];
}

export interface ThreadMessage {
  total_ms?: number | null;
  id: string;
  thread_id: string;
  turn_id: string;
  role: "user" | "assistant";
  content: string;
  status: "ready" | "streaming" | "interrupted" | "cancelled" | "failed";
  generation: number;
  content_length: number;
  plan_document_version_id?: string | null;
  presentation?: "standard" | "human_bubbles";
  research_job_id?: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface ResearchSource { id:string;ordinal:number;kind:string;canonical_url:string|null;locator:string|null;title:string|null;published_at:string|null;retrieved_at:string|null;quality_score:number|null; }
export interface ResearchTraceability { requirement:string;conclusion:string;evidence_ids:string[];source_ids:string[];citation_source_ids:string[];source_versions:{source_id:string;content_hash:string;retrieved_at:string}[];evidence_locations:{evidence_id:string;source_id:string;char_start:number;char_end:number;exact_quote_hash:string}[];supported:boolean; }
export interface ResearchJob { id:string;thread_id:string;source_turn_id:string;schedule_id:string|null;retry_of_job_id:string|null;trigger_kind:string;topic:string;source_scopes:string[];status:"QUEUED"|"RUNNING"|"COMPLETED"|"PARTIAL"|"FAILED"|"CANCELLED";phase:string;attempts:number;cancel_requested_at:string|null;created_at:string;updated_at:string;title:string|null;source_count:number;evidence_count:number;assistant_message_id:string|null;failure_reason_code?:string|null;failure_details?:{missing_requirements?:string[]}|null;traceability?:ResearchTraceability[];missing_requirements?:string[]; }
export interface ResearchSchedule { id:string;name:string;thread_id:string;topic:string;source_scopes:string[];trigger_type:"daily"|"weekly"|"interval_hours";trigger_time:string|null;trigger_weekday:number|null;interval_hours:number|null;timezone:string;enabled:boolean;notify_enabled:boolean;next_run_at:string|null;last_run_at:string|null;last_job_id:string|null;created_at:string;updated_at:string; }
export interface NotificationChannel {id:string;name:string;channel_type:"serverchan"|"wecom"|"dingtalk"|"webhook";secret_env_name:string;enabled:boolean;configured:boolean;}

export interface ThreadEvent {
  schema_version: number;
  event_id: string;
  seq: number;
  thread_id: string;
  turn_id: string;
  type: string;
  occurred_at: string;
  actor: string;
  data: Record<string, unknown>;
}

export interface GoalResponse {
  id: string;
  run_id: string;
  state: AgentState;
}

export interface PlanStep {
  id: string;
  title: string;
  description: string;
  status: "pending" | "completed" | "cancelled";
  position: number;
}

export interface PlanVersion {
  id: string;
  run_id: string;
  goal_id: string;
  version: number;
  status: string;
  summary: string;
  steps: PlanStep[];
  source_document_version_id?: string | null;
}

export interface PlanResponse {
  current: PlanVersion | null;
  history: PlanVersion[];
}

export interface PlanDocumentVersion {
  id: string;
  plan_document_id: string;
  version: number;
  base_version_id: string | null;
  title: string;
  markdown?: string;
  content_hash: string;
  source_turn_id: string | null;
  source_message_id: string | null;
  actor: "model" | "user" | "filesystem" | "restore" | string;
  change_summary: string;
  status: "prepared" | "committed" | "abandoned" | string;
  created_at: string;
  committed_at: string | null;
}

export interface PlanDocument {
  id: string;
  thread_id: string;
  title: string;
  current_version_id: string | null;
  projected_version_id: string | null;
  file_status: "pending" | "ready" | "conflict" | "failed" | "deleted" | string;
  file_path: string;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  current: PlanDocumentVersion | null;
  versions: PlanDocumentVersion[];
}

export interface PlanDocumentSummary {
  id: string;
  thread_id: string;
  title: string;
  version: number | null;
  file_status: "pending" | "ready" | "conflict" | "failed" | "deleted" | string;
  created_at: string;
  updated_at: string;
}

export type GoalProgramStatus = "DRAFT" | "ACTIVE" | "PAUSED" | "COMPLETED" | "CANCELLED";
export type GoalActionStatus = "SCHEDULED" | "COMPLETED" | "SKIPPED" | "DEFERRED" | "CANCELLED";

export interface GoalAction {
  id: string; program_id: string; program_version_id: string; logical_key: string;
  scheduled_date: string; position: number; title: string; description: string;
  estimated_minutes: number; completion_criteria: string; required: boolean;
  status: GoalActionStatus; version: number; completed_at: string | null; skipped_at: string | null;
  deferred_at: string | null; cancelled_at: string | null; deferred_from_action_id: string | null; cancel_reason: string | null;
  progress?: {state?: "PARTIAL" | "CARRIED_OVER"; source_action_id?: string; actual_minutes?: number; difficulty?: number; note?: string; completed_work?: string; remaining_work?: string; output?: string};
  time_entry?: {actual_date:string;actual_minutes:number|null};
}

export interface GoalProgress {
  required_completed: number; required_total: number; completion_rate: number;
  completion_ready: boolean; optional_completed: number;
}

export interface ProgramStructure {
  objective_title: string; objective_summary: string; start_date: string; end_date: string;
  assumptions: string[];
  milestones: Array<{ logical_key: string; title: string; target_date: string }>;
  actions: Array<Omit<GoalAction, "id" | "program_id" | "program_version_id" | "status" | "version" | "completed_at" | "skipped_at" | "deferred_at" | "cancelled_at" | "deferred_from_action_id" | "cancel_reason">>;
}

export interface GoalProgram {
  id: string; objective_title: string; objective_summary: string; status: GoalProgramStatus;
  compile_status: "COMPILING" | "READY" | "FAILED"; compile_error_code: string | null;
  timezone: string; start_date: string; end_date: string; daily_minutes: number; version: number;
  source_thread_id: string; source_plan_document_id: string; source_plan_document_version_id: string;
  source_plan_content_hash: string; current_program_version_id: string | null;
  structure: ProgramStructure | null; actions: GoalAction[]; progress: GoalProgress; next_event_seq: number; deleted_at: string | null;
  completion_summary: string | null; completion_episode_id: string | null;
  schedule_constraints?: {available_weekdays: number[]; excluded_dates: string[]};
  calendar?: {natural_days: number; study_days: number; available_dates: string[]; rest_dates: string[]};
}

export interface TodayProgramGroup {
  program: Pick<GoalProgram, "id" | "objective_title" | "objective_summary" | "status" | "timezone" | "start_date" | "end_date" | "version">;
  local_date: string; day_number: number; today: GoalAction[]; overdue: GoalAction[];
  today_estimated_minutes: number; progress: GoalProgress; review: GoalDailyReview | null;
  completed?: GoalAction[];
  day_time?: {spent_minutes:number;remaining_minutes:number|null;time_complete:boolean;unknown_action_ids:string[];has_execution_record:boolean};
  needs_review?: boolean;
  pending_review_dates?: string[];
  has_execution_record?: boolean;
}

export interface TodayResponse { date: string | null; programs: TodayProgramGroup[]; }

export interface GoalAdjustmentProposal {
  id:string; program_id:string; base_program_version_id:string; expected_plan_document_version_id:string;
  expected_plan_content_hash:string; candidate:ProgramStructure;
  diff:{added:ProgramStructure["actions"];removed:ProgramStructure["actions"];changed:Array<{logical_key:string;fields:Record<string,{before:unknown;after:unknown}>}>};
  reason:string; status:"PENDING"|"ACCEPTED"|"REJECTED"|"STALE"; version:number;
  accepted_program_version_id:string|null; plan_sync_status:string|null; plan_sync_version_id:string|null;
  created_at:string; decided_at:string|null;
}

export interface GoalDailyReview {
  id:string;program_id:string;local_date:string;status:"QUEUED"|"RUNNING"|"COMPLETED"|"FAILED";
  signals:string[];summary:string|null;encouragement:string|null;needs_adjustment:boolean|null;
  adjustment_reason:string|null;proposal:GoalAdjustmentProposal|null;error_code:string|null;
  evidence_stale?:boolean;revision?:number;history?:unknown[];
  adjustment_status?:"NOT_NEEDED"|"PENDING"|"RUNNING"|"COMPLETED"|"NO_CHANGE"|"FAILED";
  adjustment_error_code?:string|null;adjustment_attempts?:number;
}

export interface ThreadPlanResponse {
  plan: PlanDocument | null;
}

export interface EventRecord {
  schema_version: number;
  event_id: string;
  seq: number;
  run_id: string;
  goal_id: string;
  type: string;
  occurred_at: string;
  actor: string;
  correlation: Record<string, unknown>;
  data: Record<string, unknown>;
}

export interface Stats {
  run_id: string;
  state?: AgentState;
  interactions?: number | null;
  plan_completed?: number | null;
  plan_total?: number | null;
  model_attempts?: number | null;
  model_seconds?: number | null;
  tool_calls?: number | null;
  tool_seconds?: number | null;
  ttft_seconds?: number | null;
  tps?: number | null;
  cache_hit_rate?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  metrics?: Record<string, unknown>;
}

export interface MemoryRecord {
  id: string;
  run_id: string | null;
  kind: "preference" | "habit";
  content: string;
  scope: "global" | "project" | "skill";
  project_id: string | null;
  skill_name: string | null;
  confidence: number;
  status: "proposed" | "confirmed" | "rejected" | "disabled";
  version: number | null;
  evidence_event_ids: string[];
  path: string;
}

export type AgentRunStatus = "QUEUED" | "RUNNING" | "WAITING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
export type AgentTaskStatus = "QUEUED" | "RUNNING" | "WAITING_CHILDREN" | "SUCCEEDED" | "FAILED" | "CANCELLED";

export interface AgentRun {
  id: string;
  thread_id: string | null;
  objective: string;
  mode: "single" | "expert";
  status: AgentRunStatus;
  runtime_bundle_id: string;
  budget_units: number;
  reserved_budget_units: number;
  version: number;
  cancel_requested_at: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface AgentTask {
  id: string;
  agent_run_id: string;
  root_task_id: string;
  parent_task_id: string | null;
  child_key: string | null;
  role: string;
  objective: string;
  output_schema: string;
  status: AgentTaskStatus;
  priority: number;
  join_policy: "ALL_SUCCESS" | "ALL_DONE" | null;
  attempts: number;
  max_attempts: number;
  lease_epoch: number;
  budget_units: number;
  result_artifact_id: string | null;
  error_code: string | null;
  cancel_requested_at: string | null;
  cancel_reason: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface AgentArtifact {
  id: string;
  task_id: string;
  artifact_type: string;
  content: Record<string, unknown>;
  source_refs: unknown[];
  created_at: string;
}

export interface AgentEvent {
  event_id: string;
  agent_run_id: string;
  seq: number;
  task_id: string | null;
  type: string;
  actor: string;
  data: Record<string, unknown>;
  occurred_at: string;
}

export type EvolutionCandidateStatus =
  | "DRAFT" | "READY_FOR_EVAL" | "EVALUATING" | "EVALUATED" | "PENDING_APPROVAL"
  | "APPROVED" | "CANARY" | "PROMOTED" | "FAILED" | "REJECTED" | "ROLLED_BACK";

export interface EvolutionCandidate {
  id: string;
  kind: "memory" | "skill" | "policy" | "prompt" | "code";
  title: string;
  summary: string;
  status: EvolutionCandidateStatus;
  version: number;
  approval_eligible?: boolean;
  approval_block_reason?: string | null;
  approval_block_code?: string;
  risk_level: "low" | "medium" | "high" | string;
  evidence_count: number;
  evidence_refs?: string[];
  evidence_source_kinds?: string[];
  record_origin?: "demo" | "manual" | "observed";
  reason?: string;
  proposed_content?: Record<string, unknown>;
  diff?: Array<{ label: string; before?: string; after?: string }>;
  evaluation: null | {
    id?: string;
    report_digest?: string;
    status: string;
    deterministic_pass: boolean | null;
    score_delta?: number | null;
    regressions: string[];
    passed?: number | null;
    total?: number | null;
    baseline_correct?: number | null;
    candidate_correct?: number | null;
    quality_delta?: number | null;
    safety_violations?: number | null;
  };
  permission_diff: { added: string[]; removed: string[]; unchanged?: string[] };
  canary: null | { allocation?: number; sample_size?: number; challenger_sample_size?: number; champion_sample_size?: number; required_samples?: number; safety_failures?: number; success_failures?: number; promotable?: boolean; status?: string };
  rollback?: null | { kind: "manual" | "safety_auto"; actor: string; reason: string; occurred_at: string };
  approval_id?: string | null;
  created_at: string;
  updated_at: string;
}
export interface MemoryEvidence {
  source_type: string;
  source_id: string;
  source_label: string;
  excerpt: string;
}
export interface MemoryEntry {id:string;kind:"preference"|"constraint"|"fact"|"decision"|"lesson";scope_type:"user"|"project";scope_id:string;status:"ACTIVE"|"ARCHIVED"|"PURGED";content:string;revision_id:string;revision_no:number;pinned:boolean;importance:number;sensitivity:string;evidence_state?:string;evidence_label?:string;evidence_count?:number;evidence?:MemoryEvidence[];created_at:string;updated_at:string;}
export interface MemoryProposal {
  id:string;
  version?:number;
  operation:string;
  target_entry_id:string|null;
  base_revision_id:string|null;
  kind:string;
  scope_type:string;
  scope_id:string;
  content:string;
  original_content?:string;
  accepted_content?:string|null;
  model_confidence?:number;
  confidence?:number;
  evidence_state?:"VERIFIED"|"LEGACY_UNVERIFIED"|"INVALID"|string;
  independent_user_turn_count?:number;
  evidence_label?:string;
  evidence?:MemoryEvidence[];
  status:string;
  accepted_revision_id:string|null;
  reason:string;
  created_at:string;
}
export interface MemoryEpisode {id:string;thread_id:string;project_id:string|null;start_message_seq:number;end_message_seq:number;summary:string;sensitivity:string;retrieval_policy:string;status:string;version:number;created_at:string;}

export interface GoalActivationApproval {
  snapshot_hash: string;
  program_version?: number;
  source_version_id?: string;
  source_content_hash?: string;
  preview?: {
    objective_title?: string;
    start_date?: string;
    end_date?: string;
    timezone?: string;
    daily_minutes?: number;
    structure?: { actions?: { title: string; scheduled_date: string; estimated_minutes: number }[] };
  };
}

export type GoalToolName =
  | "create_plan_draft"
  | "activate_goal_plan"
  | "query_goals"
  | "get_today_tasks"
  | "get_action_context"
  | "get_plan"
  | "record_action_feedback"
  | "defer_action";

export interface ChatToolCall {
  id: string;
  turn_id: string;
  tool_name: GoalToolName | string;
  params: Record<string, unknown>;
  risk: "READ" | "WRITE";
  status: "PENDING_APPROVAL" | "APPROVED" | "REJECTED" | "EXECUTED" | "FAILED" | "CANCELLED";
  approval_id: string | null;
  binding: { goal_activation?: GoalActivationApproval } & Record<string, unknown>;
  error_code: string | null;
  continuation_turn_id: string | null;
  created_at: string;
  acted_at: string | null;
}
