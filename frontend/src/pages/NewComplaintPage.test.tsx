import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Order } from "../api/types";
import { fakeApi, json, logInAs, PRIYA, renderApp, sseResponse } from "../test/helpers";

const ORDERS: Order[] = [
  {
    order_number: "ORD-1001",
    status: "delivered",
    currency: "INR",
    paid_amount_minor: 249900,
    placed_at: "2026-11-01T10:00:00Z",
    delivered_at: "2026-11-03T10:00:00Z",
    items: [{ product_id: "p-1", product_name: "Wireless Headphones", quantity: 1, paid_amount_minor: 249900 }],
    returns: [],
  },
];

describe("NewComplaintPage (wizard)", () => {
  it("validates each step, shows a review, then creates the ticket and starts its run", async () => {
    const calls = fakeApi({
      "GET /me/orders": () => json(ORDERS),
      "POST /tickets": () =>
        json(
          {
            id: "t-9",
            order_number: "ORD-1001",
            category: null,
            status: "open",
            summary: "x",
            created_at: "2026-11-10T12:00:00Z",
            latest_run_id: null,
            latest_run_status: null,
          },
          201,
        ),
      "POST /tickets/t-9/runs": () => json({ run_id: "run-9", ticket_id: "t-9", status: "created" }, 202),
      "GET /runs/run-9/stream": () => sseResponse([], true),
    });
    logInAs(PRIYA);
    renderApp("/complaints/new");
    const user = userEvent.setup();

    // Step 1: continuing without a choice shows an inline error.
    await screen.findByRole("radio", { name: /ORD-1001/ });
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByText(/Choose an order/)).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /ORD-1001/ }));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    // Step 2: the choice starts the description with an editable sentence.
    await user.click(screen.getByRole("radio", { name: /Refund not received/ }));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    // Step 3: an empty description is refused.
    const message = screen.getByLabelText("Your message");
    expect(message).toHaveValue("I returned my item but haven't received my refund yet.");
    await user.clear(message);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByText(/Please describe what happened/)).toBeInTheDocument();
    expect(message).toHaveAttribute("aria-invalid", "true");
    await user.type(message, "  My headphones refund is missing.  ");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    // Review: exactly what will be sent.
    expect(screen.getByRole("heading", { name: "Review your request" })).toBeInTheDocument();
    expect(screen.getByText("My headphones refund is missing.")).toBeInTheDocument();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Submit request" }));

    await waitFor(() => {
      expect(calls.filter((call) => call.method === "POST").map((call) => [call.path, call.body])).toEqual([
        ["/tickets", { message: "My headphones refund is missing.", order_number: "ORD-1001" }],
        ["/tickets/t-9/runs", null],
      ]);
    });
    expect(await screen.findByRole("heading", { name: "Your request" })).toBeInTheDocument();
  });

  it("preselects the order from the link and sends no order number for a general question", async () => {
    const calls = fakeApi({
      "GET /me/orders": () => json(ORDERS),
      "POST /tickets": () => json({ success: false, error_code: "RATE_LIMITED", message: "Too many requests." }, 429),
    });
    logInAs(PRIYA);
    renderApp("/complaints/new?order=ORD-1001");
    const user = userEvent.setup();

    expect(await screen.findByRole("radio", { name: /ORD-1001/ })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: /Not about a specific order/ }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("radio", { name: /Something else/ }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.type(screen.getByLabelText("Your message"), "How long do refunds take?");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Submit request" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Too many requests.");
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ message: "How long do refunds take?" });
    expect(screen.getByRole("button", { name: "Submit request" })).toBeEnabled();
  });

  it("preselects the order and the problem from a run's report-a-problem link", async () => {
    fakeApi({ "GET /me/orders": () => json(ORDERS) });
    logInAs(PRIYA);
    renderApp("/complaints/new?order=ORD-1001&issue=damaged");
    const user = userEvent.setup();

    expect(await screen.findByRole("radio", { name: /ORD-1001/ })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByRole("radio", { name: /Damaged or wrong item/ })).toBeChecked();
  });
});
