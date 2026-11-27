export interface IdentityProfile {
  userId: string;
  roles: string[];
}

export const DEFAULT_IDENTITY: IdentityProfile = {
  userId: "u_alice",
  roles: ["employee", "exec"],
};

export interface ModelConfig {
  mode: "deepseek";
  model: string;
  base_url: string;
  has_api_key: boolean;
}

export interface ThreadSummary {
  thread_id: string;
  title: string;
  created_at?: string;
  message_count?: number;
  covered_up_to_msg_id?: number;
  summary_text?: string;
}

export interface ApprovalTicket {
  review_id: string;
  thread_id: string;
  actor_id: string;
  run_id?: string;
  op_key: string;
  tool_name: string;
  args_hash: string;
  policy_rev: number;
  summary: Record<string, unknown>;
  expires_at: number;
  decision: "pending" | "approved" | "rejected";
  approver_id?: string | null;
  consumed_at?: number | null;
}

export interface ActivityBlock {
  id: string;
  kind: "subagent" | "tool";
  name: string;
  phase: "started" | "done" | "error";
  detail?: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  truncated?: boolean;
  refId?: string;
  ossKey?: string;
  preview?: string;
}

export interface ChatTurnItem {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  activities?: ActivityBlock[];
  approval?: ApprovalTicket;
  approvalStatus?: "pending" | "approved" | "rejected" | "submitting";
  traceId?: string;
  msgId?: number;
  feedbackRating?: "up" | "down";
  feedbackReason?: string;
  ts: number;
}

export interface SpanRecord {
  trace_id: string;
  span_id: string;
  parent_span_id?: string | null;
  service_name: string;
  operation_name: string;
  start_ms: number;
  duration_ms: number;
  status: string;
  attributes: Record<string, unknown>;
}

export interface QuickPromptSpec {
  title: string;
  hint: string;
  prompt: string;
}

export interface AppMeta {
  app_name: string;
  subtitle: string;
  plugin_name: string;
  welcome_title: string;
  welcome_description: string;
  quick_prompts: QuickPromptSpec[];
  tools?: Array<{
    name: string;
    description: string;
    risk_level: string;
    is_subagent: boolean;
  }>;
}

export interface WorkspaceFileSummary {
  path?: string;
  logical_path: string;
  version: number;
  size_bytes: number;
  content_type?: string;
  extracted_chars?: number;
  updated_by?: string;
}

export interface ContextBudgetSnapshot {
  max_window_tokens: number;
  max_input_tokens: number;
  fixed_tokens: number;
  history_budget: number;
  compact_trigger_tokens: number;
  compact_trigger_ratio: number;
  summary_tokens: number;
  active_tokens: number;
  history_used_tokens: number;
  total_prompt_tokens_est: number;
}

export interface RuntimeStateSnapshot {
  thread_id?: string;
  trace_id: string | null;
  recent_traces: string[];
  spans: SpanRecord[];
  summary: {
    covered_up_to_msg_id: number;
    text: string;
  };
  context_budget?: ContextBudgetSnapshot;
  approvals: ApprovalTicket[];
  tool_runs: Array<Record<string, unknown>>;
  tool_outputs: Array<{
    ref_id: string;
    thread_id?: string;
    resource_owner?: string;
    tool_name: string;
    total_chars: number;
    oss_key?: string | null;
    preview: string;
    expires_at?: string;
  }>;
  workspace_files?: WorkspaceFileSummary[];
  loki_logs: Array<Record<string, unknown>>;
}
