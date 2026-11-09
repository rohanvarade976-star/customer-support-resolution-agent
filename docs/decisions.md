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
