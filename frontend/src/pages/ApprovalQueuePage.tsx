// Reviewers: pending, unexpired refund approvals (GET /approvals), soonest expiry first.

import { Link } from "react-router-dom";

import type { ApprovalList } from "../api/types";
import { Card, ErrorBanner, Loading, PageTitle, secondaryButtonClass } from "../components/ui";
import { useApiGet } from "../hooks/useApiGet";
import { formatDateTime } from "../lib/labels";
import { formatMoney } from "../lib/money";

export function ApprovalQueuePage() {
  const { data, error, loading, reload } = useApiGet<ApprovalList>("/approvals");

  return (
    <>
      <div className="flex items-center">
        <PageTitle>Approval queue</PageTitle>
        <button type="button" className={`${secondaryButtonClass} ml-auto`} onClick={reload}>
          Refresh
        </button>
      </div>
      <ErrorBanner message={error} />
      {loading && <Loading />}
      {data?.items.length === 0 && <p className="text-sm text-slate-600">Nothing is waiting.</p>}
      {data?.items.map((item) => (
        <Card key={item.approval_id}>
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-medium">{item.customer_name}</span>
            <span>{item.order_number}</span>
            <span className="ml-auto font-semibold">{formatMoney(item.amount_minor, item.currency)}</span>
          </div>
          <p className="mt-1 text-sm text-slate-600">{item.reason}</p>
          <div className="mt-2 flex items-center text-sm">
            <span className="text-xs text-slate-500">Expires {formatDateTime(item.expires_at)}</span>
            <Link to={`/approvals/${item.approval_id}`} className="ml-auto text-indigo-700 underline">
              Review case
            </Link>
          </div>
        </Card>
      ))}
    </>
  );
}
