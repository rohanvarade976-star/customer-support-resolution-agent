// "Our assistant is working on it": shown while a run is in progress. Each check appears when
// its event arrives, with a spinner, and turns into a tick (with a short verified detail when
// the API has one) once done. The checks come from lib/runChecks.ts: events and fixed codes
// only, no model text (ADR-023). A pulsing dot and a sweeping bar show that it is alive.

import { Check } from "lucide-react";

import type { RunStatusEvent, RunStepEvent } from "../../api/types";
import { runChecks, type Check as CheckItem } from "../../lib/runChecks";
import { cn } from "../../lib/utils";
import { Card, CardContent } from "../ui/card";

function CheckRow({ check }: { check: CheckItem }) {
  const done = check.state === "done";
  return (
    <li className="animate-slide-up flex items-start gap-3" data-testid={`check-${check.key}`} data-state={check.state}>
      {/* Keyed by the state, so the tick pops in when the check finishes. */}
      <span
        key={check.state}
        className={cn(
          "mt-0.5 grid size-6 shrink-0 place-items-center rounded-full",
          done ? "animate-pop bg-success-600 text-white" : "border-2 border-primary-200 bg-white",
        )}
      >
        {done ? (
          <Check className="size-3.5" strokeWidth={3} aria-hidden="true" />
        ) : (
          <span className="size-2 animate-pulse rounded-full bg-primary-500" aria-hidden="true" />
        )}
      </span>
      <div className="min-w-0">
        <p className={cn("text-sm font-medium", done ? "text-gray-900" : "text-primary-800")}>
          {done ? check.doneLabel : `${check.label}…`}
        </p>
        {check.detail !== null && <p className="mt-0.5 text-xs text-gray-600">{check.detail}</p>}
      </div>
    </li>
  );
}

export function AssistantWorkCard({ status, steps }: { status: RunStatusEvent; steps: RunStepEvent[] }) {
  const checks = runChecks(status, steps);
  return (
    <Card data-testid="assistant-work" className="overflow-hidden">
      {/* An indeterminate bar: we cannot know how long the run takes, only that it is working. */}
      <div className="h-1 overflow-hidden bg-primary-100" aria-hidden="true">
        <div className="animate-progress h-full w-1/3 rounded-full bg-primary-500" />
      </div>
      <CardContent className="p-6">
        <h2 className="flex items-center gap-2 text-base font-semibold text-gray-900">
          <span className="relative flex size-2.5" aria-hidden="true">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-primary-400 opacity-75" />
            <span className="relative inline-flex size-2.5 rounded-full bg-primary-600" />
          </span>
          Our assistant is checking your request
        </h2>
        <ol aria-label="Checks" aria-live="polite" className="mt-5 space-y-4">
          {checks.map((check) => (
            <CheckRow key={check.key} check={check} />
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}
