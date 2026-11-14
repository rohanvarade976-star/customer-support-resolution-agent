// A run's live status (FR-22, ADR-019).
//
// 1. Open GET /runs/{id}/stream with fetch (token in the Authorization header).
// 2. Each "status" event replaces the status; each "step" event is added (de-duplicated by id,
//    because every new stream starts with the full snapshot again).
// 3. "end: finished" -> done. "end: timeout" (the server's stream limit) -> reconnect at once.
// 4. If the stream fails or drops: poll GET /runs/{id}/events every `pollMs`, and try the
//    stream again after a growing delay. A 401/403/404 is final: stop and show the error.

import { useCallback, useEffect, useState } from "react";

import { ApiError, describeError, type ApiClient } from "../api/client";
import { readSse } from "../api/sse";
import type { RunSnapshot, RunStatusEvent, RunStepEvent } from "../api/types";

export type StreamMode = "connecting" | "live" | "polling" | "done";

export interface RunStreamState {
  status: RunStatusEvent | null;
  steps: RunStepEvent[];
  mode: StreamMode;
  error: string | null;
}

export interface RunStreamOptions {
  pollMs?: number; // polling interval while the stream is down
  maxRetryMs?: number; // longest wait before trying the stream again
}

const FATAL_STATUSES = new Set([401, 403, 404]);

function mergeSteps(current: RunStepEvent[], incoming: RunStepEvent[]): RunStepEvent[] {
  const byId = new Map(current.map((step) => [step.id, step]));
  for (const step of incoming) byId.set(step.id, step);
  return [...byId.values()].sort((a, b) => a.at.localeCompare(b.at));
}

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });

export function useRunStream(
  api: ApiClient,
  runId: string,
  { pollMs = 3000, maxRetryMs = 30000 }: RunStreamOptions = {},
): RunStreamState & { refresh: () => Promise<void> } {
  const [state, setState] = useState<RunStreamState>({
    status: null,
    steps: [],
    mode: "connecting",
    error: null,
  });

  const applySnapshot = useCallback((snapshot: RunSnapshot) => {
    setState((s) => ({ ...s, status: snapshot.run, steps: mergeSteps(s.steps, snapshot.steps) }));
  }, []);

  // One fetch of the snapshot, for the polling fallback and after the user acts.
  const refresh = useCallback(async () => {
    applySnapshot(await api.request<RunSnapshot>(`/runs/${runId}/events`));
  }, [api, runId, applySnapshot]);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    // A function, not the property: TypeScript would otherwise assume it never changes.
    const aborted = () => signal.aborted;

    // Resolves "finished" or "timeout" from the server's end event; throws if the stream
    // breaks before one arrives.
    async function streamOnce(): Promise<"finished" | "timeout"> {
      const response = await api.open(`/runs/${runId}/stream`, signal);
      setState((s) => ({ ...s, mode: "live", error: null }));
      // Set inside the callback; an object so TypeScript does not assume it stays null.
      const end: { reason: "finished" | "timeout" | null } = { reason: null };
      await readSse(response, (message) => {
        if (message.event === "status") {
          const status = JSON.parse(message.data) as RunStatusEvent;
          setState((s) => ({ ...s, status }));
        } else if (message.event === "step") {
          const step = JSON.parse(message.data) as RunStepEvent;
          setState((s) => ({ ...s, steps: mergeSteps(s.steps, [step]) }));
        } else if (message.event === "end") {
          const { reason } = JSON.parse(message.data) as { reason: string };
          end.reason = reason === "finished" ? "finished" : "timeout";
        }
      });
      if (end.reason === null) throw new Error("The live connection closed unexpectedly.");
      return end.reason;
    }

    // Polls until `ms` has passed; true if the run finished meanwhile.
    async function pollFor(ms: number): Promise<boolean> {
      const until = Date.now() + ms;
      do {
        try {
          const snapshot = await api.request<RunSnapshot>(`/runs/${runId}/events`, { signal });
          applySnapshot(snapshot);
          if (snapshot.run.finished) return true;
        } catch (error) {
          if (aborted() || isFatal(error)) throw error;
        }
        await sleep(pollMs, signal);
      } while (!aborted() && Date.now() < until);
      return false;
    }

    async function run(): Promise<void> {
      let failures = 0;
      while (!aborted()) {
        try {
          if ((await streamOnce()) === "finished") break;
          failures = 0; // a normal timeout: reconnect at once
        } catch (error) {
          if (aborted()) return;
          if (isFatal(error)) throw error;
          failures += 1;
          setState((s) => ({ ...s, mode: "polling" }));
          const retryIn = Math.min(maxRetryMs, pollMs * 2 ** (failures - 1));
          if (await pollFor(retryIn)) break;
        }
      }
      if (!aborted()) setState((s) => ({ ...s, mode: "done" }));
    }

    run().catch((error: unknown) => {
      if (!aborted()) setState((s) => ({ ...s, mode: "done", error: describeError(error) }));
    });
    return () => {
      controller.abort();
    };
  }, [api, runId, pollMs, maxRetryMs, applySnapshot]);

  return { ...state, refresh };
}

function isFatal(error: unknown): boolean {
  return error instanceof ApiError && FATAL_STATUSES.has(error.status);
}
