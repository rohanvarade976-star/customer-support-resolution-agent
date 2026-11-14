import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { PendingApproval } from "../api/types";
import { ANITA, fakeApi, json, logInAs, renderApp } from "../test/helpers";

function approval(id: string, name: string, amountMinor: number, minutesLeft: number): PendingApproval {
  return {
    approval_id: id,
    proposal_id: `p-${id}`,
    ticket_id: `t-${id}`,
    order_number: `ORD-${id}`,
    customer_name: name,
    amount_minor: amountMinor,
    currency: "INR",
    reason: "Return received",
    reasons: [],
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + minutesLeft * 60_000).toISOString(),
  };
}

describe("ApprovalQueuePage", () => {
  it("lists approvals soonest-expiry first, sorts by amount, and links each row to its case", async () => {
    fakeApi({
      "GET /approvals": () =>
        json({ items: [approval("1", "Priya Sharma", 50000, 5), approval("2", "Arjun Mehta", 249900, 45)] }),
    });
    logInAs(ANITA);
    renderApp("/approvals");

    const table = await screen.findByRole("table");
    const names = () =>
      within(table)
        .getAllByRole("row")
        .slice(1)
        .map((row) => within(row).getByRole("link").textContent);
    expect(names()).toEqual(["Priya Sharma", "Arjun Mehta"]);
    // Five minutes left: urgent, and counted in the summary.
    expect(screen.getByText("1 expiring within 10 minutes")).toBeInTheDocument();

    await userEvent.setup().click(within(table).getByRole("button", { name: "Amount" }));
    expect(names()).toEqual(["Arjun Mehta", "Priya Sharma"]); // largest first
    expect(within(table).getByRole("columnheader", { name: "Amount" })).toHaveAttribute("aria-sort", "descending");
    expect(within(table).getByRole("link", { name: "Arjun Mehta" })).toHaveAttribute("href", "/approvals/2");
  });

  it("says when nothing is waiting", async () => {
    fakeApi({ "GET /approvals": () => json({ items: [] }) });
    logInAs(ANITA);
    renderApp("/approvals");

    expect(await screen.findByText("You're all caught up")).toBeInTheDocument();
  });
});
