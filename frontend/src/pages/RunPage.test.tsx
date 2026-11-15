import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunSnapshot, RunStatusEvent, RunStepEvent } from "../api/types";
import { ANALYSIS, RATIONALE } from "../test/fixtures";
import { ANITA, fakeApi, json, logInAs, PRIYA, renderApp, sseFrame, sseResponse } from "../test/helpers";

const STATUS: RunStatusEvent = {
  run_id: "run-1",
  ticket_id: "t-1",
  status: "awaiting_customer",
  ticket_status: "awaiting_customer",
  reply: "Which order is this about?",
  reply_from: "assistant",
  finished: false,
  checked: ["order", "shipment", "policy"],
};

const STEP: RunStepEvent = {
  id: "e-1",
  type: "clarification.requested",
  actor: "system",
  at: "2026-11-10T12:00:00Z",
  details: {},
};

afterEach(() => {
  vi.useRealTimers(); // in case a test with fake timers failed before restoring them
});

describe("RunPage", () => {
  it("shows the streamed status, the verified reply and the timeline, and sends a reply", async () => {
    const calls = fakeApi({
      // A live stream: snapshot sent, connection stays open.
      "GET /runs/run-1/stream": () => sseResponse([sseFrame("status", STATUS), sseFrame("step", STEP)], true),
      "POST /runs/run-1/messages": () => json({ run_id: "run-1", status: "investigating" }, 202),
      "GET /runs/run-1/events": () =>
        json({ run: { ...STATUS, status: "investigating", reply: null }, steps: [STEP] } satisfies RunSnapshot),
    });
    logInAs(PRIYA);
    renderApp("/runs/run-1");

    expect(await screen.findByTestId("verified-reply")).toHaveTextContent("Which order is this about?");
    const activity = screen.getByRole("list", { name: "Activity" });
    expect(within(activity).getByText("We need one more detail from you")).toBeInTheDocument();
    // The stepper marks the decision stage as current: it is waiting on the customer.
    const progress = screen.getByRole("list", { name: "Progress" });
    expect(within(progress).getByText("Waiting for your reply").closest("li")).toHaveAttribute("aria-current", "step");
    expect(screen.getByText("Live")).toBeInTheDocument();
    // The stream request carries the token in the header, never in the URL.
    const stream = calls.find((call) => call.path === "/runs/run-1/stream");
    expect(stream?.authorization).toBe("Bearer token-c-1");

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Your reply"), "ORD-1001");
    await user.click(screen.getByRole("button", { name: "Send reply" }));

    await waitFor(() => {
      expect(calls.find((call) => call.method === "POST")?.body).toEqual({ text: "ORD-1001" });
    });
    // After sending, the fresh status no longer waits for the customer: the box goes away.
    await waitFor(() => {
      expect(screen.queryByLabelText("Your reply")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Reply sent")).toBeInTheDocument(); // the confirmation toast
  });

  it("falls back to polling when the stream cannot be opened", async () => {
    const finished: RunSnapshot = {
      run: { ...STATUS, status: "completed", ticket_status: "resolved", reply: "Your refund is complete.", finished: true },
      steps: [{ ...STEP, id: "e-9", type: "refund.succeeded" }],
    };
    const calls = fakeApi({
      "GET /runs/run-1/stream": () => {
        throw new TypeError("network down");
      },
      "GET /runs/run-1/events": () => json(finished),
    });
    logInAs(PRIYA);
    renderApp("/runs/run-1");

    expect(await screen.findByTestId("verified-reply")).toHaveTextContent("Your refund is complete.");
    expect(within(screen.getByRole("list", { name: "Activity" })).getByText("Your refund is complete")).toBeInTheDocument();
    expect(await screen.findByText("Up to date")).toBeInTheDocument();
    expect(calls.some((call) => call.path === "/runs/run-1/events")).toBe(true);
    expect(screen.queryByLabelText("Your reply")).not.toBeInTheDocument();
  });

  it("does not offer reviewers the customer's reply box, and shows them the AI analysis", async () => {
    fakeApi({
      "GET /runs/run-1/stream": () => sseResponse([sseFrame("status", STATUS)], true),
      "GET /runs/run-1/analysis": () => json(ANALYSIS),
    });
    logInAs(ANITA);
    renderApp("/runs/run-1");

    expect(await screen.findByTestId("verified-reply")).toBeInTheDocument();
    expect(screen.queryByLabelText("Your reply")).not.toBeInTheDocument();
    expect(await screen.findByTestId("ai-rationale")).toHaveTextContent(RATIONALE);
    expect(screen.queryByTestId("what-we-checked")).not.toBeInTheDocument();
    // The analysis sections start collapsed.
    expect(screen.getByTestId("section-ai-analysis")).not.toHaveAttribute("open");
  });

  it("shows reviewers a readable timeline and keeps raw payloads under collapsed technical details", async () => {
    const step: RunStepEvent = {
      id: "e-2",
      type: "proposal.created",
      actor: "system",
      at: "2026-11-10T12:00:00Z",
      details: { payload_hash: "deadbeefcafe", duration_ms: 812 },
    };
    fakeApi({
      "GET /runs/run-1/stream": () => sseResponse([sseFrame("status", STATUS), sseFrame("step", step)], true),
      "GET /runs/run-1/analysis": () => json(ANALYSIS),
    });
    logInAs(ANITA);
    renderApp("/runs/run-1");

    const activity = await screen.findByRole("list", { name: "Activity" });
    expect(await within(activity).findByText("Refund proposal stored")).toBeInTheDocument();
    expect(within(activity).queryByText("proposal.created")).not.toBeInTheDocument();
    expect(within(activity).queryByText("deadbeefcafe")).not.toBeInTheDocument();

    const technical = screen.getByTestId("section-technical");
    expect(technical).not.toHaveAttribute("open");
    expect(within(technical).getByText("deadbeefcafe")).toBeInTheDocument();
    await userEvent.setup().click(within(technical).getByText("Technical details"));
    expect(technical).toHaveAttribute("open");
  });

  it("tells the customer a specialist has an escalated ticket, then shows the specialist's reply", async () => {
    const escalated: RunStatusEvent = {
      ...STATUS,
      status: "escalated",
      ticket_status: "escalated",
      reply: "Your case needs a closer look from a member of our support team.",
      finished: true,
    };
    // Fake timers that still let real time pass, so the 30-second check can be skipped ahead.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let replied = false;
    fakeApi({
      "GET /runs/run-1/stream": () => sseResponse([sseFrame("status", escalated), sseFrame("end", { reason: "finished" })]),
      "GET /runs/run-1/events": () =>
        json({
          run: replied ? { ...escalated, reply: "We checked with the warehouse.", reply_from: "specialist" } : escalated,
          steps: [],
        } satisfies RunSnapshot),
    });
    logInAs(PRIYA);
    renderApp("/runs/run-1");

    const card = await screen.findByTestId("specialist-card");
    expect(card).toHaveTextContent("A specialist is looking at this");
    expect(card).toHaveTextContent("Their reply will appear on this page");
    // The assistant's escalation message is not repeated under the card.
    expect(screen.queryByTestId("verified-reply")).not.toBeInTheDocument();

    replied = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(31_000);
    });
    expect(await screen.findByTestId("specialist-reply")).toHaveTextContent("We checked with the warehouse.");
    expect(screen.getByText("Reply from a support specialist")).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("shows customers what was checked in plain words, with no AI text or internal codes", async () => {
    const calls = fakeApi({
      "GET /runs/run-1/stream": () =>
        sseResponse(
          [
            sseFrame("status", { ...STATUS, checked: ["order", "shipment", "policy", "internal_code"] }),
            sseFrame("step", { ...STEP, type: "investigation.started" }),
          ],
          true,
        ),
    });
    logInAs(PRIYA);
    renderApp("/runs/run-1");

    const card = await screen.findByTestId("what-we-checked");
    expect(within(card).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Your order details",
      "Delivery tracking from the carrier",
      "Our refund and delivery policies",
    ]);
    expect(card).not.toHaveTextContent("internal_code");
    // The customer never asks for the analysis, and nothing AI-labelled is shown.
    expect(calls.some((call) => call.path.endsWith("/analysis"))).toBe(false);
    expect(screen.queryByText(/AI-generated/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("ai-recommendation")).not.toBeInTheDocument();
    // The assistant is named in the customer's timeline.
    expect(screen.getByText("Our assistant is checking your order details")).toBeInTheDocument();
  });

  it("stops with the API's message when the run is not yours", async () => {
    fakeApi({
      "GET /runs/run-1/stream": () => json({ success: false, error_code: "RUN_NOT_FOUND", message: "Run not found." }, 404),
    });
    logInAs(PRIYA);
    renderApp("/runs/run-1");

    expect(await screen.findByRole("alert")).toHaveTextContent("Run not found.");
  });
});
