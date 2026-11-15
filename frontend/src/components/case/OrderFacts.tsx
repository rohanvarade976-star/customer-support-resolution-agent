// The order's facts, read now with the refund rules applied now (verified, never model text).
// Shared by the refund case and the escalation case; shown inside a collapsible section.

import { BadgeCheck, ShieldCheck, X } from "lucide-react";

import type { CaseFacts, GoverningPolicy } from "../../api/types";
import { formatDateTime, humanize } from "../../lib/labels";
import { formatMoney } from "../../lib/money";
import { Badge } from "../ui/badge";

// Marks content that comes from our own records or rules, never from a model.
export function VerifiedLabel() {
  return (
    <Badge tone="primary" title="Read from our records or computed by our rules">
      <ShieldCheck aria-hidden="true" />
      Verified from records
    </Badge>
  );
}

export function EligibleBadge({ facts }: { facts: CaseFacts }) {
  return facts.eligible_now ? (
    <Badge tone="success">
      <BadgeCheck aria-hidden="true" />
      Eligible now
    </Badge>
  ) : (
    <Badge tone="danger">
      <X aria-hidden="true" />
      Not eligible now
    </Badge>
  );
}

export function OrderFactsBody({ facts, currency, policy }: { facts: CaseFacts; currency: string; policy: GoverningPolicy | null }) {
  const rows: [string, string][] = [
    ["Order status", humanize(facts.order_status)],
    ["Delivered", formatDateTime(facts.delivered_at)],
    ["Return", facts.return_status === null ? "None" : humanize(facts.return_status)],
    ["Return requested", formatDateTime(facts.return_requested_at)],
    ["Return received", formatDateTime(facts.return_received_at)],
    ["Inspection", facts.inspection_status === null ? "—" : humanize(facts.inspection_status)],
    ["Paid", formatMoney(facts.paid_amount_minor, currency)],
    ["Refunded", formatMoney(facts.succeeded_refunds_minor, currency)],
    ["Refunds in progress", formatMoney(facts.in_progress_refunds_minor, currency)],
    ["Refundable balance", formatMoney(facts.refundable_balance_minor, currency)],
    ["Return window", facts.refund_window_days === null ? "No policy" : `${String(facts.refund_window_days)} days`],
    ["Policy in force", policy === null ? "None found" : `${policy.title} ${policy.version}`],
  ];
  return (
    <div className="space-y-4">
      <p className="text-xs text-gray-500">Read from the order records just now, with the rules applied now.</p>
      <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
        {rows.map(([label, value]) => (
          <div key={label} className="flex justify-between gap-4 border-b border-gray-100 pb-2 sm:block sm:border-0 sm:pb-0">
            <dt className="text-xs text-gray-500">{label}</dt>
            <dd className="text-right text-sm font-medium text-gray-900 tabular-nums sm:mt-0.5 sm:text-left">{value}</dd>
          </div>
        ))}
      </dl>
      {!facts.eligible_now && facts.reasons_now.length > 0 && (
        <div>
          <h3 className="mb-2 text-xs font-medium tracking-wide text-gray-500 uppercase">Why not, now</h3>
          <ul className="flex flex-wrap gap-2">
            {facts.reasons_now.map((reason) => (
              <li key={reason}>
                <Badge tone="danger">{humanize(reason)}</Badge>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
