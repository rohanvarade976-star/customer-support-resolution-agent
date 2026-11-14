import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { ApprovalCase } from "../api/types";
import { ANITA, fakeApi, json, logInAs, renderApp } from "../test/helpers";

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

    await waitFor(() => {
      expect(calls.find((call) => call.method === "POST")?.body).toEqual({
        decision: "approve",
        note: "Return verified",
      });
    });
    expect(await screen.findByText("The approval was recorded.")).toBeInTheDocument();
    expect(await screen.findByTestId("refund-status")).toHaveTextContent("Succeeded");
    expect(screen.getByTestId("refund-status")).toHaveTextContent("pay_ref_1");
    expect(screen.queryByRole("button", { name: "Approve refund" })).not.toBeInTheDocument();
  });

  it("sends a rejection without a note when none is typed", async () => {
    const calls = fakeApi({
      "GET /approvals/a-1": () => json(PENDING),
      "POST /approvals/a-1/decision": () =>
        json({ approval_id: "a-1", proposal_id: "p-1", decision: "rejected", proposal_status: "rejected", refund: null, message: "Rejected." }, 202),
    });
    logInAs(ANITA);
    renderApp("/approvals/a-1");
    await screen.findByTestId("amount");

    await userEvent.setup().click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(calls.find((call) => call.method === "POST")?.body).toEqual({ decision: "reject" });
    });
  });

  it("shows the API's refusal (already decided)", async () => {
    fakeApi({
      "GET /approvals/a-1": () => json(PENDING),
      "POST /approvals/a-1/decision": () =>
        json({ success: false, error_code: "ALREADY_DECIDED", message: "This approval has already been decided." }, 409),
    });
    logInAs(ANITA);
    renderApp("/approvals/a-1");
    await screen.findByTestId("amount");

    await userEvent.setup().click(screen.getByRole("button", { name: "Approve refund" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("This approval has already been decided.");
  });
});
