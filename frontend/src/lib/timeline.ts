// The activity list: events grouped into phases (Understanding, Checking, Deciding, Done),
// oldest first, with redundant entries merged. Pure display logic over the events the API sent;
// it adds no facts of its own (ADR-023).
//
// - Order: by time, then (for events stored at the same instant, such as before the
//   `clock_timestamp()` migration) by where the event belongs in a run, then as received.
// - Merged: a "ticket updated" next to the run's end (or an approval request) says the same
//   thing twice, so it is dropped; the same sentence twice in a row becomes one entry with a count.

import type { RunStepEvent } from "../api/types";
import { eventLabel, reviewerEventLabel } from "./labels";

export type PhaseKey = "understanding" | "checking" | "deciding" | "done";

export const PHASE_TITLES: Record<PhaseKey, string> = {
  understanding: "Understanding",
  checking: "Checking",
  deciding: "Deciding",
  done: "Done",
};

// Where each known event type belongs, in the order a run produces them. Types not listed
// (ticket.updated, anything new) join the phase of the entry before them.
const PHASES: Record<PhaseKey, string[]> = {
  understanding: ["run.created", "run.classified", "supervisor.decided", "injection_suspected"],
  checking: ["investigation.started", "tool.called", "investigation.completed", "policy.completed"],
  deciding: [
    "resolution.completed",
    "validation.passed",
    "validation.failed",
    "reply.guardrail_replaced",
    "clarification.requested",
    "proposal.created",
    "proposal.refused",
    "approval.requested",
    "approval.approved",
    "approval.rejected",
    "approval.expired",
    "approval.decision_refused",
    "refund.reserved",
    "refund.submitted",
    "refund.processing",
    "refund.reconciling",
    "refund.resent",
    "refund.reconciled",
    "run.paused",
    "run.resumed",
  ],
  done: [
    "refund.succeeded",
    "refund.failed",
    "refund.reconciliation_stopped",
    "ticket.specialist_replied",
    "ticket.resolved",
    "run.usage",
    "run.completed",
    "run.escalated",
    "run.failed",
  ],
};

const PHASE_ORDER: PhaseKey[] = ["understanding", "checking", "deciding", "done"];
const PHASE_OF = new Map<string, PhaseKey>();
const RANK_OF = new Map<string, number>();
for (const [phase, types] of Object.entries(PHASES) as [PhaseKey, string[]][]) {
  for (const type of types) {
    PHASE_OF.set(type, phase);
    RANK_OF.set(type, PHASE_ORDER.indexOf(phase) * 100 + types.indexOf(type));
  }
}

const RUN_ENDS = new Set(["run.completed", "run.escalated", "run.failed"]);
// A ticket update this close to one of these just restates it (the status change they cause).
const RESTATED_BY = new Set([...RUN_ENDS, "approval.requested"]);
const REDUNDANT_WINDOW_MS = 10_000; // a ticket update this close to a run's end is the same news

export interface TimelineEntry {
  id: string;
  type: string;
  label: string;
  at: string;
  actor: string;
  count: number; // 1, or more when identical entries in a row were merged
}

export interface TimelinePhase {
  key: PhaseKey;
  title: string;
  entries: TimelineEntry[];
}

function timeOf(step: RunStepEvent): number {
  const time = new Date(step.at).getTime();
  return Number.isNaN(time) ? 0 : time;
}

function ordered(steps: RunStepEvent[]): RunStepEvent[] {
  return steps
    .map((step, index) => ({ step, index }))
    .sort(
      (a, b) =>
        timeOf(a.step) - timeOf(b.step) ||
        (RANK_OF.get(a.step.type) ?? 1000) - (RANK_OF.get(b.step.type) ?? 1000) ||
        a.index - b.index,
    )
    .map(({ step }) => step);
}

function dropRedundantTicketUpdates(steps: RunStepEvent[]): RunStepEvent[] {
  const endTimes = steps.filter((step) => RESTATED_BY.has(step.type)).map(timeOf);
  return steps.filter(
    (step) =>
      step.type !== "ticket.updated" ||
      !endTimes.some((end) => Math.abs(end - timeOf(step)) <= REDUNDANT_WINDOW_MS),
  );
}

export function buildTimeline(steps: RunStepEvent[], reviewer: boolean): TimelinePhase[] {
  const kept = dropRedundantTicketUpdates(ordered(steps));
  const phases: TimelinePhase[] = [];
  let current: PhaseKey = "understanding";
  for (const step of kept) {
    current = PHASE_OF.get(step.type) ?? current; // unknown types stay where the run was
    const label = reviewer ? reviewerEventLabel(step.type) : eventLabel(step.type);
    let phase = phases.at(-1);
    if (phase?.key !== current) {
      phase = { key: current, title: PHASE_TITLES[current], entries: [] };
      phases.push(phase);
    }
    const previous = phase.entries.at(-1);
    if (previous?.label === label) {
      previous.count += 1; // the same sentence again: one entry, counted
    } else {
      phase.entries.push({ id: step.id, type: step.type, label, at: step.at, actor: step.actor, count: 1 });
    }
  }
  return phases;
}
