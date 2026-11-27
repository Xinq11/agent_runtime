export interface IdentityProfile {
  tenantId: string;
  userId: string;
  roles: string[];
}

export const DEFAULT_IDENTITY: IdentityProfile = {
  tenantId: "t_acme",
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
  tenant_id: string;
  thread_id: string;
  actor_id: string;
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

export interface RuntimeStateSnapshot {
  trace_id: string | null;
  recent_traces: string[];
  spans: SpanRecord[];
  summary: {
    covered_up_to_msg_id: number;
    text: string;
  };
  approvals: ApprovalTicket[];
  tool_runs: Array<Record<string, unknown>>;
  tool_outputs: Array<{
    ref_id: string;
    tenant_id: string;
    thread_id?: string;
    resource_owner?: string;
    tool_name: string;
    total_chars: number;
    oss_key?: string | null;
    preview: string;
    expires_at?: string;
  }>;
  workspace_files?: WorkspaceFileSummary[];
  knowledge_chunks: Array<{
    chunk_id: string;
    parent_id?: string;
    tenant_id: string;
    doc_title: string;
    section_path: string;
    acl_roles: string[];
    doc_url: string;
    oss_key?: string | null;
    snippet?: string;
    chunk_count?: number;
    status?: "active" | "disabled";
  }>;
  loki_logs: Array<Record<string, unknown>>;
}

export interface SkillSummary {
  tenant_id?: string;
  slug: string;
  version_id?: string;
  version_num: number;
  description: string;
  tags: string[];
  acl_roles: string[];
  resources: string[];
  revision?: number;
  status?: string;
}

export interface OpsOverview {
  health: {
    ok: boolean;
    postgres: string;
    minio: string;
    mode: "deepseek";
    model: string;
    base_url: string;
    has_api_key: boolean;
  };
  counts: {
    threads: number;
    tool_approvals: number;
    tool_runs: number;
    tool_outputs: number;
    knowledge_chunks: number;
    skills?: number;
    traces: number;
  };
  threads: ThreadSummary[];
  skills?: SkillSummary[];
  tool_approvals: ApprovalTicket[];
  tool_runs: Array<{
    op_key: string;
    actor_id: string;
    tool_name: string;
    status: string;
    preview?: string;
    result_ref?: string | null;
    error_code?: string | null;
    lease_until: string;
  }>;
  tool_outputs: Array<{
    ref_id: string;
    thread_id?: string;
    resource_owner: string;
    tool_name: string;
    total_chars: number;
    oss_key?: string | null;
    preview: string;
    expires_at: string;
  }>;
  knowledge_chunks: Array<{
    chunk_id: string;
    parent_id?: string;
    tenant_id: string;
    doc_title: string;
    section_path: string;
    acl_roles: string[];
    doc_url: string;
    oss_key?: string | null;
    snippet?: string;
    chunk_count?: number;
    status?: "active" | "disabled";
  }>;
  recent_traces: Array<{
    trace_id: string;
    thread_id: string;
    created_at: string;
    span_count: number;
    total_ms: number;
  }>;
  metrics_text: string;
  loki_logs: Array<Record<string, unknown>>;
}
