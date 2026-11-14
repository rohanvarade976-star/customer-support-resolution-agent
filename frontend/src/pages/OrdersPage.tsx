// The customer's own orders (GET /me/orders), with a link to raise a complaint about one.

import { Link } from "react-router-dom";

import type { Order } from "../api/types";
import { Card, ErrorBanner, Loading, PageTitle, StatusBadge } from "../components/ui";
import { useApiGet } from "../hooks/useApiGet";
import { formatDateTime, humanize } from "../lib/labels";
import { formatMoney } from "../lib/money";

export function OrdersPage() {
  const { data, error, loading } = useApiGet<Order[]>("/me/orders");

  return (
    <>
      <PageTitle>My orders</PageTitle>
      <ErrorBanner message={error} />
      {loading && <Loading />}
      {data?.length === 0 && <p className="text-sm text-slate-600">You have no orders.</p>}
      {data?.map((order) => (
        <Card key={order.order_number}>
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-medium">{order.order_number}</span>
            <StatusBadge status={order.status} />
            <span className="ml-auto font-medium">
              {formatMoney(order.paid_amount_minor, order.currency)}
            </span>
          </div>
          <ul className="mt-2 text-sm text-slate-700">
            {order.items.map((item) => (
              <li key={item.product_id}>
                {item.quantity} × {item.product_name}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-slate-500">
            Placed {formatDateTime(order.placed_at)}
            {order.delivered_at !== null && ` · Delivered ${formatDateTime(order.delivered_at)}`}
            {order.returns.map((ret) => ` · Return ${humanize(ret.status).toLowerCase()}`).join("")}
          </p>
          <Link
            to={`/complaints/new?order=${encodeURIComponent(order.order_number)}`}
            className="mt-2 inline-block text-sm text-indigo-700 underline"
          >
            Raise a complaint about this order
          </Link>
        </Card>
      ))}
    </>
  );
}
