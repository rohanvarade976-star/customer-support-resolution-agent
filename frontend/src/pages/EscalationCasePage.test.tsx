import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { EscalationCase, EscalationItem } from "../api/types";
import { ANALYSIS } from "../test/fixtures";
import { ANITA, fakeApi, json, logInAs, renderApp } from "../test/helpers";

const ITEM: EscalationItem = {
  ticket_id: "t-9",
  run_id: "run-9",
  customer_name: "Priya Sharma",
  order_number: "ORD-1001",
  category: "refund",
  complaint: "I want to speak to someone",
  escalated_at: "2026-11-10T12:00:00Z",
  reasons: [
    { code: "AI_CHOSE_TO_ESCALATE", codes: [] },
    { code: "RISK_FLAG", codes: ["LOW_CLASSIFIER_CONFIDENCE"] },
  ],
  specialist_replied: false,
};

const CASE: EscalationCase = {
  ...ITEM,
  ticket_status: "escalated",
  reply: "Your case needs a closer look from a member of our support team.",
  reply_from: "assistant",
  facts: {
    order_status: "delivered",
    delivered_at: "2026-11-01T10:00:00Z",
    return_status: "received",
    return_requested_at: "2026-11-03T10:00:00Z",
    return_received_at: "2026-11-06T10:00:00Z",
    inspection_status: "passed",
    paid_amount_minor: 249900,
    succeeded_refunds_minor: 0,
    in_progress_refunds_minor: 0,
    refundable_balance_minor: 249900,
    refund_window_days: 30,
    eligible_now: true,
    reasons_now: [],
  },
  currency: "INR",
  governing_policy: { policy_key: "refund_policy", version: "v2", title: "Refund Policy", refund_window_days: 30 },
};

const ROUTES = {
  "GET /reviewer/escalations": () => json({ items: [ITEM] }),
  "GET /runs/run-9/analysis": () => json({ ...ANALYSIS, run_id: "run-9" }),
};

describe("Escalations", () => {
  it("lists escalated cases with plain reasons, and counts them in the sidebar", async () => {
    fakeApi(ROUTES);
    logInAs(ANITA);
    renderApp("/escalations");

    const list = await screen.findByRole("list", { name: "Escalated cases" });
    expect(within(list).getByText("Priya Sharma")).toBeInTheDocument();
    expect(within(list).getByText("The AI decided a person should handle this")).toBeInTheDocument();
    expect(within(list).getByText("Risk flag: Low classification confidence")).toBeInTheDocument();
    expect(within(list).getByRole("link")).toHaveAttribute("href", "/escalations/t-9");

    const nav = screen.getAllByRole("navigation", { name: "Main" })[0];
    if (nav === undefined) throw new Error("expected the sidebar");
    expect(await within(nav).findByTestId("escalation-count")).toHaveTextContent("1");
  });

  it("sends a reply only after confirming it, then resolves with a note", async () => {
    let current: EscalationCase = CASE;
    const calls = fakeApi({
      ...ROUTES,
      "GET /reviewer/escalations/t-9": () => json(current),
      "POST /reviewer/escalations/t-9/reply": (call) => {
        const body = call.body as { text: string };
        current = { ...CASE, reply: body.text, reply_from: "specialist", specialist_replied: true };
        return json({ ticket_id: "t-9", ticket_status: "escalated", reply_from: "specialist" });
      },
      "POST /reviewer/escalations/t-9/resolve": () =>
        json({ ticket_id: "t-9", ticket_status: "resolved", reply_from: "specialist" }),
    });
    logInAs(ANITA);
    renderApp("/escalations/t-9");

    const reasons = await screen.findByTestId("escalation-reasons");
    expect(reasons).toHaveTextContent("The AI decided a person should handle this");
    // The analysis and order facts are there, collapsed.
    expect(await screen.findByTestId("section-ai-analysis")).not.toHaveAttribute("open");
    expect(screen.getByTestId("section-order-facts")).not.toHaveAttribute("open");

    // Resolving needs a reply first.
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Resolve the ticket"), "Called the warehouse");
    expect(screen.getByRole("button", { name: "Resolve ticket" })).toBeDisabled();

    await user.type(screen.getByLabelText("Reply to the customer"), "We checked your return with the warehouse.");
    await user.click(screen.getByRole("button", { name: "Send reply" }));
    const dialog = await screen.findByRole("dialog", { name: "Send this reply to the customer?" });
    expect(dialog).toHaveTextContent("We checked your return with the warehouse.");
    expect(calls.some((call) => call.method === "POST")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Send reply" }));

    await waitFor(() => {
      expect(calls.find((call) => call.path.endsWith("/reply"))?.body).toEqual({
        text: "We checked your return with the warehouse.",
      });
    });
    expect(await screen.findByText("Specialist reply")).toBeInTheDocument();
    expect(screen.getByTestId("current-reply")).toHaveTextContent("We checked your return with the warehouse.");

    const resolve = screen.getByRole("button", { name: "Resolve ticket" });
    await waitFor(() => {
      expect(resolve).toBeEnabled();
    });
    await user.click(resolve);
    const confirm = await screen.findByRole("dialog", { name: "Resolve this ticket?" });
    await user.click(within(confirm).getByRole("button", { name: "Resolve ticket" }));
    await waitFor(() => {
      expect(calls.find((call) => call.path.endsWith("/resolve"))?.body).toEqual({ note: "Called the warehouse" });
    });
    expect(await screen.findByRole("heading", { name: "Escalations" })).toBeInTheDocument();
  });

  it("explains why the guardrail refused a reply", async () => {
    fakeApi({
      ...ROUTES,
      "GET /reviewer/escalations/t-9": () => json(CASE),
      "POST /reviewer/escalations/t-9/reply": () =>
        json(
          {
            success: false,
            error_code: "REPLY_REJECTED",
            message: "This reply cannot be sent as written.",
            details: { codes: ["REPLY_CONTACT_DETAILS"] },
          },
          422,
        ),
    });
    logInAs(ANITA);
    renderApp("/escalations/t-9");

    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Reply to the customer"), "Mail me at a@b.com");
    await user.click(screen.getByRole("button", { name: "Send reply" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Send reply" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This reply cannot be sent as written.");
    expect(alert).toHaveTextContent("It contains an email address or a phone number.");
  });

  it("proposes a refund after confirming, then opens the new approval", async () => {
    const calls = fakeApi({
      ...ROUTES,
      "GET /reviewer/escalations/t-9": () => json(CASE),
      "POST /tickets/t-9/refund-proposals": () =>
        json(
          {
            proposal_id: "p-9",
            approval_id: "a-9",
            run_id: "run-10",
            ticket_id: "t-9",
            order_number: "ORD-1001",
            amount_minor: 249900,
            currency: "INR",
            payload_hash: "abc",
            status: "pending_approval",
            expires_at: "2026-11-10T13:00:00Z",
          },
          201,
        ),
    });
    logInAs(ANITA);
    renderApp("/escalations/t-9");

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Propose refund" }));
    const dialog = await screen.findByRole("dialog", { name: "Propose a refund?" });
    expect(dialog).toHaveTextContent("₹2,499.00");
    expect(dialog).toHaveTextContent("another reviewer must approve the proposal");
    await user.click(within(dialog).getByRole("button", { name: "Propose refund" }));

    await waitFor(() => {
      expect(calls.some((call) => call.method === "POST" && call.path === "/tickets/t-9/refund-proposals")).toBe(true);
    });
    // The new approval's case page (its GET is not routed here, so it shows an error state).
    await waitFor(() => {
      expect(calls.some((call) => call.path === "/approvals/a-9")).toBe(true);
    });
  });

  it("does not offer a refund proposal when the order is not eligible now", async () => {
    fakeApi({
      ...ROUTES,
      "GET /reviewer/escalations/t-9": () =>
        json({ ...CASE, facts: { ...CASE.facts, eligible_now: false, reasons_now: ["INSPECTION_FAILED"] } }),
    });
    logInAs(ANITA);
    renderApp("/escalations/t-9");

    expect(await screen.findByRole("button", { name: "Propose refund" })).toBeDisabled();
    expect(screen.getByText("Not eligible now. See Order facts for the reasons.")).toBeInTheDocument();
  });
});
