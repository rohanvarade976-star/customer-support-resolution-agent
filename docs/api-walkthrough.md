# API walkthrough (PowerShell)

Hands-on tour of the API: orders and tickets, a refund from proposal to approval to execution,
and what happens after each decision. Needs the API (and, for refunds, the mock payments provider)
running: see [running-locally.md](running-locally.md) or [docker.md](docker.md).

### Orders and tickets

Customers see only their own orders and tickets. Asking for someone else's order or ticket
returns `404`, the same as one that does not exist. Reviewers get `403` on the customer-only
routes (`/me/orders`, `POST /tickets`) and can list and read all tickets.

Log in first. The password is your `DEMO_PASSWORD`:

```powershell
$body = @{ email = "priya@example.com"; password = "<your DEMO_PASSWORD>" } | ConvertTo-Json
$login = Invoke-RestMethod -Method Post -Uri http://localhost:8000/auth/login `
  -ContentType "application/json" -Body $body
```

Every error has the same shape: `{"success": false, "error_code": "...", "message": "..."}`.

```powershell
$h = @{ Authorization = "Bearer $($login.access_token)" }   # Priya's token

# Priya's orders (items and returns included), then one order
Invoke-RestMethod -Uri http://localhost:8000/me/orders -Headers $h
Invoke-RestMethod -Uri http://localhost:8000/me/orders/ORD-1001 -Headers $h

# Create a ticket (order_number is optional; it must be one of your own orders)
$body = @{ order_number = "ORD-1001"; message = "My headphones stopped working" } | ConvertTo-Json
$ticket = Invoke-RestMethod -Method Post -Uri http://localhost:8000/tickets -Headers $h `
  -ContentType "application/json" -Body $body

# List your tickets (limit 1-100, default 20; offset default 0), then fetch one
Invoke-RestMethod -Uri "http://localhost:8000/tickets?limit=10&offset=0" -Headers $h
Invoke-RestMethod -Uri "http://localhost:8000/tickets/$($ticket.id)" -Headers $h
```

Log in as `anita@example.com` and call `GET /tickets` to see every customer's tickets,
including `customer_id` and `customer_name`.

### Refunds: proposal, human approval, execution

A reviewer turns a ticket into a refund proposal. Code decides eligibility and the amount
(the request has no body, so no amount can be sent), stores the proposal once with a
SHA-256 hash, and opens an approval that expires after `APPROVAL_TTL_MINUTES` (default 60).
Approving it issues the refund exactly once through the payment provider. Customers get
`403` on all three routes.

| Endpoint | Purpose |
|---|---|
| `POST /tickets/{ticket_id}/refund-proposals` | Check eligibility and create the proposal and pending approval |
| `GET /approvals` | Pending, unexpired approvals with order, customer, amount, reasons and expiry |
| `POST /approvals/{approval_id}/decision` | `{"decision": "approve" \| "reject", "note": "..."}` |

Start the mock payments provider first (`uv run uvicorn mock_providers.payments.app:create_app --factory --port 8001`
in its own terminal), then run Priya's refund end to end:

```powershell
$api = "http://localhost:8000"
function Login($email) {
  $body = @{ email = $email; password = "<your DEMO_PASSWORD>" } | ConvertTo-Json
  $token = (Invoke-RestMethod -Method Post -Uri "$api/auth/login" -ContentType "application/json" -Body $body).access_token
  @{ Authorization = "Bearer $token" }
}
$priya = Login "priya@example.com"
$anita = Login "anita@example.com"

# 1. Priya opens a ticket about ORD-1001 (returned, inspection passed)
$body = @{ order_number = "ORD-1001"; message = "I returned my headphones, please refund" } | ConvertTo-Json
$ticket = Invoke-RestMethod -Method Post -Uri "$api/tickets" -Headers $priya -ContentType "application/json" -Body $body

# 2. Anita (reviewer) creates the proposal: amount 249900 paise comes from the rules
$proposal = Invoke-RestMethod -Method Post -Uri "$api/tickets/$($ticket.id)/refund-proposals" -Headers $anita
$proposal

# 3. The approval queue
(Invoke-RestMethod -Uri "$api/approvals" -Headers $anita).items | Format-List

# 4. Approve: the refund is reserved, then sent to the payment provider once
$body = @{ decision = "approve"; note = "Return verified" } | ConvertTo-Json
$result = Invoke-RestMethod -Method Post -Uri "$api/approvals/$($proposal.approval_id)/decision" `
  -Headers $anita -ContentType "application/json" -Body $body
$result.refund      # status "submitted"
$result.message     # states only what the provider confirmed

# 5. Approving again is refused (409 ALREADY_DECIDED); still one refund
try { Invoke-RestMethod -Method Post -Uri "$api/approvals/$($proposal.approval_id)/decision" `
  -Headers $anita -ContentType "application/json" -Body $body } catch { $_.ErrorDetails.Message }

# 6. The provider has exactly one refund for this key
Invoke-RestMethod "http://localhost:8001/refunds?idempotency_key=refund-$($proposal.proposal_id)"
```

Ineligible orders are refused with `409 NOT_ELIGIBLE` and their reason codes in
`details.reasons`, for example Rahul's `ORD-1004` (`NO_RETURN`, `OUTSIDE_RETURN_WINDOW`),
Vikram's `ORD-1006` (`INSPECTION_FAILED`) and Kavya's `ORD-1005` (`ALREADY_REFUNDED`).

What happens after a decision:

| Outcome | Refund | Run | Ticket |
|---|---|---|---|
| Rejected | none | `completed` | `open` (needs follow-up; note kept in the audit event) |
| Approval expired | none | `escalated` | `open` (create a new proposal) |
| Provider accepted | `submitted` / `processing` | `completed` | `in_progress` (resolved only on `succeeded`) |
| Reply lost (`drop_response`) | `pending_reconciliation` | `executing` | `in_progress` |
| Provider unreachable or rejected | `failed` | `failed` | `escalated` |

To see a lost reply, switch on the fault before approving: the refund stays
`pending_reconciliation`, and the provider still finds it by its key.

```powershell
Invoke-RestMethod -Method Post http://localhost:8001/_admin/faults -ContentType "application/json" `
  -Body '{"mode": "drop_response", "count": 1}'
```

Every step is in the audit trail:

```powershell
docker compose exec postgres psql -U support -d support_agent -c `
  "SELECT event_type, actor, created_at FROM audit_events ORDER BY created_at DESC LIMIT 15;"
```
