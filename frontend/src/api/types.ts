// Shapes the API returns. They mirror the backend's Pydantic schemas (backend/src/support_agent/
// schemas/). The UI only displays these values: it never computes eligibility, amounts or
// decisions. Money is always integer paise ("*_minor").

export type Role = "customer" | "reviewer" | "admin";

export interface ApiErrorBody {
  success: false;
  error_code: string;
  message: string;
  details?: unknown;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number; // seconds
}

export interface Me {
  id: string;
  role: Role;
  name: string;
  email: string;
}

// ---------------------------------------------------------------- orders

export interface OrderItem {
  product_id: string;
  product_name: string;
  quantity: number;
  paid_amount_minor: number;
}

export interface OrderReturn {
  status: string;
  requested_at: string;
  received_at: string | null;
}

export interface Order {
  order_number: string;
  status: string;
  currency: string;
  paid_amount_minor: number;
  placed_at: string;
  delivered_at: string | null;
  items: OrderItem[];
  returns: OrderReturn[];
}

// ---------------------------------------------------------------- tickets and runs

export type RunStatus =
  | "created"
  | "investigating"
  | "awaiting_customer"
  | "awaiting_approval"
  | "executing"
  | "completed"
  | "escalated"
  | "failed";

export type TicketStatus = "open" | "in_progress" | "awaiting_customer" | "resolved" | "escalated";

export interface Ticket {
  id: string;
  order_number: string | null;
  category: string | null;
  status: TicketStatus;
  summary: string; // the customer's own words
  created_at: string;
  latest_run_id: string | null;
  latest_run_status: RunStatus | null;
  reply: string | null; // the verified reply (checked by the backend's output guardrail)
  reply_from: ReplyAuthor; // "specialist" once a reviewer has answered an escalated ticket
}

export type ReplyAuthor = "assistant" | "specialist";

export interface TicketList {
  items: Ticket[];
  limit: number;
  offset: number;
}

export interface Run {
  run_id: string;
  ticket_id: string;
  status: RunStatus;
  category: string | null;
  started_at: string;
  completed_at: string | null;
}

// The live status (GET /runs/{id}/events and the stream's "status" event).
export interface RunStatusEvent {
  run_id: string;
  ticket_id: string;
  status: RunStatus;
  ticket_status: TicketStatus;
  reply: string | null; // the verified reply: checked by the backend's output guardrail
  reply_from: ReplyAuthor;
  finished: boolean;
  // Kinds of records the run checked, as fixed codes: "order", "return", "payment",
  // "shipment", "policy". Never tool names, results or model text (ADR-021).
  checked: string[];
}

export type DetailValue = string | number | boolean | (string | number | boolean)[];

export interface RunStepEvent {
  id: string;
  type: string; // e.g. "approval.requested"
  actor: string;
  at: string;
  details: Record<string, DetailValue>; // reviewers only; {} for customers
}

export interface RunSnapshot {
  run: RunStatusEvent;
  steps: RunStepEvent[];
}

// ---------------------------------------------------------------- memory

export interface MemoryItem {
  kind: string;
  outcome_code: string;
  category: string | null;
  order_number: string | null;
  created_at: string;
  expires_at: string;
}

export interface MemoryDeleted {
  deleted: number;
}

// ---------------------------------------------------------------- approvals (reviewers)

export interface PendingApproval {
  approval_id: string;
  proposal_id: string;
  ticket_id: string;
  order_number: string;
  customer_name: string;
  amount_minor: number;
  currency: string;
  reason: string;
  reasons: string[];
  created_at: string;
  expires_at: string;
  run_id: string;
  ai_action: string | null; // the AI's recommended action code, from the audit trail
  risk_flags: string[]; // risk flag codes computed by code (ADR-021)
  readiness: ReadinessView; // computed by code (ADR-022)
  proposed_by_you: boolean; // you proposed it by hand: another reviewer must decide
}

// ---------------------------------------------------------------- readiness (ADR-022)

export type ReadinessStatus = "ready_to_approve" | "needs_attention";

export interface ReadinessReason {
  code: string; // e.g. "RISK_FLAG", "NOT_ELIGIBLE_NOW" (domain/readiness.py)
  codes: string[]; // detail codes, e.g. the risk flag or eligibility reason codes
}

export interface ReadinessView {
  status: ReadinessStatus;
  reasons: ReadinessReason[]; // most serious first
}

export interface ApprovalList {
  items: PendingApproval[];
}

export type RefundStatus =
  | "reserved"
  | "submitted"
  | "processing"
  | "succeeded"
  | "failed"
  | "pending_reconciliation";

export interface RefundSummary {
  refund_id: string;
  status: RefundStatus;
  amount_minor: number;
  provider_reference: string | null;
}

export interface DecisionResponse {
  approval_id: string;
  proposal_id: string;
  decision: "approved" | "rejected";
  proposal_status: string;
  refund: RefundSummary | null;
  message: string; // states only what has been verified
}

export interface CaseProposal {
  order_number: string;
  amount_minor: number;
  currency: string;
  reason: string;
  checks_passed: string[];
  payload_hash: string;
  status: string;
}

export interface CaseFacts {
  order_status: string;
  delivered_at: string | null;
  return_status: string | null;
  return_requested_at: string | null;
  return_received_at: string | null;
  inspection_status: string | null;
  paid_amount_minor: number;
  succeeded_refunds_minor: number;
  in_progress_refunds_minor: number;
  refundable_balance_minor: number;
  refund_window_days: number | null;
  eligible_now: boolean;
  reasons_now: string[];
}

export interface PolicyCitation {
  citation: string;
  policy_title: string;
  version: string;
  section_title: string;
  text: string;
}

export interface GoverningPolicy {
  policy_key: string;
  version: string;
  title: string;
  refund_window_days: number | null;
}

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

export interface ApprovalCase {
  approval_id: string;
  proposal_id: string;
  run_id: string;
  ticket_id: string;
  approval_status: ApprovalStatus;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  customer_name: string;
  complaint: string; // the customer's own words: shown as plain text
  ticket_status: TicketStatus;
  proposal: CaseProposal;
  facts: CaseFacts | null;
  governing_policy: GoverningPolicy | null;
  citations: PolicyCitation[];
  refund: RefundSummary | null;
  readiness: ReadinessView;
  ai_action: string | null; // the AI's recommended action code (audit trail)
  risk_flags: string[]; // risk flag codes computed by code
  proposed_by_reviewer: boolean; // a manual proposal: no AI checks ran on it
  proposed_by_you: boolean; // separation of duties: you may not decide it
}

// ---------------------------------------------------------------- escalations (ADR-022)

export interface EscalationReason {
  code: string; // e.g. "AI_CHOSE_TO_ESCALATE" (domain/escalations.py)
  codes: string[];
}

export interface EscalationItem {
  ticket_id: string;
  run_id: string;
  customer_name: string;
  order_number: string | null;
  category: string | null;
  complaint: string; // the customer's own words: shown as plain text
  escalated_at: string;
  reasons: EscalationReason[];
  specialist_replied: boolean;
}

export interface EscalationList {
  items: EscalationItem[];
}

export interface EscalationCase extends EscalationItem {
  ticket_status: TicketStatus;
  reply: string | null;
  reply_from: ReplyAuthor;
  facts: CaseFacts | null;
  currency: string | null;
  governing_policy: GoverningPolicy | null;
}

export interface EscalationActionResponse {
  ticket_id: string;
  ticket_status: TicketStatus;
  reply_from: ReplyAuthor;
}

export interface ProposalResponse {
  proposal_id: string;
  approval_id: string;
  run_id: string;
  ticket_id: string;
  order_number: string;
  amount_minor: number;
  currency: string;
  payload_hash: string;
  status: string;
  expires_at: string;
}

// ---------------------------------------------------------------- AI analysis (ADR-021)
// Reviewers only. Codes, counts and times computed by code from the audit trail, plus one
// piece of model text: `recommendation.rationale`, AI-generated and unverified, always shown
// as plain text and never as evidence.

export type AnalysisDetail = string | number | boolean | string[];

export interface AiRecommendation {
  action: string | null;
  accepted: boolean;
  reason_codes: string[];
  rationale: string | null;
  source: "ai_generated";
  verified: false;
}

export interface AgentActivity {
  agent: string;
  at: string;
  duration_ms: number | null;
  details: Record<string, AnalysisDetail>;
}

export interface ToolUse {
  call_id: string;
  tool: string;
  agent: string;
  outcome: string;
  latency_ms: number | null;
  by_code: boolean;
}

export interface QualityChecks {
  attempts: number;
  revisions: number;
  validator_passed: boolean | null;
  validation_codes: string[];
  critic_verdict: string | null;
  facts_grounded: number;
  facts_dropped: number;
  citations_verified: number;
  citations_dropped: number;
  reply_replaced: boolean;
}

export interface RiskFlag {
  code: string;
  codes: string[];
  count: number;
}

export interface RunCost {
  llm_calls: number;
  input_tokens: number;
  output_tokens: number;
  active_ms: number | null;
  fallback_calls: number;
}

export interface RunAnalysis {
  run_id: string;
  mode: string;
  status: RunStatus;
  recommendation: AiRecommendation;
  agents: AgentActivity[];
  tools: ToolUse[];
  quality: QualityChecks;
  risk_flags: RiskFlag[];
  cost: RunCost;
}

export interface AutomationSummary {
  days: number;
  since: string;
  runs: number;
  resolved_automatically: number;
  sent_for_approval: number;
  escalated: number;
  median_seconds_to_outcome: number | null;
}
