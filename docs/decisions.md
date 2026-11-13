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
- **The investigator (M5) has six read tools, not the four the PRD first listed:** `get_order`, `get_return_status`, `get_payment_status`, `get_shipment`, `get_refundable_balance` and `check_refund_eligibility`. The last two let it report the refundable amount and the rule-based eligibility decision as cited facts instead of guessing them. It has no `search_policy` (the policy agent's tool) and no write tools. The allowlist is enforced twice: the model is only offered those schemas, and the tool loop refuses any other name with `TOOL_NOT_ALLOWED` before anything runs. The PRD agent table was updated to match.
- The Gemini fallback is untested against the real API until a `GEMINI_API_KEY` is added.
- Dropping the SDK error cause makes some provider failures harder to debug. The HTTP
  status and Groq's error code are kept for that reason.


## ADR-008: One demo anchor for the seed and the mock shipping provider; the policy agent's grounding

- **Date:** 2026-11-13
- **Status:** Accepted
- **Milestone:** M6

### Context

The database seed turned "N days ago" into dates once, when it ran. The mock shipping
provider did the same when it started. Run on different days, they disagreed (Meera's
order said delivered 2026-11-08, shipping said 2026-11-12). The policy agent also needed
a rule for trusting what a model cites.

### Decision

1. `DEMO_ANCHOR_DATE` (optional) pins both to noon UTC on that date. Unset, the seed uses
   the moment it runs and the shipping mock the moment it started, so they agree on the
   same UTC day. `scripts/reset_demo.py` rebuilds the data and reminds you to restart the
   mock. The mocks cannot import from `support_agent`, so the rule exists twice
   (`core/demo_anchor.py`, `mock_providers/common/clock.py`) and a test keeps them equal.
2. The policy agent (`agents/policy.py`) has one tool, `search_policy`. A rule is kept only
   if its citation is a chunk_key returned by a search in the same run; others are dropped
   and flagged. Policy versions come from the kept citations, and `no_relevant_policy` is
   forced on when no grounded rule remains. It states rules only: eligibility and amounts
   stay in code.
3. `ToolLoop.results` keeps successful calls' arguments and typed outputs, in memory only,
   so agents can ground claims. The content-free `trace` is still the only part to store.
4. The policy agent uses the `small` tier by default (`POLICY_AGENT_TIER`): the large model
   hit many rate limits in the live investigation. Quality is measured in M9.
   *Superseded by ADR-009: the findings step now runs on `large`.*

### Alternatives considered

- A fixed default anchor date: stable, but eligibility uses the real clock, so scenarios
  would silently stop matching the PRD.
- A shared data file for the anchor: more moving parts than one environment variable.

### Consequences

- With the anchor unset, restart the shipping mock after the UTC date changes.
- `reset_demo` deletes only what belongs to the seeded customers, plus audit events with
  no run and the mock tables. LangGraph checkpoint tables (M7) will need adding then.


## ADR-009: The policy agent's findings step runs on the large tier and names its fields

- **Date:** 2026-11-13
- **Status:** Accepted, with a known limitation (below)
- **Milestone:** M6

### Context

The first real run of the policy agent failed in all four scenarios with
`POLICY_OUTPUT_INVALID`. The search loop worked. The findings call, one forced tool call
returning `PolicyFindingsDraft`, always ended in `LLMInvalidOutputError` and the one repair
attempt failed the same way. The investigator's structured summary worked on the same models.

Groq's error (read in a local debug run only, never logged) was `tool_use_failed`:
`parameters for tool policy_findings_draft did not match schema: /rules/0: missing
properties: 'statement'`. The model had written each rule as `{"text": ..., "citation": ...}`
instead of `{"statement": ..., "citation": ...}`. Groq checks tool arguments against the
schema itself and rejects the call with a 400. One of the rules was also about 350
characters, over the schema's 300-character limit.

The two schemas have the same shape (one forced tool, `list[Model]` with a `statement`
field), and `max_tokens` is the same, so the difference is in the conversation and prompt,
and I did not find which part. The search results use `content`, not `text`, so the model was
not copying a field name from them.

### Experiments

The real post-search conversation for Rahul was captured once, then the findings call was
replayed 4-5 times per variant on Groq. The number is calls that returned a valid answer.

| Variant | Small (gpt-oss-20b) | Large (gpt-oss-120b) |
|---|---|---|
| Baseline (old instruction, 300-char cap) | 0/5 | 3/5 |
| 300-char cap removed | 0/5 | 5/5 |
| Instruction names the keys `statement` and `citation` | 0/5 | 5/5 |
| Step-1 prose note removed from the context | 0/5 | not run |

Small fails every time whatever we change. Large already worked 3 times in 5 and worked every
time once either the cap or the key names were changed. Five tries is a small sample, so
"5/5" means "no failure seen", not "reliable".

### Decision

1. Two configurable tiers: `POLICY_AGENT_SEARCH_TIER` (default `small`) for the cheap search
   loop, `POLICY_AGENT_FINDINGS_TIER` (default `large`) for the findings step. This replaces
   `POLICY_AGENT_TIER`. An old `POLICY_AGENT_TIER` line in `.env` is ignored.
2. The findings instruction and the prompt name the fields: each rule has exactly the keys
   `statement` and `citation`.
3. The statement limit goes from 300 to 500 characters. Passages are long and the model
   restates them closely.
4. A malformed answer still ends in `POLICY_OUTPUT_INVALID`, never an exception. The workflow
   escalates. Tests (fake LLM): findings use the large tier, the instruction names the
   fields, a rule with `text` instead of `statement` ends in `POLICY_OUTPUT_INVALID`.

### Alternatives considered

- Keep everything on small: it returned a valid answer 0 times out of 5 in every variant.
- Run the whole agent on large: the large model hit Groq rate limits in the live
  investigation (ADR-008), and the search loop does not need it.
- Plain-text findings parsed by code: it moves the problem into a parser and gives up
  schema validation.

### Known limitation

- **Unexplained:** why the small model writes `text` for `statement` even when the instruction
  names the keys. I did not find the cause. It may be a quirk of gpt-oss-20b with Groq's
  forced tool calls.
- **Untested hypotheses:** (a) the earlier `search_policy` tool call in the history confuses a
  forced call to a different tool, so a flattened history would work; (b) renaming the field
  (for example to `text`) would work; (c) plain-string rules would work; (d) a stricter
  provider mode would help. These runs were stopped when Groq's free-tier limit was hit.
- The large-tier results above come from one conversation (Rahul). The post-fix runs of all
  four scenarios happened while Groq was rate limited, so most of them were answered by the
  Gemini fallback (`gemini-3.5-flash-lite` for the search, `gemini-3.6-flash` for the
  findings), not by Groq large. So the fix is confirmed against Groq large only by the
  replay table and one live Groq run. M9 should measure the findings failure rate per tier.
- If the findings step fails on both providers, the run escalates to a human: safe, but slow.

### Consequences

- Each policy run makes one large-tier call (findings) per run, not zero. The cost is small.
- The investigator's structured steps are unchanged.


## ADR-010: Parallel branches, the extra investigation round, and what the baseline comparison shows

- **Date:** 2026-11-13
- **Status:** Accepted
- **Milestone:** M6

### Context

The multi-agent workflow runs the investigator and the policy agent at the same time
(PRD FR-8). Both read the database through tools. The supervisor may also leave a required
record missing, and the PRD asks for at most one extra investigation round. Separately, the
single-agent baseline (FR-23) exists so the two designs can be compared, and the comparison
is only useful if we say what it does and does not measure.

### Decision

1. **One short-lived database session per parallel branch.** A SQLAlchemy `AsyncSession` is
   not safe for concurrent use, so two branches must not share one. Each branch opens its
   own session from `WorkflowDeps.branch_sessions` and closes it when the branch ends. Each
   branch writes only its own state key (`investigation`, `policy`); `errors` is the one
   shared key and has an append reducer, so the merge is safe. Everything after the join
   (required evidence, eligibility, the proposal) uses the main session.
2. **The extra investigation round is triggered by code, at most once.** After the join, if
   a required lookup (`get_order`, and `get_shipment` where it applies) still has no
   successful result after code has fetched it itself, the graph runs the investigator one
   more time, told which records are missing. The supervisor and the model do not decide
   this, and a second extra round is impossible (the round is skipped once
   `investigation_retry` exists; baseline mode never has one). The second round's call ids
   are prefixed `r2_` before the two rounds are merged, because providers (and the test
   fakes) can reuse ids such as `call_1`, and the validator looks calls up by id.
3. **The baseline comparison has a caveat.** Baseline mode has no supervisor, no separate
   specialists, no critic and no revision. It shares everything that decides: required
   evidence, code-computed eligibility and delay facts, the validator, and the rule that
   only code sets the amount. So a difference in outcome, calls, tokens or latency between
   `multi_agent` and `baseline` reflects specialization (separate investigator and policy
   agent, supervisor hints) **and** the critic and revision loop together. It cannot say
   which of the two caused it. Isolating them needs a third configuration, for example
   multi-agent with the critic off, or baseline with one revision.

### Alternatives considered

- Share one session and serialise the branches: safe, but the two LLM-heavy branches would
  no longer overlap, which removes the point of FR-8.
- Let the supervisor ask for more rounds, or let the model say when evidence is missing:
  unbounded cost and a model deciding what counts as enough. Code decides, once.
- Give the baseline a critic so the comparison is like for like: then it is no longer the
  simple single-agent reference the PRD describes.

### Consequences

- Each parallel run holds two connections for its gathering step. Reads only: no locks, but
  a connection is open while the model is called.
- Integration tests cannot use two real sessions (one rolled-back transaction on one
  connection), so they hand every branch the test session one at a time. Real overlap with
  separate sessions is tested with fakes in `tests/unit/test_workflow_multi_agent.py`.
- M9's comparison report must state the caveat in item 3 next to any multi-agent versus
  baseline number.

---

## ADR-011: Durable pause and resume with the LangGraph Postgres checkpointer

- **Date:** 2026-11-13
- **Status:** Accepted
- **Milestone:** M7

### Context

A refund run must wait for a reviewer, and a run that needs more detail must wait for the
customer (PRD FR-14, "Workflow and state model"). Those waits can last minutes or days, and
the server may restart in between. Until M6 the graph ran start to finish in one call, so a
"waiting" run could never continue. Resuming also creates new risks: a double click, two
reviewers, or a retried request could resume the same run twice, and anything that moved
money before a pause could run again when the paused node is replayed.

### Decision

1. **LangGraph's official Postgres checkpointer** (`langgraph-checkpoint-postgres`) saves the
   run's state after every step, keyed by `workflow_runs.thread_id`. It needs psycopg 3 (plus
   `psycopg-pool`, and `psycopg[binary]` because Windows has no system libpq), a second
   Postgres driver. Only the checkpointer uses it, through its own small pool; the app stays
   on SQLAlchemy + asyncpg. Both use the same `DATABASE_URL`, with the driver prefix swapped.
2. **Setup at startup, under an advisory lock.** The app's lifespan runs the checkpointer's
   own idempotent `setup()` (it creates and migrates its tables) while holding
   `pg_advisory_lock`, so two processes starting together cannot race. Alembic ignores those
   tables (`CHECKPOINT_TABLES` in `db/base.py`, `include_object` in `migrations/env.py`).
   The pool uses `autocommit` (setup's `CREATE INDEX CONCURRENTLY` needs it) and
   `prepare_threshold=0` (Supabase's transaction pooler cannot keep prepared statements).
3. **A strict type allowlist for reading checkpoints.** The serializer revives only our state's
   classes and the registered tools' output classes, found by walking the type hints. Any
   other class in a checkpoint stays plain data and is never imported. A unit test fails if a
   full run would need a type outside the list. Checkpoints store a `StrEnum` as a plain
   string, so `restore_enums` types the top-level enum fields again after each read.
4. **Pause nodes have nothing before `interrupt()`.** `wait_for_approval` and
   `wait_for_customer` start with the `interrupt` call, because resuming replays the node from
   its first line. Side effects live in other nodes (`propose_refund` before the pause,
   `execute_refund` after it), whose results are checkpointed and never replayed.
5. **After a decision, the stored row decides, not the resume value.** The decision endpoint
   stores the decision first; `check_approval` reads it back. Approve goes to
   `execute_refund`, the only node that can move money, which calls the existing refund
   executor (revalidate, short locked reservation, one provider call with a stable
   idempotency key). Reject reopens the ticket; expired, changed or missing goes to
   escalation. The graph is given a `DisabledLLMClient` on this path, so no model call is
   possible after a decision. Manual (M3) runs keep their old path.
6. **A compare-and-set claim before every resume.** One `UPDATE ... WHERE status = <waiting>`
   moves the run out of its waiting state; exactly one request can match it. For a reviewer's
   decision (approve, reject or expiry) the run moves to `executing`, then ends as
   `completed`, `escalated` or `failed`. For a customer reply it moves to `investigating`. The
   claim and `run.resumed` (who resumed it: role and id, no content) are committed before the
   graph runs, so no lock is held during model or provider calls. A loser gets 409. The
   one-refund-per-proposal constraint and the executor's early return stay as a second net.
7. **Clarification is bounded.** `MAX_CLARIFICATION_ROUNDS` (default 2) questions; the next
   one escalates instead. After an unclear category or a missing order the run classifies
   again (an order number in the reply is still only a hint, checked for ownership); after
   the resolution's own question it gathers again, with the previous pass's evidence reset
   (so that pass may have its own one extra investigation round, ADR-010).
8. **Events are recorded per segment.** Each call runs one segment (start or resume, to the
   next pause or the end). Events come from what nodes wrote in that segment only, so a
   resumed run never repeats earlier events, and usage is added, not overwritten.
9. **Windows needs the selector event loop.** psycopg's async mode cannot run on Windows'
   default Proactor loop. The API runs with
   `--loop support_agent.core.event_loop:selector_loop_factory`, scripts use
   `core.event_loop.run`, and the integration tests use pytest-asyncio's loop-factory hook.
   Linux (CI, production) already uses a selector loop.

**Checkpoints are not a system of record.** They are the run's working memory: they hold the
complaint, the customer's replies and model output so a run can continue, and may be pruned.
Money, decisions, statuses and the audit trail live only in our own tables. Customer replies
are kept only in the checkpoint for now; a proper customer messages table, with ownership
and retention rules, is planned for the UI milestone (M10), where the conversation is shown.

### Alternatives considered

- Our own state table instead of a checkpointer: more code to resume mid-graph correctly,
  and it would duplicate what LangGraph already does.
- A checkpointer on asyncpg (community package): not official, and less maintained than the
  driver the official package is tested with.
- Trusting the resume value (`{"decision": "approve"}`): the value travels through code we
  do not control as tightly as a committed row; a stored, locked, hashed approval is the
  only thing allowed to authorise money.
- Locking the run row for the whole resume: simpler, but it would hold a database lock
  during LLM and provider calls (CLAUDE.md rule 7).
- LangGraph's permissive default serializer: it imports any class named in a checkpoint and
  warns that this will be blocked in a future version.

### Consequences

- The app needs the checkpointer at startup; setup runs on every start (cheap when nothing
  changed).
- A run that crashes after its claim but before its segment is saved stays claimed (or is
  marked failed). The background worker (M7 Task 2) and reconciliation (M8) pick these up.
- An approval that expires with no reviewer action leaves its run paused until a reviewer
  touches it; expiring idle approvals belongs with the worker.
- Each new type stored in the state must be reachable from `WorkflowState`'s type hints (or
  be a tool output), or the allowlist test fails. That is intended.
