# Mock services

Fake third-party providers used in development and tests. Run from `backend/`.

Fake third-party providers used in development and tests. Run each one in its own
terminal. Each has interactive API docs at `/docs`.

| Provider | Port | Storage | Start command |
|---|---|---|---|
| Payments | 8001 | Postgres schema `mock_payments` | `uv run uvicorn mock_providers.payments.app:create_app --factory --port 8001` |
| Shipping | 8002 | In memory, from `data/mock_shipping/shipments.json` | `uv run uvicorn mock_providers.shipping.app:create_app --factory --port 8002` |
| Ticketing | 8003 | Postgres schema `mock_ticketing` | `uv run uvicorn mock_providers.ticketing.app:create_app --factory --port 8003` |

Payments and ticketing need Postgres running (`docker compose up -d`) and create their
own schema on startup. Shipping needs no database; its dates are written as "days ago"
so the demo data stays current.

Try them:

```powershell
# Arjun's delayed laptop
Invoke-RestMethod http://localhost:8002/shipments/ORD-1002

# Create a ticket (writes need an Idempotency-Key; reusing it returns the original)
$ticket = Invoke-RestMethod -Method Post http://localhost:8003/tickets -ContentType "application/json" `
  -Headers @{ "Idempotency-Key" = "demo-ticket-1" } `
  -Body '{"customer_reference": "cust_arjun", "order_reference": "ORD-1002", "subject": "Laptop late", "description": "In transit for 8 days"}'

# Add a note, then change the status (open, pending, resolved, closed)
Invoke-RestMethod -Method Post "http://localhost:8003/tickets/$($ticket.id)/notes" -ContentType "application/json" `
  -Headers @{ "Idempotency-Key" = "demo-note-1" } -Body '{"body": "Held at Nagpur hub."}'
Invoke-RestMethod -Method Patch "http://localhost:8003/tickets/$($ticket.id)" -ContentType "application/json" `
  -Body '{"status": "pending"}'
```

Failures can be switched on for the next N requests (not available when `APP_ENV=production`):

```powershell
Invoke-RestMethod -Method Post http://localhost:8001/_admin/faults -ContentType "application/json" `
  -Body '{"mode": "drop_response", "count": 1}'
```

Modes: `none`, `slow` (needs `delay_ms`), `error`, `drop_response`, plus `decline` on
payments only. `drop_response` does the work and then replies 504, as if the reply was lost.
