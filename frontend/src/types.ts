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
  phase: "DISCOVERING" | "PLANNING" | "READY_TO_START" | "EXECUTING" | "REVIEWING" | "ADJUSTING" | "COMPLETED" | "PAUSED" | "CANCELLED";
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
  name: string;
  title: string;
  description: string;
  enabled: boolean;
}

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
  skill_names?: string[];
  source_plan_document_id?: string | null;
  source_plan_document_version_id?: string | null;
  source_plan_content_hash?: string | null;
}

export interface MessageRecord {
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
  created_at: string;
  updated_at: string;
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

export interface ResearchJob { id:string;thread_id:string;source_turn_id:string;schedule_id:string|null;retry_of_job_id:string|null;trigger_kind:string;topic:string;source_scopes:string[];status:"QUEUED"|"RUNNING"|"COMPLETED"|"FAILED"|"CANCELLED";phase:string;attempts:number;cancel_requested_at:string|null;created_at:string;updated_at:string;title:string|null;source_count:number;evidence_count:number;assistant_message_id:string|null;failure_reason_code?:string|null;failure_details?:{missing_requirements?:string[]}|null; }
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
}

export interface TodayProgramGroup {
  program: Pick<GoalProgram, "id" | "objective_title" | "objective_summary" | "status" | "timezone" | "start_date" | "end_date" | "version">;
  local_date: string; day_number: number; today: GoalAction[]; overdue: GoalAction[];
  today_estimated_minutes: number; progress: GoalProgress; review: GoalDailyReview | null;
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
  risk_level: "low" | "medium" | "high" | string;
  evidence_count: number;
  evidence_refs?: string[];
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
  };
  permission_diff: { added: string[]; removed: string[]; unchanged?: string[] };
  canary: null | { allocation?: number; sample_size?: number; challenger_sample_size?: number; champion_sample_size?: number; required_samples?: number; safety_failures?: number; success_failures?: number; promotable?: boolean; status?: string };
  approval_id?: string | null;
  created_at: string;
  updated_at: string;
}
export interface MemoryEntry {id:string;kind:"preference"|"constraint"|"fact"|"decision"|"lesson";scope_type:"user"|"project";scope_id:string;status:"ACTIVE"|"ARCHIVED"|"PURGED";content:string;revision_id:string;revision_no:number;pinned:boolean;importance:number;sensitivity:string;created_at:string;updated_at:string;}
export interface MemoryProposal {id:string;operation:string;target_entry_id:string|null;base_revision_id:string|null;kind:string;scope_type:string;scope_id:string;content:string;confidence:number;status:string;accepted_revision_id:string|null;reason:string;created_at:string;}
export interface MemoryEpisode {id:string;thread_id:string;project_id:string|null;start_message_seq:number;end_message_seq:number;summary:string;sensitivity:string;retrieval_policy:string;status:string;created_at:string;}
