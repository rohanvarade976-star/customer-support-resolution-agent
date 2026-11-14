// Reviewers: pending, unexpired refund approvals (GET /approvals) as a sortable table, with
// the time left before each expires coloured by urgency. Sorting only reorders the rows the
// API returned; the amounts are the stored proposals', formatted for display.
//
// Above the table, what the system did on its own this week; on each row, the AI's recommended
// action (labelled as AI) and the risk flags computed by code (ADR-021). Both are codes from
// the audit trail: the AI's rationale is shown on the case page only.

import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronRight,
  Clock,
  Inbox,
  RefreshCw,
  ShieldExclamationPoint,
  Sparkles,
} from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { ApprovalList, PendingApproval } from "../api/types";
import { AutomationSummaryCards } from "../components/ai/AutomationSummaryCards";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, ListSkeleton } from "../components/states";
import { Badge, type BadgeTone } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { useApiGet } from "../hooks/useApiGet";
import { useNow } from "../hooks/useNow";
import { actionLabel, riskText } from "../lib/analysis";
import { formatDateTime } from "../lib/labels";
import { formatMoney } from "../lib/money";
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

// Risk flags are computed by code; the badge lists them in its title and screen-reader text.
function RiskIndicator({ flags }: { flags: string[] }) {
  if (flags.length === 0) {
    return <span className="text-xs text-gray-500">No flags</span>;
  }
  const titles = flags.map((code) => riskText(code).title);
  return (
    <Badge tone="warning" title={titles.join(" · ")} data-testid="risk-indicator">
      <ShieldExclamationPoint aria-hidden="true" />
      {flags.length === 1 ? (titles[0] ?? "1 flag") : `${String(flags.length)} risk flags`}
      {flags.length > 1 && <span className="sr-only">: {titles.join(", ")}</span>}
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

  const items = data?.items ?? [];
  const sorted = [...items].sort((a, b) => {
    const result = COMPARE[sort.key](a, b);
    return sort.direction === "asc" ? result : -result;
  });
  const urgent = items.filter((item) => {
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
      ) : items.length === 0 ? (
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
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-gray-100 px-5 py-3 text-sm text-gray-600">
            <span>
              <span className="font-semibold text-gray-900 tabular-nums">{items.length}</span> waiting
            </span>
            {urgent > 0 && (
              <span className="flex items-center gap-1.5 text-danger-700">
                <span className="size-1.5 rounded-full bg-danger-500" aria-hidden="true" />
                {urgent} expiring within 10 minutes
              </span>
            )}
          </div>

          {/* Phones: a list of cards. */}
          <ul className="divide-y divide-gray-100 md:hidden">
            {sorted.map((item) => (
              <li key={item.approval_id}>
                <Link to={`/approvals/${item.approval_id}`} className="flex items-center gap-3 px-5 py-4 hover:bg-gray-50">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-3">
                      <span className="truncate font-medium text-gray-900">{item.customer_name}</span>
                      <span className="font-semibold text-gray-900 tabular-nums">{formatMoney(item.amount_minor, item.currency)}</span>
                    </div>
                    <p className="mt-0.5 truncate text-sm text-gray-600">
                      {item.order_number} · {item.reason}
                    </p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      <ExpiryBadge expiresAt={item.expires_at} now={now} />
                      <AiActionBadge action={item.ai_action} />
                      {item.risk_flags.length > 0 && <RiskIndicator flags={item.risk_flags} />}
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
                  <TableHead className="hidden 2xl:table-cell">Reason</TableHead>
                  <TableHead>AI recommendation</TableHead>
                  <TableHead>Risk</TableHead>
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
                    <TableCell className="hidden max-w-60 truncate text-gray-600 2xl:table-cell" title={item.reason}>
                      {item.reason}
                    </TableCell>
                    <TableCell>
                      <AiActionBadge action={item.ai_action} />
                    </TableCell>
                    <TableCell>
                      <RiskIndicator flags={item.risk_flags} />
                    </TableCell>
                    <TableCell>
                      <ExpiryBadge expiresAt={item.expires_at} now={now} />
                    </TableCell>
                    <TableCell className="w-10 text-right">
                      <ChevronRight className="inline size-4 text-gray-400 transition-transform group-hover:translate-x-0.5 group-hover:text-gray-600" aria-hidden="true" />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </Card>
      )}
    </>
  );
}
