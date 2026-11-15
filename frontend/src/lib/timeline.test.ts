import { describe, expect, it } from "vitest";

import type { RunStepEvent } from "../api/types";
import { buildTimeline } from "./timeline";

let n = 0;
const step = (type: string, at: string): RunStepEvent => ({
  id: `e-${String(n++)}`,
  type,
  actor: "system",
  at,
  details: {},
});
const T = (seconds: number) => `2026-11-10T12:00:${String(seconds).padStart(2, "0")}Z`;

const types = (steps: RunStepEvent[], reviewer = false) =>
  buildTimeline(steps, reviewer).flatMap((phase) => phase.entries.map((entry) => entry.type));

describe("buildTimeline", () => {
  it("groups a run into Understanding, Checking, Deciding and Done, oldest first", () => {
    const steps = [
      step("run.created", T(0)),
      step("run.classified", T(1)),
      step("investigation.started", T(2)),
      step("policy.completed", T(3)),
      step("resolution.completed", T(4)),
      step("run.completed", T(5)),
    ];
    expect(buildTimeline(steps, false).map((phase) => [phase.title, phase.entries.map((e) => e.label)])).toEqual([
      ["Understanding", ["We've received your request", "Our assistant identified what you need help with"]],
      ["Checking", ["Our assistant is checking your order details", "Our assistant checked the applicable policy"]],
      ["Deciding", ["Our assistant prepared a resolution"]],
      ["Done", ["Resolved"]],
    ]);
  });

  it("merges 'ticket updated' into the run's end, whichever comes first", () => {
    const after = [step("run.completed", T(5)), step("ticket.updated", T(5))];
    const before = [step("ticket.updated", T(4)), step("run.completed", T(5))];
    expect(types(after)).toEqual(["run.completed"]);
    expect(types(before)).toEqual(["run.completed"]);
  });

  it("also merges it into an approval request", () => {
    expect(types([step("approval.requested", T(5)), step("ticket.updated", T(5))])).toEqual(["approval.requested"]);
  });

  it("keeps a ticket update that is not next to the end", () => {
    const steps = [step("proposal.created", T(1)), step("ticket.updated", T(2)), step("run.completed", T(59))];
    expect(types(steps)).toEqual(["proposal.created", "ticket.updated", "run.completed"]);
  });

  it("orders by the event order, even when events share one stored time", () => {
    const same = T(7);
    const steps = [
      step("run.completed", same),
      step("resolution.completed", same),
      step("run.classified", same),
      step("run.created", same),
    ];
    expect(types(steps)).toEqual(["run.created", "run.classified", "resolution.completed", "run.completed"]);
  });

  it("counts the same sentence repeated in a row instead of listing it again", () => {
    const steps = [step("validation.failed", T(1)), step("validation.passed", T(2))];
    const [phase] = buildTimeline(steps, false);
    expect(phase?.entries).toHaveLength(1);
    expect(phase?.entries[0]?.count).toBe(2);
  });

  it("gives reviewers their own wording", () => {
    const [phase] = buildTimeline([step("run.classified", T(1))], true);
    expect(phase?.entries[0]?.label).toBe("Classifier labelled the request");
  });

  it("starts a new group when the run goes back to an earlier phase", () => {
    const steps = [step("approval.requested", T(1)), step("investigation.started", T(2))];
    expect(buildTimeline(steps, false).map((phase) => phase.title)).toEqual(["Deciding", "Checking"]);
  });
});
