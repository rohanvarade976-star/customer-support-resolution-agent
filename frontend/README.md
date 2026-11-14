# Frontend

React + TypeScript + Vite + Tailwind. A thin client: it shows what the API returns and never
computes eligibility, amounts or decisions. Design: ADR-019 in `docs/decisions.md`.

## Pages

| Path | Who | What |
|---|---|---|
| `/login` | everyone | Log in with a demo account; the role comes from `GET /me` |
| `/orders` | customer | Your orders, with "Raise a complaint" |
| `/complaints/new` | customer | Create a ticket and start its run, then open the run |
| `/tickets` | customer | Your tickets, their status and a link to each one's latest run |
| `/runs/:runId` | owner, reviewer | Live status (SSE), the verified reply, a reply box when asked |
| `/memory` | customer | What is remembered about past cases; delete all |
| `/approvals` | reviewer | Pending refund approvals |
| `/approvals/:approvalId` | reviewer | Evidence, policy citations, the stored amount; approve or reject; the refund's status |

## Layout

```
src/
  api/        client.ts (fetch + token + errors), sse.ts (stream parser), types.ts (API shapes)
  auth/       auth.ts (context, useAuth, roles), AuthProvider.tsx, sessionStore.ts (token storage)
  hooks/      useApiGet.ts (load one endpoint), useRunStream.ts (SSE with polling fallback)
  lib/        money.ts (integer paise to text), labels.ts (fixed wording for codes)
  components/ Layout.tsx (header, nav, logout), ui.tsx (small shared pieces)
  pages/      one file per page, plus tests for login, the run page and the approval case
  test/       setup.ts (DOM matchers), helpers.tsx (fake API, render the app at a URL)
```

## Commands (PowerShell, from `frontend/`)

```powershell
npm ci                  # install exactly what package-lock.json lists
Copy-Item .env.example .env.local   # optional: VITE_API_BASE_URL (default http://localhost:8000)
npm run dev             # http://localhost:5173 (the API must be running, see the root README)
npm run lint            # ESLint
npm run typecheck       # TypeScript, strict
npm test                # Vitest + React Testing Library
npm run build           # type check, then the production build in dist/
```

Needs Node 22.13 or newer. The API's `CORS_ALLOWED_ORIGINS` must include the page's origin
(`http://localhost:5173` and `http://localhost:3000` are the defaults).
