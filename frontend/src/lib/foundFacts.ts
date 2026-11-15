// "What we found": the verified facts the API sends about the order (RunStatusEvent.facts) as
// short labelled lines. Fixed wording per value. This only displays: it never decides
// eligibility, amounts or outcomes (ADR-023).

import type { CustomerRefundStatus, OrderStatusCode, ReturnStatusCode, RunFacts } from "../api/types";
import { formatDateOnly, formatDay } from "./dates";
import { humanize } from "./labels";

export type FoundIcon = "order" | "delivery" | "return" | "refund";

export interface FoundFact {
  key: string;
  icon: FoundIcon;
  label: string;
  value: string;
}

const RETURN_TEXT: Record<ReturnStatusCode, string> = {
  requested: "Return requested",
  in_transit: "Return on its way back to us",
  received: "Return received",
  cancelled: "Return cancelled",
};

const REFUND_TEXT: Record<CustomerRefundStatus, string> = {
  in_progress: "Refund is being processed",
  succeeded: "Refund completed",
  failed: "Refund could not be completed",
};

function orderStatusText(status: OrderStatusCode, deliveredAt: string | null): string {
  if (status === "delivered") return deliveredAt === null ? "Delivered" : `Delivered on ${formatDay(deliveredAt)}`;
  if (status === "shipped") return "On its way";
  if (status === "placed") return "Placed, not shipped yet";
  return humanize(status);
}

export function foundFacts(facts: RunFacts | null): FoundFact[] {
  if (facts === null) return [];
  const found: FoundFact[] = [
    {
      key: "order",
      icon: "order",
      label: "Order",
      value: `${facts.order_number} · ${orderStatusText(facts.order_status, facts.delivered_at)}`,
    },
  ];
  // Only while the order is in transit: the carrier's own status and expected date.
  if (facts.shipment !== null && facts.shipment.status === "in_transit") {
    found.push({
      key: "shipment",
      icon: "delivery",
      label: "Delivery",
      value: `In transit with the carrier · expected ${formatDateOnly(facts.shipment.expected_delivery)}`,
    });
  }
  if (facts.return_status !== null) {
    found.push({ key: "return", icon: "return", label: "Return", value: RETURN_TEXT[facts.return_status] });
  }
  if (facts.refund_status !== null) {
    found.push({ key: "refund", icon: "refund", label: "Refund", value: REFUND_TEXT[facts.refund_status] });
  }
  return found;
}
