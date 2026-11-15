// Where the login session lives between page loads (ADR-019).
//
// sessionStorage: kept across a reload of this tab, gone when the tab closes, never shared
// with other tabs, never sent to a server by the browser. The trade-off: any script running on
// the page could read it, so an XSS bug would expose the token. We avoid such bugs (React
// escapes all text, no raw HTML anywhere, a strict CSP) and tokens expire within the hour.
// An httpOnly cookie would hide it from scripts, but needs CSRF protection and a cookie flow
// the API does not have.

import type { Me } from "../api/types";

const KEY = "support-agent.session";

// The token in memory: what the API client sends. Kept outside React so every request reads
// the current value, and a logout applies at once.
let memoryToken: string | null = null;

export function currentToken(): string | null {
  return memoryToken;
}

// Only for the moment between POST /auth/login and GET /me, before the session is saved.
export function setPendingToken(token: string | null): void {
  memoryToken = token;
}

export interface Session {
  token: string;
  user: Me;
  expiresAt: number; // epoch milliseconds
}

function isSession(value: unknown): value is Session {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<Session>;
  return (
    typeof candidate.token === "string" &&
    typeof candidate.expiresAt === "number" &&
    typeof candidate.user === "object" &&
    typeof candidate.user.role === "string"
  );
}

export function loadSession(now: number = Date.now()): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!isSession(parsed) || parsed.expiresAt <= now) {
      sessionStorage.removeItem(KEY);
      return null;
    }
    memoryToken = parsed.token;
    return parsed;
  } catch {
    return null; // storage blocked or damaged: start logged out
  }
}

export function saveSession(session: Session): void {
  memoryToken = session.token;
  try {
    sessionStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    // storage blocked: the session still works in memory until the page reloads
  }
}

export function clearSession(): void {
  memoryToken = null;
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // nothing stored
  }
}
