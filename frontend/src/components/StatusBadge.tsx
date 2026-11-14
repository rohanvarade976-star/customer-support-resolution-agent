// A status code from the API as a badge: a tone that matches its meaning, and readable words.
// An unknown code is shown as itself in neutral gray, never hidden or guessed at.

import { humanize } from "../lib/labels";
import { Badge, type BadgeTone } from "./ui/badge";

const STATUS_TONE: Record<string, BadgeTone> = {
  // done and good
  completed: "success",
  resolved: "success",
  succeeded: "success",
  approved: "success",
  delivered: "success",
  // in motion: nothing for the person to do
  created: "info",
  open: "info",
  in_progress: "info",
  investigating: "info",
  awaiting_approval: "info",
  executing: "info",
  processing: "info",
  submitted: "info",
  reserved: "info",
  shipped: "info",
  // waiting on someone, or needs a person
  awaiting_customer: "warning",
  pending: "warning",
  pending_reconciliation: "warning",
  escalated: "warning",
  // ended badly
  failed: "danger",
  rejected: "danger",
  expired: "danger",
  cancelled: "danger",
};

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  return (
    <Badge tone={STATUS_TONE[status] ?? "neutral"} dot>
      {label ?? humanize(status)}
    </Badge>
  );
}
