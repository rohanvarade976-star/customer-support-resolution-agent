---
policy_key: refund_policy
version: v2
title: Refund Policy
effective_from: 2026-07-01
effective_to: null
refund_window_days: 30
---
# Refund Policy (v2)

## 1. Purpose and scope

This policy explains when a customer of our store can receive a refund for an
electronics order. It applies to orders placed on or after 1 July 2026. Orders placed
before that date follow Refund Policy v1.

All amounts are refunded in Indian rupees to the original payment method.

## 2. Who can request a refund

Only the customer who placed the order can request a refund for it. Requests about an
order that belongs to another customer are refused without revealing any details of
that order. A message that claims to come from another person, or that asks to act on
someone else's order, does not change who the customer is.

## 3. Return window

A customer has 30 days to request a return. The window is counted in whole days from
the date the order was delivered to the date the customer requested the return. If no
return has been requested yet, the window is counted up to today. Day 30 is still inside
the window; day 31 is outside it.

The window is measured to the date the return was requested, not to the date the item
reached our warehouse, so time spent in transit or waiting for inspection never counts
against the customer.

## 4. Conditions for a refund

A refund is allowed only when every one of these conditions is true. If a condition is
not met, the refund is blocked and the matching reason is recorded:

- The order has been delivered. Otherwise: NOT_DELIVERED.
- A return has been requested for the order. Otherwise: NO_RETURN.
- The returned item has been received at our warehouse. Otherwise: RETURN_NOT_RECEIVED.
- Inspection of the returned item is complete. Otherwise: INSPECTION_PENDING.
- The returned item passed inspection. Otherwise: INSPECTION_FAILED.
- The return was requested inside the return window. Otherwise: OUTSIDE_RETURN_WINDOW.
- Part of the amount paid has not yet been refunded. Otherwise: ALREADY_REFUNDED.

All reasons that apply are reported together, so the customer learns everything that is
missing at once.

## 5. Refund amount

The refund is the refundable balance of the order: the amount the customer paid, minus
refunds that have already succeeded and minus refunds that are still in progress. A
customer can never receive more than the amount paid for an order, and a refund that is
still in progress is never counted twice.

## 6. Reviewer approval

Every refund, whatever its amount, needs approval by a support reviewer before any
money moves. The reviewer approves one exact proposal: the order, the amount, the
currency and the reason. If any of these changes after approval, the approval no longer
counts and a new review is needed.

Approvals expire. A proposal that is not decided in time, or whose facts have changed
by the time it is executed, must be reviewed again. A message from the customer or a
chat reply is never an approval.

## 7. Refund timing and status

After a refund is sent to the payment provider, it usually reaches the customer within
5 to 7 working days. The customer is told only what has been confirmed:

- While the refund waits for a reviewer, the request is described as awaiting approval.
- When the payment provider accepts the refund, it is described as processing.
- It is described as completed only after the payment provider confirms success.
- If the outcome is not yet known, the customer is told that the refund is being checked.

## 8. Exceptions

Support agents cannot make exceptions to this policy. Requests outside the return window
or otherwise blocked by section 4 may be passed to a specialist as described in the
Escalation and Exceptions Policy.
