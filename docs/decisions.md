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

### Implementation (2026-11-12, M4 Task 2)

This note records how the decision above was built. The decision itself is unchanged.

- `services/eligibility.load_order` looks up the `refund_policy` version in force on the
  order's `placed_at` date and puts its `refund_window_days` into `OrderFacts`. Proposals,
  refund execution and the `check_refund_eligibility` tool all go through `load_order`, so
  they cannot apply different windows.
- The order's date is taken in **UTC** (`domain/policies.policy_date`), so the same order
  always maps to the same policy version whatever the server's time zone is.
- `check_eligibility(facts, now)` no longer takes a window argument. When
  `refund_window_days` is `None` (no refund policy in force on that date), the decision
  is not eligible with the new reason code **`NO_APPLICABLE_POLICY`**, reported first.
  The window check is skipped (nothing is invented); the other checks (delivery, return,
  balance) are still reported, so a reviewer sees the full picture.
- **`Settings.refund_window_days` was removed.** Keeping it would leave a second, stale
  copy of the window that code could start using again by accident: exactly the mismatch
  this ADR exists to prevent. The refund window now has one source: each refund policy
  file's front matter, ingested into `policy_documents.refund_window_days`.
- **The consistency test changed purpose.** It used to check v2's front matter against
  the setting. It now checks, for every refund policy file, that the prose states the same
  number of days as that file's own front matter (v1: 15, v2: 30), because the front
  matter is now what eligibility uses.
- Consequence: eligibility now needs the policy documents in the database. A fresh
  environment must run `scripts/ingest_policies.py` after migrating, or every order is
  `NO_APPLICABLE_POLICY` (safe: nothing is refunded, cases escalate).

---

## ADR-006: Keyword search keeps all-words matching

- **Date:** 2026-11-12
- **Status:** Accepted
- **Milestone:** M4

### Context

Policy retrieval is hybrid (M4 Task 2): a vector search and a Postgres full-text keyword
search, merged with Reciprocal Rank Fusion (k=60). The keyword query is built with
`websearch_to_tsquery`, which requires **every** word of the query to appear in a
section. On the retrieval eval (`evals/datasets/retrieval_questions.json`, 15 questions,
real embedder BAAI/bge-small-en-v1.5), full customer questions almost never matched every
word, so the keyword search returned nothing and the ranking came from the vector search
alone. That looked like a missed opportunity, so we tried matching **any** word.

### Experiment

The customer's text was reduced to its words (letters and digits only, so quotes and `-`
could not act as operators) and joined with `or` before `websearch_to_tsquery`, e.g.
"How long to return?" became `How or long or to or return`. Nothing else changed.

| | All words (baseline) | Any word (experiment) |
|---|---|---|
| hit@1 | 12/15 (80%) | 12/15 (80%) |
| hit@3 | 15/15 (100%) | 15/15 (100%) |
| Keyword search contributed | never | on most questions |

- **Fixed** (now first): `rahul-exception` (`escalation_exceptions_v1:section_2`) and
  `escalation-when` (`escalation_exceptions_v1:section_3`).
- **Broke** (no longer first, still in the top 3): `vikram-failed-inspection`
  (`damaged_products_v1:section_5`, beaten by `return_processing_v1:section_4`) and
  `aditya-inspection-pending` (`return_processing_v1:section_4`, beaten by
  `refund_policy_v2:section_4`).
- `meera-how-to-return` missed in both runs.

Off-topic rejection was unchanged. On the full `search_policy` service, pizza, washing
machines in Germany and the weather returned no relevant evidence either way.
"Can I pay with cryptocurrency?" returned three weak passages (similarity 0.55–0.58)
either way. The threshold is applied to vector similarity, which this change does not
touch.

### Why it did not help

The policies share one domain vocabulary: return, refund, order, inspection, delivered.
With any-word matching, almost every section matches some word of almost every question,
so the keyword ranking mostly echoes the vector ranking. Where it differs, a frequent word
such as "inspection" pulls a section that mentions it often above the section that
actually answers the question. The result reshuffles close calls in both directions
instead of improving them.

### Decision

Keep all-words matching (`websearch_to_tsquery` on the raw query). In practice the keyword
search then contributes only when a question uses a section's exact terms. This is cheap,
safe against syntax errors, and costs nothing on the eval. The rule we set before the
experiment was to adopt it only if hit@1 improved without making off-topic rejection
worse. It did not improve hit@1, so the change was reverted.

### Alternatives considered

- Any-word (OR) matching — tried above; same hit@1, different misses.
- Dropping keyword search entirely — would give the same results on this eval, but keeps
  no way to catch exact terms (reason codes, "UPI", product words) that the small
  embedding model may blur. It costs little to keep.

### Consequences and follow-ups

Retrieval quality currently rests on the vector search. Two options to evaluate, on a
larger question set than 15, before changing anything:

1. **Weight the vector ranking higher in fusion** (weighted RRF), so keyword matches
   break ties instead of overriding a clearly better meaning match.
2. **Raise `POLICY_MIN_SIMILARITY` to about 0.6**, so weak matches such as the
   cryptocurrency question report no relevant evidence. Check that this does not drop
   correct answers (the lowest correct first-place similarity on the current set must
   stay above it).

Fifteen questions are too few to separate small changes: one question is about 7
percentage points of hit@1. The eval set should grow before either option is judged.

---

## ADR-007: The LLM client: forced tool calls for structured output, one retry loop, Groq then Gemini

- **Date:** 2026-11-12
- **Status:** Accepted
- **Milestone:** M5

### Context

Every agent needs to call an LLM. The free tiers rate-limit often, providers have
outages, and models sometimes return output that does not fit the shape we asked for.
The PRD requires: back off, fall back to the secondary provider, then pause ("Failure
handling"); one repair attempt for invalid output; every model output checked against a
Pydantic schema; providers and models swappable through configuration; and token usage
recorded per call. Customer text and model output must never reach logs.

### Decision

1. **Our own interface.** Agents depend on `LLMClient` (`llm/client.py`) with
   `chat(messages, tools)` and `structured(messages, Schema)`, and our own message and
   result types (`llm/types.py`). LangChain's model layer (`ChatGroq`,
   `ChatGoogleGenerativeAI`, `bind_tools`) is used only inside the adapters
   (`llm/providers.py`).
2. **Structured output is a forced tool call.** The schema is offered as one tool with a
   snake_case name (`TriageResult` -> `triage_result`) and the model must call it. We
   validate the arguments with Pydantic ourselves. If they are invalid, the model gets one
   repair message listing field locations and rules (not the invalid values). If they are
   still invalid, `StructuredOutputError`.
3. **One retry loop.** LangChain's and the SDKs' own retries are switched off. Our client
   retries rate limits and transient errors (timeouts, connection failures, 5xx) up to
   `LLM_MAX_ATTEMPTS` per provider with full-jitter exponential backoff, honouring
   Retry-After and capped at `LLM_BACKOFF_MAX_SECONDS`. Then it moves to the next provider:
   Groq first, Gemini only if `GEMINI_API_KEY` is set. When all fail, `LLMUnavailableError`
   (the workflow will pause the run, M7). Bad requests, bad keys and unknown models are
   not retried and do not fall back: retrying cannot fix them.
4. **Errors carry no content.** Adapters raise our errors `from None`. Groq's error body
   includes `failed_generation` (the model's output, so possibly customer text); chaining
   the SDK error would put it into any logged traceback. Our messages hold only the HTTP
   status, Groq's short error code, or an exception class name.
5. **Usage log.** One `llm_call` line per attempt: provider, model, tier, attempt,
   outcome, input/output/reasoning tokens, latency, and the error class on failure.
6. **Models (config, not code).** Groq small `openai/gpt-oss-20b`, large
   `openai/gpt-oss-120b`, with `GROQ_REASONING_EFFORT=low`. Gemini small
   `gemini-3.5-flash-lite`, large `gemini-3.6-flash`.

### Alternatives considered

- `with_structured_output` from LangChain: its parsing and retries happen out of sight,
  and its default method differs by provider. We want our own validation, exactly one
  visible repair, and the same mechanism on both providers.
- JSON mode / JSON-schema response format: not supported by every Groq model, while tool
  calling is supported by every model we would pick for agents.
- The Llama models (`llama-3.1-8b-instant`, `llama-3.3-70b-versatile`), the original plan:
  Groq's docs still list them as production, but our API key's model list does not
  include them and calls return 404 `model_not_found`.
- A retry library (tenacity): a dozen lines of our own loop are easy to read and log, and
  match the provider clients from M2.

### Consequences

- Agents and tests never import LangChain; switching providers or models is config only.
- Tests use the scripted `FakeProvider` underneath the real client, so retry, fallback and
  repair logic is tested without any network call.
- **gpt-oss models reason before answering, and reasoning counts as output tokens.** At
  the default effort a 60-token limit was used up by reasoning and the answer came back
  empty. Keep `GROQ_REASONING_EFFORT=low` unless an eval shows a step needs more, and give
  every call enough `max_tokens` for reasoning plus the answer. Reasoning tokens are
  logged separately to make this visible.
- gpt-oss models do not make parallel tool calls; the investigator (M5) calls tools one
  at a time, which also keeps its step limit simple.
- Temperature 0 is not fully deterministic on gpt-oss (5 runs gave 2 different answers in
  the playground). Tests must never depend on the real model; evals must allow for this.
- The Gemini fallback is untested against the real API until a `GEMINI_API_KEY` is added.
- Dropping the SDK error cause makes some provider failures harder to debug. The HTTP
  status and Groq's error code are kept for that reason.
