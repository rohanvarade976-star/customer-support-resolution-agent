// The run's detailed timeline, newest first. Customers see a fixed sentence per event type
// (lib/labels.ts); reviewers see the event codes and the allowlisted details (ADR-019).

import type { RunStepEvent } from "../api/types";
import { eventLabel, formatDateTime } from "../lib/labels";
import { formatRelative } from "../lib/time";
import { cn } from "../lib/utils";

export function RunTimeline({ steps, reviewer }: { steps: RunStepEvent[]; reviewer: boolean }) {
  if (steps.length === 0) {
    return <p className="text-sm text-gray-500">Nothing has happened yet. Updates will appear here.</p>;
  }
  const newestFirst = [...steps].reverse();
  return (
    <ol aria-label="Activity">
      {newestFirst.map((step, i) => {
        const last = i === newestFirst.length - 1;
        const details = Object.entries(step.details);
        return (
          <li key={step.id} className="relative flex gap-4 pb-6 last:pb-0">
            {!last && <span aria-hidden="true" className="absolute top-5 -bottom-0.5 left-[5px] w-px bg-gray-200" />}
            <span
              aria-hidden="true"
              className={cn(
                "mt-1.5 size-[11px] shrink-0 rounded-full ring-4",
                i === 0 ? "bg-primary-600 ring-primary-100" : "bg-gray-300 ring-white",
              )}
            />
            <div className="min-w-0 flex-1">
              {reviewer ? (
                <p className="flex flex-wrap items-center gap-2">
                  <code className="font-mono text-sm font-medium text-gray-900">{step.type}</code>
                  <span className="text-xs text-gray-500">by {step.actor}</span>
                </p>
              ) : (
                <p className="text-sm font-medium text-gray-900">{eventLabel(step.type)}</p>
              )}
              <p className="mt-0.5 text-xs text-gray-500">
                <time dateTime={step.at} title={formatDateTime(step.at)}>
                  {formatRelative(step.at)}
                </time>
              </p>
              {reviewer && details.length > 0 && (
                <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 rounded-lg bg-gray-50 px-3 py-2 text-xs">
                  {details.map(([key, value]) => (
                    <div key={key} className="contents">
                      <dt className="font-mono text-gray-500">{key}</dt>
                      <dd className="font-mono break-all text-gray-800">
                        {Array.isArray(value) ? value.join(", ") : String(value)}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
