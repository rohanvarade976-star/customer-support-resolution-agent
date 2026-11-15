import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Ticket } from "../api/types";
import { fakeApi, json, logInAs, PRIYA, renderApp } from "../test/helpers";

function ticket(id: string, status: Ticket["status"], runStatus: Ticket["latest_run_status"]): Ticket {
  return {
    id,
    order_number: "ORD-1001",
    category: "refund_request",
    status,
    summary: `Ticket ${id}`,
    created_at: "2026-11-10T12:00:00Z",
    latest_run_id: runStatus === null ? null : `run-${id}`,
    latest_run_status: runStatus,
    reply: null,
    reply_from: "assistant",
  };
}

describe("HomePage", () => {
  it("counts the tickets by the status the API returned and offers the next action", async () => {
    fakeApi({
      "GET /tickets?limit=50": () =>
        json({
          items: [
            ticket("a", "awaiting_customer", "awaiting_customer"),
            ticket("b", "in_progress", "awaiting_approval"),
            ticket("c", "resolved", "completed"),
          ],
          limit: 50,
          offset: 0,
        }),
      "GET /me/orders": () => json([]),
    });
    logInAs(PRIYA);
    renderApp("/home");

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(await within(summary).findByText("Open requests")).toBeInTheDocument();
    const value = (label: string) => within(summary).getByText(label).nextElementSibling?.textContent;
    expect(value("Open requests")).toBe("2");
    expect(value("Awaiting your reply")).toBe("1");
    expect(value("Resolved")).toBe("1");

    expect(screen.getByRole("link", { name: "Reply now" })).toHaveAttribute("href", "/runs/run-a");
    expect(screen.getByText("No orders yet")).toBeInTheDocument();
  });

  it("shows an empty state with a next step when there are no tickets", async () => {
    fakeApi({
      "GET /tickets?limit=50": () => json({ items: [], limit: 50, offset: 0 }),
      "GET /me/orders": () => json([]),
    });
    logInAs(PRIYA);
    renderApp("/home");

    expect(await screen.findByText("No requests yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start a request" })).toHaveAttribute("href", "/complaints/new");
  });

  it("shows the error with a retry button", async () => {
    fakeApi({
      "GET /tickets?limit=50": () => json({ success: false, error_code: "X", message: "Service unavailable." }, 503),
      "GET /me/orders": () => json([]),
    });
    logInAs(PRIYA);
    renderApp("/home");

    expect(await screen.findByRole("alert")).toHaveTextContent("Service unavailable.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
