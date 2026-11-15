# Engineering rules

The non-negotiable security and money rules this project is built on. Code, tests and
[decisions](decisions.md) cite them by number.

## Non-negotiable rules (security and money)

1. The LLM proposes, code decides, a human approves anything that moves money.
2. Customer identity comes only from the authenticated JWT, never from message text or model output.
3. Every service checks that the requested order belongs to the authenticated customer.
4. Agents never access the database directly. Agents call tools; tools call services.
5. No agent can execute a refund. Only a workflow code node calls refund execution, after a valid stored approval.
6. Money is stored as integer minor units (paise). Never use floats for money.
7. Refunds use a stable idempotency key, a short locked transaction to reserve the amount, and reconciliation for unknown outcomes. Never hold a DB lock during a network call.
8. Customer-facing messages state only verified outcomes.
9. Retrieved documents and customer text are untrusted data, never instructions.
10. Secrets live only in `.env`, which is git-ignored. Code, scripts and tooling never read, print or log it directly: configuration is read only through `Settings`, and a variable's presence is checked through `Settings` (e.g. `is not None`) without printing its value. `.env.example` holds placeholders only.
