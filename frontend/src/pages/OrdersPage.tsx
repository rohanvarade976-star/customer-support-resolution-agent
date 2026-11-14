// The customer's own orders (GET /me/orders) as cards, searchable, each with a way to get
// help about it. Amounts are the API's integer paise, formatted for display.

import { CalendarDays, LifeBuoy, Package, Search, SearchX, Truck, Undo2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { Order } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, ListSkeleton } from "../components/states";
import { StatusBadge } from "../components/StatusBadge";
import { Button } from "../components/ui/button";
import { Card, CardFooter } from "../components/ui/card";
import { Input } from "../components/ui/form";
import { useApiGet } from "../hooks/useApiGet";
import { formatDateTime, humanize } from "../lib/labels";
import { formatMoney } from "../lib/money";

function matches(order: Order, needle: string): boolean {
  if (needle === "") return true;
  return (
    order.order_number.toLowerCase().includes(needle) ||
    order.items.some((item) => item.product_name.toLowerCase().includes(needle))
  );
}

export function OrdersPage() {
  const { data, error, loading, reload } = useApiGet<Order[]>("/me/orders");
  const [query, setQuery] = useState("");
  const shown = (data ?? []).filter((order) => matches(order, query.trim().toLowerCase()));

  return (
    <>
      <PageHeader
        title="My orders"
        description="Orders on your account. Get help with any of them in a few steps."
      />

      {error !== null ? (
        <ErrorState message={error} onRetry={reload} />
      ) : loading || data === null ? (
        <ListSkeleton rows={3} />
      ) : data.length === 0 ? (
        <Card>
          <EmptyState icon={Package} title="No orders yet" description="Orders you place will appear here." />
        </Card>
      ) : (
        <>
          <div className="relative mb-4 sm:w-80">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-gray-400" aria-hidden="true" />
            <label htmlFor="order-search" className="sr-only">
              Search orders
            </label>
            <Input
              id="order-search"
              type="search"
              placeholder="Search by order or product"
              className="pl-9"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
              }}
            />
          </div>
          {shown.length === 0 ? (
            <Card>
              <EmptyState
                icon={SearchX}
                title="No orders match"
                description="Try an order number such as ORD-1001, or a product name."
                action={
                  <Button
                    variant="outline"
                    onClick={() => {
                      setQuery("");
                    }}
                  >
                    Clear search
                  </Button>
                }
              />
            </Card>
          ) : (
            <ul className="grid gap-4 md:grid-cols-2">
              {shown.map((order) => (
                <li key={order.order_number} className="flex min-w-0">
                  <OrderCard order={order} />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </>
  );
}

function OrderCard({ order }: { order: Order }) {
  return (
    <Card className="flex w-full min-w-0 flex-col">
      <div className="flex items-start justify-between gap-3 px-5 pt-5">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-gray-900">{order.order_number}</h2>
          <p className="mt-0.5 flex items-center gap-1.5 text-xs text-gray-500">
            <CalendarDays className="size-3.5" aria-hidden="true" />
            Placed {formatDateTime(order.placed_at)}
          </p>
        </div>
        <StatusBadge status={order.status} />
      </div>

      {/* flex-1: in the two-column grid, cards in a row share one height and footers line up. */}
      <div className="flex-1 px-5 pt-4 pb-5">
        <ul className="divide-y divide-gray-100 rounded-lg border border-gray-100">
          {order.items.map((item) => (
            <li key={item.product_id} className="flex items-start justify-between gap-3 px-3 py-2.5 text-sm">
              <span className="min-w-0 text-gray-800">
                <span className="text-gray-500 tabular-nums">{item.quantity} ×</span> {item.product_name}
              </span>
              <span className="shrink-0 text-gray-700 tabular-nums">{formatMoney(item.paid_amount_minor, order.currency)}</span>
            </li>
          ))}
        </ul>

        {(order.delivered_at !== null || order.returns.length > 0) && (
          <div className="mt-4 space-y-1.5 text-xs text-gray-600">
            {order.delivered_at !== null && (
              <p className="flex items-start gap-1.5">
                <Truck className="mt-px size-3.5 shrink-0 text-gray-400" aria-hidden="true" />
                Delivered {formatDateTime(order.delivered_at)}
              </p>
            )}
            {order.returns.map((ret) => (
              <p key={ret.requested_at} className="flex items-start gap-1.5">
                <Undo2 className="mt-px size-3.5 shrink-0 text-gray-400" aria-hidden="true" />
                Return {humanize(ret.status).toLowerCase()} · requested {formatDateTime(ret.requested_at)}
              </p>
            ))}
          </div>
        )}
      </div>

      <CardFooter className="justify-between">
        <span className="text-sm">
          <span className="text-gray-500">Total </span>
          <span className="font-semibold text-gray-900 tabular-nums">
            {formatMoney(order.paid_amount_minor, order.currency)}
          </span>
        </span>
        <Button asChild variant="outline" size="sm">
          <Link to={`/complaints/new?order=${encodeURIComponent(order.order_number)}`}>
            <LifeBuoy aria-hidden="true" />
            Get help
            <span className="sr-only"> with {order.order_number}</span>
          </Link>
        </Button>
      </CardFooter>
    </Card>
  );
}
