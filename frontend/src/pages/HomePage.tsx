// The customer's home: a greeting, three counts of their tickets by status, their recent
// tickets and orders. The counts only tally the statuses the API returned for the latest
// tickets; nothing is decided here.

import {
  ArrowRight,
  CircleCheck,
  Inbox,
  MessageSquareText,
  Package,
  Plus,
  TriangleExclamationPoint,
  type LucideIcon,
} from "lucide-react";
import { Link } from "react-router-dom";

import type { Order, TicketList } from "../api/types";
import { useAuth } from "../auth/auth";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, ListSkeleton } from "../components/states";
import { StatusBadge } from "../components/StatusBadge";
import { TicketListItem } from "../components/TicketListItem";
import { Button } from "../components/ui/button";
import { Card, CardHeader, CardTitle } from "../components/ui/card";
import { Skeleton } from "../components/ui/skeleton";
import { useApiGet } from "../hooks/useApiGet";
import { formatMoney } from "../lib/money";
import { orderItemsSummary } from "../lib/orders";
import { formatRelative } from "../lib/time";
import { cn } from "../lib/utils";

// The same list "My tickets" loads: the latest 50 tickets.
const TICKETS_PATH = "/tickets?limit=50";

interface StatProps {
  label: string;
  value: number;
  hint: string;
  icon: LucideIcon;
  tone: "primary" | "warning" | "success";
  highlight?: boolean;
}

const STAT_ICON = {
  primary: "bg-primary-50 text-primary-600",
  warning: "bg-warning-50 text-warning-600",
  success: "bg-success-50 text-success-600",
};

function Stat({ label, value, hint, icon: Icon, tone, highlight = false }: StatProps) {
  return (
    <Card className={cn("p-5", highlight && "border-warning-200 ring-1 ring-warning-200")}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-gray-600">{label}</p>
          <p className="mt-2 text-3xl font-semibold tracking-tight text-gray-900 tabular-nums">{value}</p>
        </div>
        <span className={cn("grid size-10 place-items-center rounded-lg", STAT_ICON[tone])}>
          <Icon className="size-5" aria-hidden="true" />
        </span>
      </div>
      <p className="mt-3 text-xs text-gray-500">{hint}</p>
    </Card>
  );
}

export function HomePage() {
  const { user } = useAuth();
  const tickets = useApiGet<TicketList>(TICKETS_PATH);
  const orders = useApiGet<Order[]>("/me/orders");
  const firstName = (user?.name ?? "").split(" ")[0] ?? "";

  const items = tickets.data?.items ?? [];
  const open = items.filter((ticket) => ticket.status !== "resolved").length;
  const awaiting = items.filter((ticket) => ticket.status === "awaiting_customer").length;
  const resolved = items.filter((ticket) => ticket.status === "resolved").length;

  return (
    <>
      <PageHeader
        title={`Welcome back, ${firstName}`}
        description="Here's where your requests stand."
        actions={
          <Button asChild>
            <Link to="/complaints/new">
              <Plus aria-hidden="true" />
              New request
            </Link>
          </Button>
        }
      />

      {tickets.error !== null ? (
        <ErrorState message={tickets.error} onRetry={tickets.reload} />
      ) : (
        <>
          <section aria-label="Summary" className="grid gap-4 sm:grid-cols-3">
            {tickets.loading ? (
              [0, 1, 2].map((i) => (
                <Card key={i} className="space-y-3 p-5">
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="h-8 w-12" />
                  <Skeleton className="h-3 w-32" />
                </Card>
              ))
            ) : (
              <>
                <Stat label="Open requests" value={open} hint="Being worked on or waiting" icon={Inbox} tone="primary" />
                <Stat
                  label="Awaiting your reply"
                  value={awaiting}
                  hint={awaiting > 0 ? "We need a detail from you to continue" : "Nothing needs your attention"}
                  icon={TriangleExclamationPoint}
                  tone="warning"
                  highlight={awaiting > 0}
                />
                <Stat label="Resolved" value={resolved} hint="Closed with a verified outcome" icon={CircleCheck} tone="success" />
              </>
            )}
          </section>

          <div className="mt-6 grid gap-6 lg:grid-cols-3">
            <div className="lg:col-span-2">
              {tickets.loading ? (
                <ListSkeleton rows={3} />
              ) : (
                <Card>
                  <CardHeader className="items-center pb-4">
                    <CardTitle>Recent requests</CardTitle>
                    {items.length > 0 && (
                      <Button asChild variant="link" size="sm">
                        <Link to="/tickets">
                          View all
                          <ArrowRight aria-hidden="true" />
                        </Link>
                      </Button>
                    )}
                  </CardHeader>
                  {items.length === 0 ? (
                    <EmptyState
                      icon={MessageSquareText}
                      title="No requests yet"
                      description="When something goes wrong with an order, start a request and follow it here."
                      action={
                        <Button asChild>
                          <Link to="/complaints/new">Start a request</Link>
                        </Button>
                      }
                    />
                  ) : (
                    <ul className="divide-y divide-gray-100 border-t border-gray-100">
                      {items.slice(0, 4).map((ticket) => (
                        <TicketListItem key={ticket.id} ticket={ticket} />
                      ))}
                    </ul>
                  )}
                </Card>
              )}
            </div>

            <OrdersCard orders={orders.data} loading={orders.loading} error={orders.error} onRetry={orders.reload} />
          </div>
        </>
      )}
    </>
  );
}

interface OrdersCardProps {
  orders: Order[] | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

function OrdersCard({ orders, loading, error, onRetry }: OrdersCardProps) {
  if (error !== null) return <ErrorState message={error} onRetry={onRetry} />;
  if (loading || orders === null) return <ListSkeleton rows={3} />;
  return (
    <Card className="self-start">
      <CardHeader className="items-center pb-4">
        <CardTitle>Your orders</CardTitle>
        {orders.length > 0 && (
          <Button asChild variant="link" size="sm">
            <Link to="/orders">
              View all
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        )}
      </CardHeader>
      {orders.length === 0 ? (
        <EmptyState icon={Package} title="No orders yet" description="Orders you place will appear here." />
      ) : (
        <ul className="divide-y divide-gray-100 border-t border-gray-100">
          {orders.slice(0, 4).map((order) => (
            <li key={order.order_number} className="px-5 py-3.5">
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-medium text-gray-900">{order.order_number}</span>
                <StatusBadge status={order.status} />
              </div>
              <p className="mt-1 truncate text-sm text-gray-600">{orderItemsSummary(order)}</p>
              <div className="mt-1 flex items-center justify-between gap-3 text-xs text-gray-500">
                <span>Placed {formatRelative(order.placed_at)}</span>
                <span className="font-medium text-gray-700 tabular-nums">
                  {formatMoney(order.paid_amount_minor, order.currency)}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
