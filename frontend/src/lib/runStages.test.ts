import { describe, expect, it } from "vitest";

import { runProgress } from "./runStages";

const states = (status: Parameters<typeof runProgress>[0]) => runProgress(status).stages.map((stage) => stage.state);

describe("runProgress", () => {
  it("marks earlier stages complete, the run's stage current, later ones upcoming", () => {
    expect(states("created")).toEqual(["current", "upcoming", "upcoming", "upcoming"]);
    expect(states("investigating")).toEqual(["complete", "current", "upcoming", "upcoming"]);
    expect(states("awaiting_approval")).toEqual(["complete", "complete", "current", "upcoming"]);
  });

  it("names the current stage after the run's status, and flags a wait on the customer", () => {
    const progress = runProgress("awaiting_customer");
    expect(progress.stages[2]).toMatchObject({ label: "Waiting for your reply", state: "current", tone: "attention" });
    expect(progress.headline).toBe("Waiting for your reply");
  });

  it("completes every stage of a finished run, with the outcome on the last one", () => {
    expect(states("completed")).toEqual(["complete", "complete", "complete", "complete"]);
    expect(runProgress("completed").stages[3]).toMatchObject({ label: "Completed", tone: "success" });
    expect(runProgress("escalated").stages[3]).toMatchObject({ label: "With a specialist", tone: "warning" });
    expect(runProgress("failed").stages[3]).toMatchObject({ label: "Failed", tone: "danger" });
  });
});
