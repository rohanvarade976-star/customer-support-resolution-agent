// Fixed wording for codes the API returns. A code with no entry is shown as itself, so a new
// backend code is never hidden or guessed at.

// The customer-facing sentence for each live event (PRD "Live events").
const EVENT_LABELS: Record<string, string> = {
  "run.created": "We've received your request",
  "run.classified": "We've identified what you need help with",
  "investigation.started": "Checking your order details",
  "policy.completed": "Checking the applicable policy",
  "clarification.requested": "We need one more detail from you",
  "proposal.created": "Preparing a resolution",
  "resolution.completed": "Preparing a resolution",
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
  "run.completed": "Resolved",
  "run.escalated": "Passed to a support specialist",
  "run.failed": "Something went wrong; a person will follow up",
};

export function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? type;
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
