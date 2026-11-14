// A package-tracking style stepper: Received → Investigating → Decision → Outcome.
// Vertical on phones, horizontal from the small breakpoint up. The stages come from
// lib/runStages.ts, a fixed lookup of the status the API sent.

import { Check, UserRound, X } from "lucide-react";

import type { RunStatus } from "../api/types";
import { runProgress, type Stage } from "../lib/runStages";
import { cn } from "../lib/utils";

const STATE_TEXT = { complete: "completed", current: "current step", upcoming: "not started" };

function StageMarker({ stage, index }: { stage: Stage; index: number }) {
  if (stage.state === "complete") {
    const colour =
      stage.tone === "warning" ? "bg-warning-500" : stage.tone === "danger" ? "bg-danger-600" : stage.tone === "success" ? "bg-success-600" : "bg-primary-600";
    const Icon = stage.tone === "warning" ? UserRound : stage.tone === "danger" ? X : Check;
    return (
      <span className={cn("grid size-8 shrink-0 place-items-center rounded-full text-white", colour)}>
        <Icon className="size-4" strokeWidth={2.5} aria-hidden="true" />
      </span>
    );
  }
  if (stage.state === "current") {
    const attention = stage.tone === "attention";
    return (
      <span
        className={cn(
          "grid size-8 shrink-0 place-items-center rounded-full border-2 bg-white",
          attention ? "border-warning-500" : "border-primary-600",
        )}
      >
        <span className={cn("size-2.5 animate-pulse rounded-full", attention ? "bg-warning-500" : "bg-primary-600")} />
      </span>
    );
  }
  return (
    <span className="grid size-8 shrink-0 place-items-center rounded-full border-2 border-gray-200 bg-white text-xs font-semibold text-gray-500">
      {index + 1}
    </span>
  );
}

export function RunProgress({ status }: { status: RunStatus }) {
  const { stages } = runProgress(status);
  return (
    <ol aria-label="Progress" className="flex flex-col sm:flex-row">
      {stages.map((stage, i) => {
        const last = i === stages.length - 1;
        const lineDone = stage.state === "complete";
        return (
          <li
            key={stage.key}
            aria-current={stage.state === "current" ? "step" : undefined}
            className="relative flex items-center gap-3 pb-5 last:pb-0 sm:flex-1 sm:flex-col sm:gap-2 sm:pb-0 sm:text-center"
          >
            {!last && (
              <span
                aria-hidden="true"
                className={cn(
                  "absolute top-8 bottom-0 left-[15px] w-0.5",
                  "sm:top-[15px] sm:right-[calc(-50%+1.25rem)] sm:bottom-auto sm:left-[calc(50%+1.25rem)] sm:h-0.5 sm:w-auto",
                  lineDone ? "bg-primary-600" : "bg-gray-200",
                )}
              />
            )}
            <StageMarker stage={stage} index={i} />
            <span
              className={cn(
                "text-sm font-medium",
                stage.state === "upcoming" ? "text-gray-500" : "text-gray-900",
              )}
            >
              {stage.label}
              <span className="sr-only"> ({STATE_TEXT[stage.state]})</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}
