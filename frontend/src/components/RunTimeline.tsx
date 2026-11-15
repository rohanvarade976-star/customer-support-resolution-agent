// The run's activity, grouped into phases (Understanding, Checking, Deciding, Done), oldest
// first, in plain words for everyone: customers see a fixed sentence per event type, reviewers a
// neutral description of what happened and who did it (lib/labels.ts). Redundant entries are
// merged (lib/timeline.ts). Each shows a relative time and the exact time. The raw event codes,
// ids, hashes and timings are for reviewers only, and only inside the collapsed "Technical
// details" section (TechnicalDetails below, ADR-022).

import type { RunStepEvent } from "../api/types";
import { useNow } from "../hooks/useNow";
import { formatClock } from "../lib/dates";
import { formatDateTime, humanize } from "../lib/labels";
import { buildTimeline } from "../lib/timeline";
import { formatRelative } from "../lib/time";
import { cn } from "../lib/utils";

export function RunTimeline({ steps, reviewer }: { steps: RunStepEvent[]; reviewer: boolean }) {
  const now = useNow(30_000);
  if (steps.length === 0) {
    return <p className="text-sm text-gray-500">Nothing has happened yet. Updates will appear here.</p>;
  }
  const phases = buildTimeline(steps, reviewer);
  const newestId = phases.at(-1)?.entries.at(-1)?.id;
  return (
    <ol aria-label="Activity" className="space-y-6">
      {phases.map((phase, p) => (
        <li key={`${phase.key}-${String(p)}`}>
          <h3 className="mb-3 text-xs font-semibold tracking-wide text-gray-600 uppercase">{phase.title}</h3>
          <ol>
            {phase.entries.map((entry, i) => {
              const last = i === phase.entries.length - 1;
              const newest = entry.id === newestId;
              return (
                <li key={entry.id} className="animate-slide-up relative flex gap-4 pb-5 last:pb-0">
                  {!last && <span aria-hidden="true" className="absolute top-5 -bottom-0.5 left-[5px] w-px bg-gray-200" />}
                  <span
                    aria-hidden="true"
                    className={cn(
                      "mt-1.5 size-[11px] shrink-0 rounded-full ring-4",
                      newest ? "bg-primary-600 ring-primary-100" : "bg-gray-300 ring-white",
                    )}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-gray-900">
                      {entry.label}
                      {entry.count > 1 && <span className="ml-1.5 text-xs font-normal text-gray-500">×{entry.count}</span>}
                    </p>
                    <p className="mt-0.5 text-xs text-gray-500">
                      <time dateTime={entry.at} title={formatDateTime(entry.at)}>
                        {formatRelative(entry.at, now)} · {formatClock(entry.at)}
                      </time>
                      {reviewer && <> · {humanize(entry.actor)}</>}
                    </p>
                  </div>
                </li>
              );
            })}
          </ol>
        </li>
      ))}
    </ol>
  );
}

// Reviewers: every audit event as recorded, oldest first, with its allowlisted payload fields
// (ids, codes, counts, timings). Shown inside a collapsed section on the run page.
export function TechnicalDetails({ steps }: { steps: RunStepEvent[] }) {
  if (steps.length === 0) return <p className="text-sm text-gray-500">No events recorded yet.</p>;
  return (
    <ol aria-label="Audit events" className="space-y-3">
      {steps.map((step) => {
        const details = Object.entries(step.details);
        return (
          <li key={step.id} className="rounded-lg border border-gray-100 px-3 py-2.5">
            <p className="flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5">
              <code className="font-mono text-xs font-medium break-all text-gray-900">{step.type}</code>
              <span className="font-mono text-xs text-gray-500">
                {step.actor} · {step.at}
              </span>
            </p>
            {details.length > 0 && (
              <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 rounded-md bg-gray-50 px-3 py-2 text-xs">
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
          </li>
        );
      })}
    </ol>
  );
}
