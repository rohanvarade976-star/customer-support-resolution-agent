import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { RunSnapshot, RunStatusEvent, RunStepEvent } from "../api/types";
import { ANITA, fakeApi, json, logInAs, PRIYA, renderApp, sseFrame, sseResponse } from "../test/helpers";

const STATUS: RunStatusEvent = {
  run_id: "run-1",
  ticket_id: "t-1",
  status: "awaiting_customer",
  ticket_status: "awaiting_customer",
  reply: "Which order is this about?",
  finished: false,
};

const STEP: RunStepEvent = {
  id: "e-1",
  type: "clarification.requested",
  actor: "system",
  at: "2026-11-10T12:00:00Z",
  details: {},
};

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
    expect(screen.getByText("We need one more detail from you")).toBeInTheDocument();
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
    expect(screen.getByText("Your refund is complete", { selector: "li" })).toBeInTheDocument();
    expect(await screen.findByText("Up to date")).toBeInTheDocument();
    expect(calls.some((call) => call.path === "/runs/run-1/events")).toBe(true);
    expect(screen.queryByLabelText("Your reply")).not.toBeInTheDocument();
  });

  it("does not offer reviewers the customer's reply box", async () => {
    fakeApi({ "GET /runs/run-1/stream": () => sseResponse([sseFrame("status", STATUS)], true) });
    logInAs(ANITA);
    renderApp("/runs/run-1");

    expect(await screen.findByTestId("verified-reply")).toBeInTheDocument();
    expect(screen.queryByLabelText("Your reply")).not.toBeInTheDocument();
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
