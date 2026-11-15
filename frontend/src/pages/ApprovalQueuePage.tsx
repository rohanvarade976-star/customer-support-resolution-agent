// Reviewers: pending, unexpired refund approvals (GET /approvals) as a sortable table, with
// the time left before each expires coloured by urgency. Sorting only reorders the rows the
// API returned; the amounts are the stored proposals', formatted for display.
//
// Above the table, what the system did on its own this week; on each row, the AI's recommended
// action (labelled as AI) from the audit trail (ADR-021). The AI's rationale is on the case page.
//
// Two tabs split the queue by the readiness verdict computed by code (ADR-022): "Ready to
// approve" and "Needs attention", each with its count. Each row shows its verdict and, when it
// needs attention, the most serious reason. The tabs only filter the rows the API returned.

import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronRight,
  CircleCheckBig,
  Clock,
  Inbox,
  RefreshCw,
  Sparkles,
  TriangleAlert,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { ApprovalList, PendingApproval, ReadinessStatus } from "../api/types";
import { AutomationSummaryCards } from "../components/ai/AutomationSummaryCards";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, ListSkeleton } from "../components/states";
import { Badge, type BadgeTone } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { useApiGet } from "../hooks/useApiGet";
import { useNow } from "../hooks/useNow";
import { actionLabel } from "../lib/analysis";
import { formatDateTime } from "../lib/labels";
import { formatMoney } from "../lib/money";
import { readinessText } from "../lib/readiness";
import { timeUntil, type Urgency } from "../lib/time";
import { cn } from "../lib/utils";

type SortKey = "customer" | "order" | "amount" | "expiry";
type Direction = "asc" | "desc";

const URGENCY_TONE: Record<Urgency, BadgeTone> = {
  expired: "danger",
  urgent: "danger",
  soon: "warning",
  normal: "neutral",
};

const COMPARE: Record<SortKey, (a: PendingApproval, b: PendingApproval) => number> = {
  customer: (a, b) => a.customer_name.localeCompare(b.customer_name),
  order: (a, b) => a.order_number.localeCompare(b.order_number, undefined, { numeric: true }),
  amount: (a, b) => a.amount_minor - b.amount_minor, // integers: an exact comparison
  expiry: (a, b) => a.expires_at.localeCompare(b.expires_at),
};

const TABS: { key: ReadinessStatus; label: string; icon: LucideIcon; iconClass: string }[] = [
  { key: "ready_to_approve", label: "Ready to approve", icon: CircleCheckBig, iconClass: "text-success-600" },
  { key: "needs_attention", label: "Needs attention", icon: TriangleAlert, iconClass: "text-warning-600" },
];

function ExpiryBadge({ expiresAt, now }: { expiresAt: string; now: number }) {
  const left = timeUntil(expiresAt, now);
  return (
    <Badge tone={URGENCY_TONE[left.urgency]} title={`Expires ${formatDateTime(expiresAt)}`}>
      <Clock aria-hidden="true" />
      {left.short}
    </Badge>
  );
}

function AiActionBadge({ action }: { action: string | null }) {
  return (
    <Badge tone="ai" title="The AI's recommendation. Not verified; the amount comes from the refund rules.">
      <Sparkles aria-hidden="true" />
      <span className="sr-only">AI recommends: </span>
      {actionLabel(action)}
    </Badge>
  );
}

// The verdict computed by code, and for a case that needs attention its most serious reason.
function ReadinessBadge({ item }: { item: PendingApproval }) {
  if (item.readiness.status === "ready_to_approve") {
    return (
      <Badge tone="success" data-testid="readiness">
        <CircleCheckBig aria-hidden="true" />
        Ready
      </Badge>
    );
  }
  const reasons = item.readiness.reasons.map((reason) => readinessText(reason).text);
  const more = reasons.length - 1;
  return (
    <span className="flex min-w-0 flex-col items-start gap-1" data-testid="readiness">
      <Badge tone="warning" title={reasons.join(" · ")}>
        <TriangleAlert aria-hidden="true" />
        Needs attention
      </Badge>
      <span className="line-clamp-2 text-xs text-gray-600">
        {reasons[0]}
        {more > 0 && <span className="text-gray-500"> · +{more} more</span>}
      </span>
    </span>
  );
}

function YoursBadge() {
  return (
    <Badge tone="neutral" title="You proposed this refund, so another reviewer must decide it">
      <UserRound aria-hidden="true" />
      Proposed by you
    </Badge>
  );
}

interface SortHeaderProps {
  column: SortKey;
  label: string;
  sort: { key: SortKey; direction: Direction };
  onToggle: (key: SortKey) => void;
  className?: string;
}

// A column header that sorts by its column; aria-sort tells screen readers the current order.
function SortHeader({ column, label, sort, onToggle, className }: SortHeaderProps) {
  const active = sort.key === column;
  const Icon = !active ? ArrowUpDown : sort.direction === "asc" ? ArrowUp : ArrowDown;
  return (
    <TableHead className={className} aria-sort={active ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}>
      <button
        type="button"
        onClick={() => {
          onToggle(column);
        }}
        className={cn(
          "-mx-1.5 inline-flex cursor-pointer items-center gap-1 rounded px-1.5 py-1 hover:text-gray-900",
          active && "text-gray-900",
        )}
      >
        {label}
        <Icon className={cn("size-3.5", active ? "text-gray-700" : "text-gray-400")} aria-hidden="true" />
      </button>
    </TableHead>
  );
}

export function ApprovalQueuePage() {
  const { data, error, loading, reload } = useApiGet<ApprovalList>("/approvals");
  const navigate = useNavigate();
  const now = useNow(30_000);
  const [sort, setSort] = useState<{ key: SortKey; direction: Direction }>({ key: "expiry", direction: "asc" });
  const [chosenTab, setChosenTab] = useState<ReadinessStatus | null>(null);

  const all = data?.items ?? [];
  const counts: Record<ReadinessStatus, number> = {
    ready_to_approve: all.filter((item) => item.readiness.status === "ready_to_approve").length,
    needs_attention: all.filter((item) => item.readiness.status === "needs_attention").length,
  };
  // Until the reviewer picks a tab: the quick decisions first, unless there are none.
  const tab: ReadinessStatus = chosenTab ?? (counts.ready_to_approve > 0 ? "ready_to_approve" : "needs_attention");
  const items = all.filter((item) => item.readiness.status === tab);
  const sorted = [...items].sort((a, b) => {
    const result = COMPARE[sort.key](a, b);
    return sort.direction === "asc" ? result : -result;
  });
  const urgent = all.filter((item) => {
    const urgency = timeUntil(item.expires_at, now).urgency;
    return urgency === "urgent" || urgency === "expired";
  }).length;

  function toggle(key: SortKey) {
    setSort((current) =>
      current.key === key
        ? { key, direction: current.direction === "asc" ? "desc" : "asc" }
        : { key, direction: key === "amount" ? "desc" : "asc" },
    );
  }

  return (
    <>
      <PageHeader
        title="Approval queue"
        description="Refunds proposed by the system and checked by code, waiting for a person to decide."
        actions={
          <Button variant="outline" onClick={reload}>
            <RefreshCw aria-hidden="true" />
            Refresh
          </Button>
        }
      />

      <AutomationSummaryCards />

      {error !== null ? (
        <ErrorState message={error} onRetry={reload} />
      ) : loading ? (
        <ListSkeleton rows={4} />
      ) : all.length === 0 ? (
        <Card>
          <EmptyState
            icon={Inbox}
            title="You're all caught up"
            description="New refund proposals appear here as soon as they are ready for a decision."
            action={
              <Button variant="outline" onClick={reload}>
                <RefreshCw aria-hidden="true" />
                Check again
              </Button>
            }
          />
        </Card>
      ) : (
        <Card>
          <div className="flex flex-col gap-3 border-b border-gray-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
            <div role="tablist" aria-label="Readiness" className="grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1 sm:flex">
              {TABS.map(({ key, label, icon: Icon, iconClass }) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={tab === key}
                  onClick={() => {
                    setChosenTab(key);
                  }}
                  className={cn(
                    "flex cursor-pointer items-center justify-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm font-medium whitespace-nowrap transition-colors sm:px-3",
                    tab === key ? "bg-white text-gray-900 shadow-xs" : "text-gray-600 hover:text-gray-900",
                  )}
                >
                  <Icon className={cn("hidden size-4 sm:block", iconClass)} aria-hidden="true" />
                  {label}
                  <span className="text-xs text-gray-500 tabular-nums">{counts[key]}</span>
                </button>
              ))}
            </div>
            {urgent > 0 && (
              <span className="flex items-center gap-1.5 text-sm text-danger-700">
                <span className="size-1.5 rounded-full bg-danger-500" aria-hidden="true" />
                {urgent} expiring within 10 minutes
              </span>
            )}
          </div>

          {items.length === 0 ? (
            <EmptyState
              icon={tab === "ready_to_approve" ? CircleCheckBig : Inbox}
              title={tab === "ready_to_approve" ? "Nothing is ready to approve" : "Nothing needs attention"}
              description={
                tab === "ready_to_approve"
                  ? "Every waiting case has something to check first. See Needs attention."
                  : "Every waiting case passed all checks."
              }
            />
          ) : (
            <>
              {/* Phones: a list of cards. */}
              <ul className="divide-y divide-gray-100 md:hidden">
                {sorted.map((item) => (
                  <li key={item.approval_id}>
                    <Link to={`/approvals/${item.approval_id}`} className="flex items-center gap-3 px-4 py-4 hover:bg-gray-50">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center justify-between gap-3">
                          <span className="truncate font-medium text-gray-900">{item.customer_name}</span>
                          <span className="font-semibold text-gray-900 tabular-nums">{formatMoney(item.amount_minor, item.currency)}</span>
                        </div>
                        <p className="mt-0.5 truncate text-sm text-gray-600">{item.order_number}</p>
                        <div className="mt-2">
                          <ReadinessBadge item={item} />
                        </div>
                        <div className="mt-2 flex flex-wrap gap-2">
                          <ExpiryBadge expiresAt={item.expires_at} now={now} />
                          <AiActionBadge action={item.ai_action} />
                          {item.proposed_by_you && <YoursBadge />}
                        </div>
                      </div>
                      <ChevronRight className="size-4 shrink-0 text-gray-400" aria-hidden="true" />
                    </Link>
                  </li>
                ))}
              </ul>

              {/* Wider screens: a sortable table. */}
              <div className="hidden md:block">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <SortHeader column="customer" label="Customer" sort={sort} onToggle={toggle} />
                      <SortHeader column="order" label="Order" sort={sort} onToggle={toggle} />
                      <SortHeader column="amount" label="Amount" sort={sort} onToggle={toggle} className="text-right" />
                      <TableHead>Readiness</TableHead>
                      <TableHead className="hidden xl:table-cell">AI recommendation</TableHead>
                      <SortHeader column="expiry" label="Expires in" sort={sort} onToggle={toggle} />
                      <TableHead>
                        <span className="sr-only">Open</span>
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sorted.map((item) => (
                      <TableRow
                        key={item.approval_id}
                        className="group cursor-pointer hover:bg-gray-50"
                        onClick={() => void navigate(`/approvals/${item.approval_id}`)}
                      >
                        <TableCell className="whitespace-nowrap">
                          {/* The link makes each row reachable by keyboard; a click anywhere on the row does the same. */}
                          <Link to={`/approvals/${item.approval_id}`} className="rounded font-medium text-gray-900">
                            {item.customer_name}
                          </Link>
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-gray-700">{item.order_number}</TableCell>
                        <TableCell className="text-right font-semibold text-gray-900 tabular-nums">
                          {formatMoney(item.amount_minor, item.currency)}
                        </TableCell>
                        <TableCell className="max-w-72">
                          <div className="flex flex-col items-start gap-1">
                            <ReadinessBadge item={item} />
                            {item.proposed_by_you && <YoursBadge />}
                          </div>
                        </TableCell>
                        <TableCell className="hidden xl:table-cell">
                          <AiActionBadge action={item.ai_action} />
                        </TableCell>
                        <TableCell>
                          <ExpiryBadge expiresAt={item.expires_at} now={now} />
                        </TableCell>
                        <TableCell className="w-10 text-right">
                          <ChevronRight
                            className="inline size-4 text-gray-400 transition-transform group-hover:translate-x-0.5 group-hover:text-gray-600"
                            aria-hidden="true"
                          />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </>
          )}
        </Card>
      )}
    </>
  );
}
