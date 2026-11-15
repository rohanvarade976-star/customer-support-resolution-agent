// Reviewers: what the system did on its own in the last 7 days (GET /reviewer/automation-summary,
// ADR-021). Counts computed by the backend; this only displays them. A failed load hides the
// cards (the queue below still works) rather than blocking the reviewer's work.

import { CircleCheck, Hourglass, ShieldCheck, UserRound, type LucideIcon } from "lucide-react";

import type { AutomationSummary } from "../../api/types";
import { useApiGet } from "../../hooks/useApiGet";
import { formatDuration } from "../../lib/analysis";
import { cn } from "../../lib/utils";
import { Card } from "../ui/card";
import { Skeleton } from "../ui/skeleton";

const ICON_TONE = {
  success: "bg-success-50 text-success-600",
  primary: "bg-primary-50 text-primary-600",
  warning: "bg-warning-50 text-warning-600",
  neutral: "bg-gray-100 text-gray-600",
};

interface StatProps {
  label: string;
  value: string;
  hint: string;
  icon: LucideIcon;
  tone: keyof typeof ICON_TONE;
}

function Stat({ label, value, hint, icon: Icon, tone }: StatProps) {
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-gray-600">{label}</p>
          <p className="mt-1.5 text-2xl font-semibold tracking-tight text-gray-900 tabular-nums">{value}</p>
        </div>
        <span className={cn("grid size-9 shrink-0 place-items-center rounded-lg", ICON_TONE[tone])}>
          <Icon className="size-4.5" aria-hidden="true" />
        </span>
      </div>
      <p className="mt-2 text-xs text-gray-500">{hint}</p>
    </Card>
  );
}

export function AutomationSummaryCards() {
  const { data, error, loading } = useApiGet<AutomationSummary>("/reviewer/automation-summary");
  if (error !== null) return null;
  return (
    <section aria-label={`Automation in the last ${String(data?.days ?? 7)} days`} className="mb-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
      {loading || data === null ? (
        [0, 1, 2, 3].map((i) => (
          <Card key={i} className="space-y-3 p-4">
            <Skeleton className="h-4 w-24" />
            <Skeleton className="h-7 w-12" />
          </Card>
        ))
      ) : (
        <>
          <Stat
            label="Resolved automatically"
            value={String(data.resolved_automatically)}
            hint="Explained or answered without a person"
            icon={CircleCheck}
            tone="success"
          />
          <Stat
            label="Sent for approval"
            value={String(data.sent_for_approval)}
            hint="Refunds proposed, waiting on a reviewer"
            icon={ShieldCheck}
            tone="primary"
          />
          <Stat
            label="Escalated"
            value={String(data.escalated)}
            hint="Passed to a person by code"
            icon={UserRound}
            tone="warning"
          />
          <Stat
            label="Median time to outcome"
            value={data.median_seconds_to_outcome === null ? "—" : formatDuration(Math.round(data.median_seconds_to_outcome * 1000))}
            hint={`From request to outcome, last ${String(data.days)} days`}
            icon={Hourglass}
            tone="neutral"
          />
        </>
      )}
    </section>
  );
}
