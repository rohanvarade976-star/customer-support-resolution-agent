// Fixed wording for codes the API returns. A code with no entry is shown as itself, so a new
// backend code is never hidden or guessed at.

// The customer-facing sentence for each live event (PRD "Live events"). The assistant's work
// is named as such ("Our assistant is checking..."), so customers can see an AI is helping;
// what it decides is still checked by our rules before anything is sent (ADR-021).
const EVENT_LABELS: Record<string, string> = {
  "run.created": "We've received your request",
  "run.classified": "Our assistant identified what you need help with",
  "investigation.started": "Our assistant is checking your order details",
  "policy.completed": "Our assistant checked the applicable policy",
  "clarification.requested": "We need one more detail from you",
  "proposal.created": "Preparing a resolution",
  "resolution.completed": "Our assistant prepared a resolution",
  "validation.passed": "Resolution checked against our rules",
  "validation.failed": "Resolution checked against our rules",
  "approval.requested": "Your refund request is awaiting approval",
  "approval.approved": "Your refund was approved",
  "approval.rejected": "Your refund request was declined",
  "approval.expired": "Your request needs another review",
  "refund.submitted": "The payment service accepted your refund",
  "refund.processing": "Your refund is being processed",
  "refund.succeeded": "Your refund is complete",
  "refund.failed": "Your refund could not be completed",
  "refund.reconciling": "We couldn't confirm the refund outcome yet; we're checking",
  "ticket.updated": "Your ticket has been updated",
  "ticket.specialist_replied": "A support specialist replied",
  "ticket.resolved": "A support specialist closed your request",
  "run.completed": "Resolved",
  "run.escalated": "Passed to a support specialist",
  "run.failed": "Something went wrong; a person will follow up",
};

export function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? type;
}

// The reviewer's timeline: what happened, in plain words, for every audit event type. The raw
// code and payload stay one click away under "Technical details" (ADR-022).
const REVIEWER_EVENT_LABELS: Record<string, string> = {
  "run.created": "Run started",
  "run.classified": "Classifier labelled the request",
  "supervisor.decided": "Supervisor chose the specialists",
  "investigation.started": "Investigator started",
  "tool.called": "A record was looked up",
  "investigation.completed": "Investigator finished",
  "policy.completed": "Policy agent finished",
  "resolution.completed": "Resolution agent finished its draft",
  "validation.passed": "Draft passed the validator",
  "validation.failed": "Draft failed the validator",
  "injection_suspected": "Instruction-like customer text detected",
  "reply.guardrail_replaced": "Output guardrail replaced a reply",
  "clarification.requested": "Customer asked for more detail",
  "proposal.created": "Refund proposal stored",
  "proposal.refused": "Refund proposal refused by the rules",
  "approval.requested": "Approval requested",
  "approval.approved": "Refund approved by a reviewer",
  "approval.rejected": "Refund rejected by a reviewer",
  "approval.expired": "Approval expired",
  "approval.decision_refused": "Decision refused: the proposer cannot decide",
  "refund.reserved": "Refund amount reserved",
  "refund.submitted": "Payment service accepted the refund",
  "refund.processing": "Refund processing",
  "refund.succeeded": "Refund succeeded",
  "refund.failed": "Refund failed",
  "refund.reconciling": "Refund outcome unknown; reconciling",
  "refund.resent": "Refund re-sent during reconciliation",
  "refund.reconciled": "Refund outcome reconciled",
  "refund.reconciliation_stopped": "Reconciliation gave up",
  "ticket.updated": "Ticket status changed",
  "ticket.specialist_replied": "A reviewer replied to the customer",
  "ticket.resolved": "A reviewer resolved the ticket",
  "run.paused": "Run paused",
  "run.resumed": "Run resumed",
  "run.usage": "Usage recorded",
  "run.completed": "Run completed",
  "run.escalated": "Run escalated to a person",
  "run.failed": "Run failed",
};

export function reviewerEventLabel(type: string): string {
  return REVIEWER_EVENT_LABELS[type] ?? humanize(type);
}

const RUN_STATUS_LABELS: Record<string, string> = {
  created: "Received",
  investigating: "Investigating",
  awaiting_customer: "Waiting for your reply",
  awaiting_approval: "Awaiting approval",
  executing: "Processing",
  completed: "Completed",
  escalated: "With a specialist",
  failed: "Failed",
};

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? status;
}

// Ticket statuses as customers read them.
const TICKET_STATUS_LABELS: Record<string, string> = {
  open: "Open",
  in_progress: "In progress",
  awaiting_customer: "Waiting on you",
  resolved: "Resolved",
  escalated: "With a specialist",
};

export function ticketStatusLabel(status: string): string {
  return TICKET_STATUS_LABELS[status] ?? status;
}

const MEMORY_OUTCOME_LABELS: Record<string, string> = {
  status_explained: "Order status explained",
  refund_rejected: "Refund declined",
  refund_submitted: "Refund submitted",
  refund_processing: "Refund processing",
  refund_succeeded: "Refund completed",
  refund_failed: "Refund failed",
  escalated: "Passed to a person",
};

export function memoryOutcomeLabel(code: string): string {
  return MEMORY_OUTCOME_LABELS[code] ?? code;
}

// Codes like "order_status" or "NO_RETURN" as readable words.
export function humanize(code: string): string {
  const words = code.replace(/[_.]+/g, " ").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function formatDateTime(iso: string | null): string {
  if (iso === null) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}
