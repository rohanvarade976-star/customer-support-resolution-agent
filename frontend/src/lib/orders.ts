// Short wording for an order, for lists and cards.

import type { Order } from "../api/types";

// "Wireless Headphones" or "Wireless Headphones + 2 more".
export function orderItemsSummary(order: Order): string {
  const [first, ...rest] = order.items;
  if (first === undefined) return "No items";
  return rest.length === 0 ? first.product_name : `${first.product_name} + ${rest.length} more`;
}
