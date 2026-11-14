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
}

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
  finished: boolean;
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
}
