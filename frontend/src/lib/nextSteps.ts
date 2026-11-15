// What the customer can do next, chosen by code from statuses the API returned. No AI text
// decides any of it, and nothing here computes eligibility or amounts (ADR-023). Each step is
// either an action (a link) or a short explanation.

import type { RunStatusEvent } from "../api/types";

export interface NextStep {
  key: "specialist" | "approval" | "refund" | "report" | "again" | "failed";
  kind: "action" | "info";
  title: string;
  text: string;
  to?: string; // an action's link
  linkLabel?: string;
}

// The expectation is a setting on the server (days), worded here as an expectation, not a promise.
export function expectedReply(days: number): string {
  const amount = days === 1 ? "one business day" : `${String(days)} business days`;
  return `Our specialists usually reply within ${amount}. Their reply will appear here.`;
}

export function nextSteps(status: RunStatusEvent): NextStep[] {
  const steps: NextStep[] = [];
  const facts = status.facts;

  if (status.status === "escalated" && status.ticket_status === "escalated") {
    const replied = status.reply_from === "specialist";
    steps.push({
      key: "specialist",
      kind: "info",
      title: "A specialist is looking at this",
      text: replied
        ? "They have replied below and may follow up here if anything else is needed."
        : `A member of our support team is reviewing your case. ${expectedReply(status.specialist_reply_business_days)} You don't need to do anything.`,
    });
  }
  if (status.status === "awaiting_approval") {
    steps.push({
      key: "approval",
      kind: "info",
      title: "What happens next",
      text: "A member of our team reviews every refund before it goes ahead. You don't need to do anything: the decision will appear on this page.",
    });
  }
  if (status.status === "executing" || facts?.refund_status === "in_progress") {
    steps.push({
      key: "refund",
      kind: "info",
      title: "Your refund is being processed",
      text: "We're waiting for the payment service to confirm it. The final result will appear on this page.",
    });
  }
  if (status.status === "failed") {
    steps.push({
      key: "failed",
      kind: "info",
      title: "A person will follow up",
      text: "Something went wrong on our side. Your request is saved, and a member of our team will pick it up.",
    });
  }
  // A delivered order with no refund involved: offer to report a problem with the item.
  if (status.status === "completed" && facts?.order_status === "delivered" && facts.refund_status === null) {
    steps.push({
      key: "report",
      kind: "action",
      title: "Something wrong with this item?",
      text: `Tell us what's wrong with order ${facts.order_number} and we'll look into it.`,
      to: `/complaints/new?order=${encodeURIComponent(facts.order_number)}&issue=damaged`,
      linkLabel: "Report a problem with this item",
    });
  }
  if (status.ticket_status === "resolved") {
    steps.push({
      key: "again",
      kind: "action",
      title: "Need anything else?",
      text: "You can start a new request at any time.",
      to: "/complaints/new",
      linkLabel: "Start another request",
    });
  }
  return steps;
}
