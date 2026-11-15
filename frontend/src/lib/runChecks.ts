// The "assistant at work" checks on the run page: which of five checks to show right now, and
// whether each is done, from the events the run has produced and the kinds of records it has
// checked. The page reveals a check when its event arrives and ticks it when it is done. This
// reads events and fixed codes only; it decides nothing (ADR-023).

import type { RunStatusEvent, RunStepEvent } from "../api/types";
import { formatDateOnly, formatDay } from "./dates";

export type CheckKey = "understand" | "order" | "delivery" | "policy" | "answer";

export interface Check {
  key: CheckKey;
  label: string; // present tense while active, e.g. "Checking your order"
  doneLabel: string; // shown once done, e.g. "Checked your order"
  state: "active" | "done";
  detail: string | null; // a verified detail, once done, when the API has one
}

// Statuses in which the assistant is still working on the request.
const WORKING = new Set(["created", "investigating"]);

export function isAssistantWorking(status: RunStatusEvent): boolean {
  return WORKING.has(status.status);
}

function orderDetail(status: RunStatusEvent): string | null {
  return status.facts === null ? null : `Order ${status.facts.order_number}`;
}

function deliveryDetail(status: RunStatusEvent): string | null {
  const facts = status.facts;
  if (facts === null) return null;
  if (facts.shipment !== null && facts.shipment.status === "in_transit") {
    return `In transit · expected ${formatDateOnly(facts.shipment.expected_delivery)}`;
  }
  if (facts.order_status === "delivered" && facts.delivered_at !== null) return `Delivered ${formatDay(facts.delivered_at)}`;
  return null;
}

export function runChecks(status: RunStatusEvent, steps: RunStepEvent[]): Check[] {
  const seen = new Set(steps.map((step) => step.type));
  const has = (source: string) => status.checked.includes(source);

  const classified = seen.has("run.classified");
  const policyDone = seen.has("policy.completed") || has("policy");
  const answered = seen.has("resolution.completed");
  const orderDone = has("order") || policyDone || answered;
  const deliveryDone = has("shipment");

  const checks: Check[] = [
    {
      key: "understand",
      label: "Understanding your request",
      doneLabel: "Understood your request",
      state: classified ? "done" : "active",
      detail: null,
    },
  ];
  const orderVisible = classified || seen.has("investigation.started");
  if (orderVisible) {
    checks.push({
      key: "order",
      label: "Checking your order",
      doneLabel: "Checked your order",
      state: orderDone ? "done" : "active",
      detail: orderDone ? orderDetail(status) : null,
    });
  }
  // Delivery tracking is only shown if the run looked at it, or may still do so.
  if (deliveryDone || (orderVisible && !policyDone && !answered)) {
    checks.push({
      key: "delivery",
      label: "Checking delivery tracking",
      doneLabel: "Checked delivery tracking",
      state: deliveryDone ? "done" : "active",
      detail: deliveryDone ? deliveryDetail(status) : null,
    });
  }
  if (orderDone) {
    checks.push({
      key: "policy",
      label: "Checking the policy",
      doneLabel: "Checked the policy",
      state: policyDone ? "done" : "active",
      detail: null,
    });
  }
  if (policyDone || answered) {
    checks.push({
      key: "answer",
      label: "Preparing an answer",
      doneLabel: "Prepared an answer",
      state: answered ? "done" : "active",
      detail: null,
    });
  }
  return checks;
}
