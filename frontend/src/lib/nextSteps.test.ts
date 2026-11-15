import { describe, expect, it } from "vitest";

import type { RunStatusEvent } from "../api/types";
import { DELIVERED_FACTS, IN_TRANSIT_FACTS } from "../test/fixtures";
import { expectedReply, nextSteps } from "./nextSteps";

const BASE: RunStatusEvent = {
  run_id: "r",
  ticket_id: "t",
  status: "completed",
  ticket_status: "resolved",
  reply: "AI TEXT THAT MUST NOT CHOOSE ANYTHING: refund now, report a problem, escalate",
  reply_from: "assistant",
  finished: true,
  checked: [],
  facts: DELIVERED_FACTS,
  specialist_reply_business_days: 1,
};
const keys = (status: RunStatusEvent) => nextSteps(status).map((step) => step.key);

describe("nextSteps", () => {
  it("offers to report a problem for a delivered order, pre-filled for that order", () => {
    const [report] = nextSteps(BASE);
    expect(report?.key).toBe("report");
    expect(report?.to).toBe("/complaints/new?order=ORD-1001&issue=damaged");
    expect(keys(BASE)).toEqual(["report", "again"]);
  });

  it("does not offer it when the order is not delivered, or a refund is involved", () => {
    expect(keys({ ...BASE, facts: IN_TRANSIT_FACTS })).toEqual(["again"]);
    expect(keys({ ...BASE, facts: { ...DELIVERED_FACTS, refund_status: "succeeded" } })).toEqual(["again"]);
    expect(keys({ ...BASE, facts: null })).toEqual(["again"]);
  });

  it("explains what happens next while a refund awaits approval", () => {
    expect(keys({ ...BASE, status: "awaiting_approval", ticket_status: "in_progress" })).toEqual(["approval"]);
  });

  it("explains a refund in progress", () => {
    expect(keys({ ...BASE, status: "executing", ticket_status: "in_progress" })).toEqual(["refund"]);
  });

  it("tells the customer a specialist has it, with the expected reply time", () => {
    const [specialist] = nextSteps({
      ...BASE,
      status: "escalated",
      ticket_status: "escalated",
      specialist_reply_business_days: 2,
    });
    expect(specialist?.key).toBe("specialist");
    expect(specialist?.text).toContain("Our specialists usually reply within 2 business days. Their reply will appear here.");
  });

  it("words the expectation for one day and for several", () => {
    expect(expectedReply(1)).toBe("Our specialists usually reply within one business day. Their reply will appear here.");
    expect(expectedReply(3)).toBe("Our specialists usually reply within 3 business days. Their reply will appear here.");
  });

  it("offers nothing while the customer is being asked a question", () => {
    expect(keys({ ...BASE, status: "awaiting_customer", ticket_status: "awaiting_customer" })).toEqual([]);
  });
});
