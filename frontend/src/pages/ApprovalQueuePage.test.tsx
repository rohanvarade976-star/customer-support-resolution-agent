import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { PendingApproval, ReadinessReason } from "../api/types";
import { SUMMARY } from "../test/fixtures";
import { ANITA, fakeApi, json, logInAs, renderApp } from "../test/helpers";

const SUMMARY_ROUTE = { "GET /reviewer/automation-summary": () => json(SUMMARY) };

function approval(
  id: string,
  name: string,
  amountMinor: number,
  minutesLeft: number,
  reasons: ReadinessReason[] = [],
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
    risk_flags: [],
    readiness: { status: reasons.length === 0 ? "ready_to_approve" : "needs_attention", reasons },
    proposed_by_you: false,
  };
}

const RISK: ReadinessReason = { code: "RISK_FLAG", codes: ["DELIVERY_DATE_CONFLICT", "INJECTION_SUSPECTED"] };
const EXPIRING: ReadinessReason = { code: "EXPIRING_SOON", codes: [] };

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

  it("splits the queue into Ready to approve and Needs attention tabs, with counts and the top reason", async () => {
    fakeApi({
      ...SUMMARY_ROUTE,
      "GET /approvals": () =>
        json({
          items: [
            approval("1", "Priya Sharma", 50000, 30, [RISK, EXPIRING]),
            approval("2", "Arjun Mehta", 249900, 45),
            approval("3", "Meera Iyer", 120000, 50),
          ],
        }),
    });
    logInAs(ANITA);
    renderApp("/approvals");

    const tabs = await screen.findByRole("tablist", { name: "Readiness" });
    const ready = within(tabs).getByRole("tab", { name: /Ready to approve/ });
    const attention = within(tabs).getByRole("tab", { name: /Needs attention/ });
    expect(ready).toHaveTextContent("2");
    expect(attention).toHaveTextContent("1");
    // Ready cases come first: the quick decisions.
    expect(ready).toHaveAttribute("aria-selected", "true");
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row").slice(1).map((row) => within(row).getByRole("link").textContent)).toEqual([
      "Arjun Mehta",
      "Meera Iyer",
    ]);
    expect(within(table).getAllByTestId("readiness")[0]).toHaveTextContent("Ready");

    await userEvent.setup().click(attention);
    expect(attention).toHaveAttribute("aria-selected", "true");
    const [row] = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    if (row === undefined) throw new Error("expected one row");
    const badge = within(row).getByTestId("readiness");
    // The most serious reason in plain words, and how many more.
    expect(badge).toHaveTextContent("Needs attention");
    expect(badge).toHaveTextContent("Risk flag: Delivery dates disagree, Instruction-like text");
    expect(badge).toHaveTextContent("+1 more");
  });

  it("opens on Needs attention when nothing is ready", async () => {
    fakeApi({ ...SUMMARY_ROUTE, "GET /approvals": () => json({ items: [approval("1", "Priya Sharma", 50000, 30, [EXPIRING])] }) });
    logInAs(ANITA);
    renderApp("/approvals");

    const tabs = await screen.findByRole("tablist", { name: "Readiness" });
    expect(within(tabs).getByRole("tab", { name: /Needs attention/ })).toHaveAttribute("aria-selected", "true");
    expect(within(screen.getByRole("table")).getByText("Expires in less than 10 minutes")).toBeInTheDocument();
  });

  it("shows the week's automation summary, and each row's AI recommendation", async () => {
    fakeApi({
      ...SUMMARY_ROUTE,
      "GET /approvals": () =>
        json({
          items: [approval("1", "Priya Sharma", 50000, 30), approval("2", "Arjun Mehta", 249900, 45)],
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
    expect(within(arjun).getByText("Refund")).toBeInTheDocument();
  });

  it("keeps the queue working when the summary cannot load", async () => {
    fakeApi({ "GET /approvals": () => json({ items: [approval("1", "Priya Sharma", 50000, 30)] }) });
    logInAs(ANITA);
    renderApp("/approvals");

    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.queryByText("Resolved automatically")).not.toBeInTheDocument();
  });
});
