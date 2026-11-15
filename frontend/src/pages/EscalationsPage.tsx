// Reviewers: escalated tickets that still need a person (GET /reviewer/escalations, ADR-022),
// newest escalation first. Each row: who, which order, how long ago, and why it was escalated
// in plain words (the reasons are codes computed by code from the audit trail).

import { ChevronRight, Headset, Inbox, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";

import type { EscalationItem, EscalationList } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, ListSkeleton } from "../components/states";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { useApiGet } from "../hooks/useApiGet";
import { formatDateTime, humanize } from "../lib/labels";
import { escalationText } from "../lib/readiness";
import { formatRelative } from "../lib/time";

export function EscalationsPage() {
  const { data, error, loading, reload } = useApiGet<EscalationList>("/reviewer/escalations");
  const items = data?.items ?? [];

  return (
    <>
      <PageHeader
        title="Escalations"
        description="Cases the system handed to a person. Reply to the customer, propose a refund, or resolve."
        actions={
          <Button variant="outline" onClick={reload}>
            <RefreshCw aria-hidden="true" />
            Refresh
          </Button>
        }
      />
      {error !== null ? (
        <ErrorState message={error} onRetry={reload} />
      ) : loading ? (
        <ListSkeleton rows={3} />
      ) : items.length === 0 ? (
        <Card>
          <EmptyState
            icon={Inbox}
            title="No escalated cases"
            description="When the system passes a case to a person, it appears here."
          />
        </Card>
      ) : (
        <Card>
          <p className="border-b border-gray-100 px-5 py-3 text-sm text-gray-600">
            <span className="font-semibold text-gray-900 tabular-nums">{items.length}</span> waiting for a person
          </p>
          <ul className="divide-y divide-gray-100" aria-label="Escalated cases">
            {items.map((item) => (
              <EscalationRow key={item.ticket_id} item={item} />
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}

function EscalationRow({ item }: { item: EscalationItem }) {
  return (
    <li>
      <Link to={`/escalations/${item.ticket_id}`} className="flex items-center gap-4 px-5 py-4 hover:bg-gray-50">
        <span className="hidden size-10 shrink-0 place-items-center rounded-lg bg-warning-50 text-warning-700 sm:grid">
          <Headset className="size-5" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium text-gray-900">{item.customer_name}</span>
            <span className="text-sm text-gray-600">· {item.order_number ?? "No order"}</span>
            {item.category !== null && <span className="text-xs text-gray-500">· {humanize(item.category)}</span>}
            {item.specialist_replied && <Badge tone="info">Replied</Badge>}
          </div>
          {/* The customer's own words, shown as plain text. */}
          <p className="mt-1 line-clamp-1 text-sm text-gray-700">{item.complaint}</p>
          <ul className="mt-2 flex flex-wrap gap-1.5" aria-label="Why it was escalated">
            {item.reasons.map((reason) => (
              <li key={reason.code}>
                <Badge tone="warning" className="whitespace-normal">
                  {escalationText(reason.code, reason.codes)}
                </Badge>
              </li>
            ))}
          </ul>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <time className="hidden text-xs text-gray-500 sm:inline" dateTime={item.escalated_at} title={formatDateTime(item.escalated_at)}>
            {formatRelative(item.escalated_at)}
          </time>
          <ChevronRight className="size-4 text-gray-400" aria-hidden="true" />
        </div>
      </Link>
    </li>
  );
}
