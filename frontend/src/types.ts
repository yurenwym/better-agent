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
}

export interface MessageRecord {
  id: string;
  run_id: string;
  interaction_id: string | null;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  streaming?: boolean;
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
}

export interface PlanResponse {
  current: PlanVersion | null;
  history: PlanVersion[];
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
