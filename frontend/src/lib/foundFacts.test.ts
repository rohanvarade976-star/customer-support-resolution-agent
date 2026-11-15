import { describe, expect, it } from "vitest";

import { DELIVERED_FACTS, IN_TRANSIT_FACTS } from "../test/fixtures";
import { foundFacts } from "./foundFacts";

describe("foundFacts", () => {
  it("lists the verified facts the API sent, in plain words", () => {
    expect(foundFacts(DELIVERED_FACTS).map((f) => [f.label, f.value])).toEqual([
      ["Order", "ORD-1001 · Delivered on 3 Nov"],
      ["Return", "Return received"],
    ]);
  });

  it("shows the carrier's status and expected date while in transit", () => {
    expect(foundFacts(IN_TRANSIT_FACTS).map((f) => [f.label, f.value])).toEqual([
      ["Order", "ORD-1002 · On its way"],
      ["Delivery", "In transit with the carrier · expected 18 Nov"],
    ]);
  });

  it("says nothing about a refund unless the API reports one, and never shows an amount", () => {
    const lines = foundFacts({ ...DELIVERED_FACTS, refund_status: "in_progress" });
    expect(lines.at(-1)?.value).toBe("Refund is being processed");
    expect(lines.some((line) => /₹|\d{3,}/.test(line.value.replace("ORD-1001", "")))).toBe(false);
  });

  it("is empty when the ticket has no order", () => {
    expect(foundFacts(null)).toEqual([]);
  });
});
