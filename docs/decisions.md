# Architecture Decision Records

Short records of important decisions: what we chose, why, and what it costs us.
Add new entries at the bottom. Never edit an old decision; write a new one that
replaces it and mark the old one "Superseded by ADR-NNN".

---

## ADR-000: Template

- **Date:** YYYY-MM-DD
- **Status:** Proposed | Accepted | Superseded by ADR-NNN
- **Milestone:** M?

### Context

What problem are we solving? What constraints matter (free tier, security, time)?

### Decision

What we chose, in one or two sentences.

### Alternatives considered

- Option A — why not.
- Option B — why not.

### Consequences

What gets easier, what gets harder, and what we must watch for.

---

## ADR-001: UUID primary keys

- **Date:** 2026-11-09
- **Status:** Accepted
- **Milestone:** M1

### Context

IDs appear in URLs, API responses, tool calls and agent prompts. Sequential IDs
(`/orders/42`) let anyone guess other customers' records and reveal how many orders
exist. Some IDs (proposals, refunds) must be known before the row is inserted, so
the idempotency key and audit events can refer to them.

### Decision

Every table uses a UUID (v4) primary key, generated in Python with a
`gen_random_uuid()` server default as a backup. Orders also get a human-readable,
unique `order_number` (e.g. `ORD-1042`) for customers and support staff.

### Alternatives considered

- Auto-increment integers — guessable, and the ID is only known after INSERT.
- UUID v7 (time-ordered) — better index locality, but not yet built into Python
  3.12 or Postgres 16; not worth a dependency at our data size.

### Consequences

- IDs reveal nothing and can be created anywhere (app, seed scripts, tests).
- IDs are 16 bytes and hard to read aloud, which is why `order_number` exists.
- Guessing is not access control: services must still check ownership.

---

## ADR-002: Money stored as integer paise

- **Date:** 2026-11-09
- **Status:** Accepted
- **Milestone:** M1

### Context

Floats cannot represent most decimal amounts exactly (`0.1 + 0.2 != 0.3`). In a
system that issues refunds, rounding drift could over-refund or block a valid one.

### Decision

All amounts are stored as `BIGINT` columns named `*_minor`, in the smallest
currency unit (paise for INR; ₹2,499.00 = `249900`). CHECK constraints enforce
`> 0` for refund amounts and `>= 0` for paid amounts. Conversion to rupees
happens only when displaying.

### Alternatives considered

- `FLOAT` / `DOUBLE` — inexact; ruled out by the non-negotiable rules.
- `NUMERIC(12, 2)` — exact, but arrives in Python as `Decimal`, which is easy to
  mix up with floats and does not match payment provider APIs (they use minor units).

### Consequences

- Integer arithmetic is exact, fast and matches payment APIs directly.
- The `_minor` suffix makes the unit obvious in every query and payload.
- Every display layer must divide by 100 (correct for INR; other currencies
  have different minor units, which matters only if we add them).
- Cross-row rules (total refunds ≤ paid amount) cannot be a CHECK; the refund
  service enforces them under a row lock (M8).

---

## ADR-003: Statuses as strings with CHECK constraints, not Postgres ENUM types

- **Date:** 2026-11-09
- **Status:** Accepted
- **Milestone:** M1

### Context

Status columns (run, ticket, refund operation, ...) must only hold known values,
or a typo could leave a refund in a state no code handles. The values will grow
as the project develops.

### Decision

Statuses are Python `StrEnum`s in `domain/enums.py`, stored as `VARCHAR(32)` with
a named CHECK constraint listing the allowed values (SQLAlchemy `Enum` with
`native_enum=False, create_constraint=True`). The lowercase value is stored, not
the Python member name.

### Alternatives considered

- Postgres `ENUM` types — values cannot be removed or renamed easily, `ALTER TYPE
  ... ADD VALUE` has transaction restrictions, and Alembic autogenerate handles
  them poorly.
- Plain strings with no CHECK — no protection against bad values.
- Lookup tables with foreign keys — more joins and seed data for no benefit at this size.

### Consequences

- Invalid statuses are rejected by both Python and the database.
- Adding or removing a value is a migration that drops and recreates one CHECK constraint.
- The enum lists live in two places (Python and the migration); a migration must
  accompany every enum change.

---

## ADR-004: Refund creation is never retried automatically

- **Date:** 2026-11-10
- **Status:** Accepted
- **Milestone:** M2

### Context

Retrying a failed network call is normally the right thing to do, and our provider
clients retry reads and ticket writes automatically. A refund is different: it moves
money. If we send a refund request and the reply never arrives, we cannot tell whether
the refund happened. Sending it again "just in case" relies entirely on the provider
honouring our idempotency key; if it does not (bug, expired key, different provider),
the customer is refunded twice.

Not every failure is uncertain, though:

- **Connect failure** (`ConnectError`, `ConnectTimeout`, `PoolTimeout`): we never
  reached the provider, so nothing was sent and no refund can exist. This is safe to
  report as a plain temporary failure.
- **Read timeout, connection dropped after sending, any 5xx, a 504**: the request may
  have arrived and been processed before things went wrong. The outcome is unknown.

### Decision

`PaymentClient.create_refund` makes exactly one attempt and never retries.

- A connect failure raises `ProviderUnavailableError` with `request_sent=False`.
- A read timeout, a dropped connection, a 5xx, or a 2xx we cannot parse raises
  `OutcomeUnknownError`, carrying the idempotency key.
- A 4xx raises `ProviderRejectedError` (404 raises `ResourceNotFoundError`).

An unknown outcome is resolved only by **reconciliation**: ask the provider
`find_refund_by_key(idempotency_key)`. If a refund is found, use its status. If the
provider answers 404 (`ResourceNotFoundError`), no refund was created and a new attempt
with the same key is safe. The refund operation stays in `pending_reconciliation` until
then, and customers are never told the refund succeeded before it is verified.

### Alternatives considered

- Retry automatically with the same idempotency key — usually works, but makes our
  money safety depend completely on the provider's idempotency behaviour, and hides
  uncertainty from the workflow that must record and explain it.
- Treat every failure as "unknown" — safe, but a simple connect failure would then
  need a reconciliation round trip for no reason.
- Treat every failure as "failed" — wrong and dangerous: the refund may have happened,
  and a later fresh attempt could pay out twice.

### Consequences

- No double refunds from our retry logic, even if provider idempotency fails.
- The refund executor (M8) must handle `OutcomeUnknownError` by moving the operation
  to `pending_reconciliation` and scheduling reconciliation, rather than retrying.
- Some refunds finish later than they could have, after a reconciliation step.
- The distinction relies on httpx's exception types; anything we cannot classify as
  "definitely not sent" is treated as unknown.

---

## ADR-005: The refund window comes from the policy version in force when the order was placed

- **Date:** 2026-11-12
- **Status:** Accepted
- **Milestone:** M4

### Context

Refund policies are versioned. Refund Policy v1 (orders placed up to and including
2026-06-30) gives a 15-day return window; Refund Policy v2 (orders placed from
2026-07-01) gives 30 days. Each version's window is stored in its front matter as
`refund_window_days` and ingested into `policy_documents.refund_window_days`.

Today `check_eligibility` uses one number for every order: `Settings.refund_window_days`
(30). Once retrieval picks the policy version by order date (FR-5), an order placed in
June 2026 would be shown v1's 15-day rule while the code applied 30 days. The cited
policy and the decision would disagree, which breaks "customer-facing messages state
only verified outcomes" and makes reviewer evidence misleading.

### Decision

The refund window used by eligibility must come from the refund policy version in force
on the order's `placed_at` date (`effective_from <= placed_at date <= effective_to`, or
`effective_to` is NULL). This is implemented in M4 Task 2, together with version-aware
retrieval, so that the policy cited and the window applied always come from the same
document. Eligibility is not changed in M4 Task 1.

### Alternatives considered

- Keep one window in config for all orders — simple, but contradicts the cited policy
  for any order placed under v1.
- Use the version in force on the delivery date or the return request date — the PRD
  (FR-5) ties the policy to the order date, and a customer's terms should be fixed when
  they buy.

### Consequences

- Eligibility will need the window as an input looked up per order, not a global setting.
- An order whose `placed_at` date matches no refund policy version has no applicable
  policy, so the case escalates rather than inventing a window (PRD "Failure handling").
- Until Task 2 ships, all seeded orders are recent (placed after 2026-07-01), so they
  fall under v2 and the current 30-day setting matches. A unit test keeps
  `refund_policy_v2.md`'s `refund_window_days` equal to `Settings.refund_window_days`.
