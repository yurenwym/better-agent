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
  status: "ready" | "streaming" | "interrupted" | "cancelled";
  generation: number;
  content_length: number;
  plan_document_version_id?: string | null;
  created_at: string;
  completed_at: string | null;
}

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
  current: PlanDocumentVersion | null;
  versions: PlanDocumentVersion[];
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
