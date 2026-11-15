import { describe, expect, it } from "vitest";

import type { RunStatusEvent, RunStepEvent } from "../api/types";
import { DELIVERED_FACTS, IN_TRANSIT_FACTS } from "../test/fixtures";
import { isAssistantWorking, runChecks } from "./runChecks";

const BASE: RunStatusEvent = {
  run_id: "r",
  ticket_id: "t",
  status: "investigating",
  ticket_status: "in_progress",
  reply: null,
  reply_from: "assistant",
  finished: false,
  checked: [],
  facts: DELIVERED_FACTS,
  specialist_reply_business_days: 1,
};

const step = (type: string, i = 0): RunStepEvent => ({
  id: `${type}-${String(i)}`,
  type,
  actor: "system",
  at: "2026-11-10T12:00:00Z",
  details: {},
});
const summary = (status: RunStatusEvent, steps: RunStepEvent[]) =>
  runChecks(status, steps).map((check) => `${check.key}:${check.state}`);

describe("runChecks", () => {
  it("starts with one check, running", () => {
    expect(summary(BASE, [step("run.created")])).toEqual(["understand:active"]);
  });

  it("reveals each check as its event arrives and ticks the earlier ones", () => {
    const classified = [step("run.created"), step("run.classified")];
    expect(summary(BASE, classified)).toEqual(["understand:done", "order:active", "delivery:active"]);

    const ordered = { ...BASE, checked: ["order", "shipment"] };
    expect(summary(ordered, [...classified, step("investigation.started")])).toEqual([
      "understand:done",
      "order:done",
      "delivery:done",
      "policy:active",
    ]);

    const policy = { ...BASE, checked: ["order", "shipment", "policy"] };
    expect(summary(policy, [...classified, step("policy.completed")])).toEqual([
      "understand:done",
      "order:done",
      "delivery:done",
      "policy:done",
      "answer:active",
    ]);
    expect(summary(policy, [...classified, step("policy.completed"), step("resolution.completed")]).at(-1)).toBe(
      "answer:done",
    );
  });

  it("drops the delivery check when the run never looked at tracking", () => {
    const status = { ...BASE, checked: ["order", "policy"] };
    const steps = [step("run.classified"), step("policy.completed")];
    expect(summary(status, steps)).toEqual(["understand:done", "order:done", "policy:done", "answer:active"]);
  });

  it("adds a verified detail from the API's facts, never from text", () => {
    const delivered = runChecks({ ...BASE, checked: ["order", "shipment"] }, [step("run.classified")]);
    expect(delivered.find((c) => c.key === "order")?.detail).toBe("Order ORD-1001");
    expect(delivered.find((c) => c.key === "delivery")?.detail).toBe("Delivered 3 Nov");

    const transit = runChecks({ ...BASE, facts: IN_TRANSIT_FACTS, checked: ["order", "shipment"] }, [
      step("run.classified"),
    ]);
    expect(transit.find((c) => c.key === "delivery")?.detail).toBe("In transit · expected 18 Nov");

    const noFacts = runChecks({ ...BASE, facts: null, checked: ["order", "shipment"] }, [step("run.classified")]);
    expect(noFacts.every((check) => check.detail === null)).toBe(true);
  });

  it("is only shown while the assistant works", () => {
    expect(isAssistantWorking({ ...BASE, status: "created" })).toBe(true);
    expect(isAssistantWorking(BASE)).toBe(true);
    for (const status of ["awaiting_customer", "awaiting_approval", "executing", "completed", "escalated", "failed"] as const) {
      expect(isAssistantWorking({ ...BASE, status })).toBe(false);
    }
  });
});
