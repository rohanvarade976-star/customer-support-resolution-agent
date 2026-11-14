# Frontend

React + TypeScript + Vite + Tailwind. A thin client: it shows what the API returns and never
computes eligibility, amounts or decisions. Design: ADR-019 (data, live status, security) and
ADR-020 (design system, CSP-safe components) in `docs/decisions.md`.

## Pages

| Path | Who | What |
|---|---|---|
| `/login` | everyone | Split screen; demo account cards fill in the email only. The role comes from `GET /me` |
| `/home` | customer | Greeting, ticket counts by status, recent requests and orders |
| `/complaints/new` | customer | "New request" wizard: order → what went wrong → describe → review. Creates the ticket, starts its run, opens the run |
| `/tickets` | customer | Your tickets, searchable and filtered by status, with each one's next action |
| `/orders` | customer | Your orders as cards, searchable, with "Get help" |
| `/runs/:runId` | owner, reviewer | Progress stepper, the verified reply, a reply box when asked, live indicator (SSE), timeline |
| `/memory` | customer | "Privacy and memory": what is remembered about past cases; delete all (confirmed) |
| `/approvals` | reviewer | Pending refund approvals: sortable table, time to expiry by urgency |
| `/approvals/:approvalId` | reviewer | Evidence and policy text on the left; the stored amount, approve or reject (confirmed), and the refund's status on the right |

## Layout

```
src/
  index.css   design tokens (colours, font, type scale, radii, shadows, focus ring)
  api/        client.ts (fetch + token + errors), sse.ts (stream parser), types.ts (API shapes)
  auth/       auth.ts (context, useAuth, roles), AuthProvider.tsx, sessionStore.ts (token storage)
  hooks/      useApiGet.ts (load one endpoint), useRunStream.ts (SSE with polling fallback),
              useNow.ts (a clock for "expires in")
  lib/        money.ts (integer paise to text), labels.ts (fixed wording for codes),
              runStages.ts (status -> stepper stage), time.ts, navigation.ts, orders.ts, utils.ts (cn)
  components/
    ui/       shadcn/ui-style building blocks: button, badge, card, form, table, alert, skeleton,
              dialog (native <dialog>), dropdown-menu (Radix), toast
    AppShell.tsx, Sidebar.tsx, TopBar.tsx   the frame around every signed-in page
    PageHeader.tsx, states.tsx (empty, error, skeletons), StatusBadge.tsx, ConfirmDialog.tsx,
    RunProgress.tsx, RunTimeline.tsx, TicketListItem.tsx
  pages/      one file per page, with tests for the key flows
  test/       setup.ts (DOM matchers, <dialog> and ResizeObserver stand-ins), helpers.tsx (fake API)
scripts/      screenshots.mjs (Playwright: every page at phone and desktop width)
```

## Design rules

- Use the tokens: `primary-*`, `gray-*`, `success-*`, `warning-*`, `danger-*`, `info-*`. Tailwind's
  default colours are switched off, so `bg-indigo-500` does not compile.
- Build pages from `components/ui` and the shared states: a skeleton while loading, an empty state
  with a next step, an error state with a retry.
- Never add a library that injects `<style>` tags at runtime: the CSP blocks them (ADR-020).
  Dialogs use the native `<dialog>`; the dropdown menu is non-modal; toasts are our own.

## Commands (PowerShell, from `frontend/`)

```powershell
npm ci                  # install exactly what package-lock.json lists
Copy-Item .env.example .env.local   # optional: VITE_API_BASE_URL (default http://localhost:8000)
npm run dev             # http://localhost:5173 (the API must be running, see the root README)
npm run lint            # ESLint
npm run typecheck       # TypeScript, strict
npm test                # Vitest + React Testing Library
npm run build           # type check, then the production build in dist/
npm run preview         # serve dist/ at http://localhost:4173 with the production CSP

npx playwright install chromium     # once: the browser for screenshots (not shipped)
npm run screenshots     # build, then capture every page into screenshots/ (git-ignored)
```

`npm run screenshots` needs no backend: it answers API calls from fixed fake data. It fails on
any CSP violation, console error, unknown API call, or a page wider than the screen.

Needs Node 22.13 or newer. The API's `CORS_ALLOWED_ORIGINS` must include the page's origin
(`http://localhost:5173` and `http://localhost:3000` are the defaults).
