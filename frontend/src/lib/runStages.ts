// The run page's progress stepper: which of four stages a run is in, from the status the
// API sent. A fixed lookup for display only: it decides nothing and infers nothing beyond the
// status itself. The detailed timeline below the stepper shows what actually happened.

import type { RunStatus } from "../api/types";
import { runStatusLabel } from "./labels";

export type StageState = "complete" | "current" | "upcoming";
export type StageTone = "default" | "attention" | "warning" | "danger" | "success";

export interface Stage {
  key: "received" | "investigating" | "decision" | "outcome";
  label: string;
  state: StageState;
  tone: StageTone;
}

const STAGE_KEYS = ["received", "investigating", "decision", "outcome"] as const;
const DEFAULT_LABELS = ["Received", "Investigating", "Decision", "Resolved"];

// For each status: the stage it is in, how to colour that stage, and a sentence for customers.
const STATUS_STAGE: Record<RunStatus, { index: number; tone: StageTone; summary: string }> = {
  created: { index: 0, tone: "default", summary: "We've received your request and will start on it shortly." },
  investigating: { index: 1, tone: "default", summary: "We're checking your order details and the policy that applies." },
  awaiting_customer: { index: 2, tone: "attention", summary: "Please answer the question below so we can continue." },
  awaiting_approval: { index: 2, tone: "default", summary: "A member of our team is reviewing the proposed refund." },
  executing: { index: 2, tone: "default", summary: "Your refund is being processed with the payment service." },
  completed: { index: 3, tone: "success", summary: "This request is resolved." },
  escalated: { index: 3, tone: "warning", summary: "A support specialist has your case and will reply here." },
  failed: { index: 3, tone: "danger", summary: "Something went wrong on our side. A person will follow up." },
};

const FINAL_STATUSES = new Set<RunStatus>(["completed", "escalated", "failed"]);

export interface RunProgress {
  stages: Stage[];
  headline: string; // e.g. "Waiting for your reply"
  summary: string; // one sentence under the headline
  tone: StageTone;
}

export function runProgress(status: RunStatus): RunProgress {
  const { index, tone, summary } = STATUS_STAGE[status];
  const finished = FINAL_STATUSES.has(status);
  const stages = STAGE_KEYS.map((key, i): Stage => {
    // A finished run has completed every stage; the last one carries its outcome.
    const state: StageState = i < index || (finished && i === index) ? "complete" : i === index ? "current" : "upcoming";
    const label = i === index ? runStatusLabel(status) : (DEFAULT_LABELS[i] ?? key);
    return { key, label, state, tone: i === index ? tone : "default" };
  });
  return { stages, headline: runStatusLabel(status), summary, tone };
}
