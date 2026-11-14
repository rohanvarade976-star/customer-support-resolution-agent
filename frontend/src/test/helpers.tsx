// Test helpers: a fake API behind fetch, a logged-in session, and rendering the real app at a URL.

import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { API_BASE_URL } from "../api/client";
import type { Me } from "../api/types";
import { App } from "../App";
import { AuthProvider } from "../auth/AuthProvider";
import { saveSession } from "../auth/sessionStore";

export interface RecordedCall {
  method: string;
  path: string;
  body: unknown;
  authorization: string | null;
}

// Handlers keyed "METHOD /path" (the path includes any query string). Each returns a
// Response, or throws to simulate a network failure.
export type Routes = Record<string, (call: RecordedCall) => Response | Promise<Response>>;

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// A text/event-stream response. With `keepOpen`, the stream never ends (a live connection).
export function sseResponse(frames: string[], keepOpen = false): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      if (!keepOpen) controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

export function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

export function fakeApi(routes: Routes): RecordedCall[] {
  const calls: RecordedCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const path = input.replace(API_BASE_URL, "");
      const method = init.method ?? "GET";
      const headers = new Headers(init.headers);
      const call: RecordedCall = {
        method,
        path,
        body: typeof init.body === "string" ? JSON.parse(init.body) : null,
        authorization: headers.get("Authorization"),
      };
      calls.push(call);
      const handler = routes[`${method} ${path}`];
      if (handler === undefined) return json({ success: false, error_code: "NOT_FOUND", message: "Not found." }, 404);
      return handler(call);
    }),
  );
  return calls;
}

export const PRIYA: Me = { id: "c-1", role: "customer", name: "Priya", email: "priya@example.com" };
export const ANITA: Me = { id: "s-1", role: "reviewer", name: "Anita", email: "anita@example.com" };

export function logInAs(user: Me): void {
  saveSession({ token: `token-${user.id}`, user, expiresAt: Date.now() + 3_600_000 });
}

export function renderApp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}
