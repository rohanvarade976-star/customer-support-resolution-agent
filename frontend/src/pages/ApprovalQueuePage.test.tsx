import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { PendingApproval } from "../api/types";
import { SUMMARY } from "../test/fixtures";
import { ANITA, fakeApi, json, logInAs, renderApp } from "../test/helpers";

const SUMMARY_ROUTE = { "GET /reviewer/automation-summary": () => json(SUMMARY) };

function approval(
  id: string,
  name: string,
  amountMinor: number,
  minutesLeft: number,
  riskFlags: string[] = [],
): PendingApproval {
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
    run_id: `run-${id}`,
    ai_action: "refund",
    risk_flags: riskFlags,
  };
}

describe("ApprovalQueuePage", () => {
  it("lists approvals soonest-expiry first, sorts by amount, and links each row to its case", async () => {
    fakeApi({
      ...SUMMARY_ROUTE,
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
    fakeApi({ ...SUMMARY_ROUTE, "GET /approvals": () => json({ items: [] }) });
    logInAs(ANITA);
    renderApp("/approvals");

    expect(await screen.findByText("You're all caught up")).toBeInTheDocument();
  });

  it("shows the week's automation summary, and each row's AI recommendation and risk flags", async () => {
    fakeApi({
      ...SUMMARY_ROUTE,
      "GET /approvals": () =>
        json({
          items: [
            approval("1", "Priya Sharma", 50000, 30, ["DELIVERY_DATE_CONFLICT", "INJECTION_SUSPECTED"]),
            approval("2", "Arjun Mehta", 249900, 45),
          ],
        }),
    });
    logInAs(ANITA);
    renderApp("/approvals");

    const summary = await screen.findByRole("region", { name: "Automation in the last 7 days" });
    expect(await within(summary).findByText("27")).toBeInTheDocument(); // resolved automatically
    expect(within(summary).getByText("Resolved automatically")).toBeInTheDocument();
    expect(within(summary).getByText("9")).toBeInTheDocument(); // sent for approval
    expect(within(summary).getByText("6")).toBeInTheDocument(); // escalated
    expect(within(summary).getByText("8.4 s")).toBeInTheDocument();

    const table = await screen.findByRole("table");
    const [priya, arjun] = within(table).getAllByRole("row").slice(1);
    if (priya === undefined || arjun === undefined) throw new Error("expected two rows");
    expect(within(priya).getByText("Refund")).toBeInTheDocument();
    expect(within(priya).getByText("AI recommends:")).toBeInTheDocument();
    const risk = within(priya).getByTestId("risk-indicator");
    expect(risk).toHaveTextContent("2 risk flags");
    expect(risk).toHaveAttribute("title", "Delivery dates disagree · Instruction-like text");
    expect(within(arjun).getByText("No flags")).toBeInTheDocument();
  });

  it("keeps the queue working when the summary cannot load", async () => {
    fakeApi({ "GET /approvals": () => json({ items: [approval("1", "Priya Sharma", 50000, 30)] }) });
    logInAs(ANITA);
    renderApp("/approvals");

    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.queryByText("Resolved automatically")).not.toBeInTheDocument();
  });
});
