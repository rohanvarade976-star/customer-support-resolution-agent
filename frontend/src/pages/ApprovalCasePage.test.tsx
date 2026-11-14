import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { ApprovalCase } from "../api/types";
import { ANALYSIS, RATIONALE } from "../test/fixtures";
import { ANITA, fakeApi, json, logInAs, renderApp } from "../test/helpers";

const ANALYSIS_ROUTE = { "GET /runs/run-1/analysis": () => json(ANALYSIS) };

const PENDING: ApprovalCase = {
  approval_id: "a-1",
  proposal_id: "p-1",
  run_id: "run-1",
  ticket_id: "t-1",
  approval_status: "pending",
  created_at: "2026-11-10T12:00:00Z",
  expires_at: "2026-11-10T13:00:00Z",
  decided_at: null,
  customer_name: "Priya Sharma",
  complaint: "I returned my headphones, please refund",
  ticket_status: "in_progress",
  proposal: {
    order_number: "ORD-1001",
    amount_minor: 249900,
    currency: "INR",
    reason: "Return received and inspection passed",
    checks_passed: ["RETURN_RECEIVED", "INSPECTION_PASSED"],
    payload_hash: "abc",
    status: "pending_approval",
  },
  facts: null,
  governing_policy: { policy_key: "refund_policy", version: "v2", title: "Refund Policy", refund_window_days: 30 },
  citations: [
    {
      citation: "refund_policy_v2:section_3",
      policy_title: "Refund Policy",
      version: "v2",
      section_title: "Return window",
      text: "Returns are accepted within 30 days.",
    },
  ],
  refund: null,
};

const APPROVED: ApprovalCase = {
  ...PENDING,
  approval_status: "approved",
  decided_at: "2026-11-10T12:05:00Z",
  refund: { refund_id: "r-1", status: "succeeded", amount_minor: 249900, provider_reference: "pay_ref_1" },
};

describe("ApprovalCasePage", () => {
  it("shows the stored amount and citations, sends the decision with the note, then the refund status", async () => {
    let decided = false;
    const calls = fakeApi({
      ...ANALYSIS_ROUTE,
      "GET /approvals/a-1": () => json(decided ? APPROVED : PENDING),
      "POST /approvals/a-1/decision": () => {
        decided = true;
        return json(
          {
            approval_id: "a-1",
            proposal_id: "p-1",
            decision: "approved",
            proposal_status: "approved",
            refund: null,
            message: "The approval was recorded.",
          },
          202,
        );
      },
    });
    logInAs(ANITA);
    renderApp("/approvals/a-1");

    // Exactly the API's amount, formatted from integer paise.
    expect(await screen.findByTestId("amount")).toHaveTextContent("₹2,499.00");
    expect(screen.getByText(/Return window/)).toBeInTheDocument();
    expect(screen.getByText("Returns are accepted within 30 days.")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/Note/), "Return verified");
    await user.click(screen.getByRole("button", { name: "Approve refund" }));

    // Nothing is sent until the reviewer confirms; the dialog states the exact amount.
    const dialog = await screen.findByRole("dialog", { name: "Approve this refund?" });
    expect(dialog).toHaveTextContent("₹2,499.00 will be refunded to Priya Sharma for ORD-1001");
    expect(calls.some((call) => call.method === "POST")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Approve ₹2,499.00" }));

    await waitFor(() => {
      expect(calls.find((call) => call.method === "POST")?.body).toEqual({
        decision: "approve",
        note: "Return verified",
      });
    });
    const panel = screen.getByRole("complementary", { name: "Decision" });
    expect(await within(panel).findByText("The approval was recorded.")).toBeInTheDocument();
    expect(await screen.findByTestId("refund-status")).toHaveTextContent("Succeeded");
    expect(screen.getByTestId("refund-status")).toHaveTextContent("pay_ref_1");
    expect(screen.queryByRole("button", { name: "Approve refund" })).not.toBeInTheDocument();
  });

  it("sends a rejection without a note when none is typed", async () => {
    const calls = fakeApi({
      ...ANALYSIS_ROUTE,
      "GET /approvals/a-1": () => json(PENDING),
      "POST /approvals/a-1/decision": () =>
        json({ approval_id: "a-1", proposal_id: "p-1", decision: "rejected", proposal_status: "rejected", refund: null, message: "Rejected." }, 202),
    });
    logInAs(ANITA);
    renderApp("/approvals/a-1");
    await screen.findByTestId("amount");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Reject" }));
    const dialog = await screen.findByRole("dialog", { name: "Reject this refund?" });
    expect(dialog).toHaveTextContent("₹2,499.00");
    await user.click(within(dialog).getByRole("button", { name: "Reject refund" }));

    await waitFor(() => {
      expect(calls.find((call) => call.method === "POST")?.body).toEqual({ decision: "reject" });
    });
  });

  it("sends nothing when the reviewer cancels the confirmation", async () => {
    const calls = fakeApi({ ...ANALYSIS_ROUTE, "GET /approvals/a-1": () => json(PENDING) });
    logInAs(ANITA);
    renderApp("/approvals/a-1");
    await screen.findByTestId("amount");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Approve refund" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(calls.some((call) => call.method === "POST")).toBe(false);
    expect(screen.getByRole("button", { name: "Approve refund" })).toBeEnabled();
  });

  it("shows the API's refusal (already decided)", async () => {
    fakeApi({
      ...ANALYSIS_ROUTE,
      "GET /approvals/a-1": () => json(PENDING),
      "POST /approvals/a-1/decision": () =>
        json({ success: false, error_code: "ALREADY_DECIDED", message: "This approval has already been decided." }, 409),
    });
    logInAs(ANITA);
    renderApp("/approvals/a-1");
    await screen.findByTestId("amount");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Approve refund" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Approve ₹2,499.00" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("This approval has already been decided.");
  });

  it("shows the AI analysis apart from the verified records, with the rationale as plain text", async () => {
    fakeApi({ ...ANALYSIS_ROUTE, "GET /approvals/a-1": () => json(PENDING) });
    logInAs(ANITA);
    renderApp("/approvals/a-1");

    // The AI's recommendation: labelled, in its own card, and its text is never parsed as HTML.
    const card = await screen.findByTestId("ai-recommendation");
    expect(within(card).getByText("AI-generated · not verified")).toBeInTheDocument();
    expect(within(card).getByText("Refund")).toBeInTheDocument();
    const rationale = within(card).getByTestId("ai-rationale");
    expect(rationale.textContent).toBe(RATIONALE);
    expect(rationale.querySelector("b")).toBeNull();
    // Verified cards keep their label; the AI card never carries it.
    expect(screen.getAllByText("Verified from records").length).toBeGreaterThanOrEqual(2);
    expect(within(card).queryByText("Verified from records")).not.toBeInTheDocument();

    // Computed by code: risk flags, agents in order, quality checks and tools.
    const flags = screen.getByTestId("risk-flags");
    expect(within(flags).getByText("Delivery dates disagree")).toBeInTheDocument();
    expect(within(flags).getByText("Instruction-like text")).toBeInTheDocument();
    const agents = within(screen.getByRole("list", { name: "Agent activity" })).getAllByRole("listitem");
    expect(agents.map((item) => item.querySelector("span.font-medium")?.textContent)).toEqual([
      "Classifier",
      "Supervisor",
      "Investigator",
      "Policy agent",
      "Resolution agent",
      "Critic",
    ]);
    expect(screen.getByText("4 tools called, 3 facts grounded, 1 added by code")).toBeInTheDocument();
    expect(screen.getByText("3 policy sections cited, 1 dropped")).toBeInTheDocument();
    expect(screen.getByText("Proposed: Refund, 1 revision")).toBeInTheDocument();
    expect(screen.getByText("Critic approved")).toBeInTheDocument();
    const quality = screen.getByTestId("quality-checks");
    expect(within(quality).getByText("3 / 1")).toBeInTheDocument(); // citations verified / dropped
    const tools = screen.getByRole("list", { name: "Tools used" });
    expect(within(tools).getByText("Shipment tracking").closest("li")).toHaveTextContent("by code");
  });

  it("says when code rejected the AI's draft, with no rationale", async () => {
    const rejected = {
      ...ANALYSIS,
      recommendation: {
        ...ANALYSIS.recommendation,
        action: "escalate",
        accepted: false,
        rationale: null,
        reason_codes: ["EVIDENCE_CONFLICT"],
      },
    };
    fakeApi({ "GET /runs/run-1/analysis": () => json(rejected), "GET /approvals/a-1": () => json(PENDING) });
    logInAs(ANITA);
    renderApp("/approvals/a-1");

    const card = await screen.findByTestId("ai-recommendation");
    expect(card).toHaveTextContent("No AI draft was accepted");
    expect(within(card).queryByTestId("ai-rationale")).not.toBeInTheDocument();
  });
});
