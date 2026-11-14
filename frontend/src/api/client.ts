// A small fetch wrapper: the API's base URL, the bearer token, JSON in and out, and the API's
// one error shape turned into an ApiError. No retries here: the caller decides.

import type { ApiErrorBody } from "./types";

// Built into the bundle by Vite. Not a secret: it is only where the API lives.
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfterSeconds: number | null;

  constructor(status: number, code: string, message: string, retryAfterSeconds: number | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

function isErrorBody(value: unknown): value is ApiErrorBody {
  return (
    typeof value === "object" &&
    value !== null &&
    "error_code" in value &&
    "message" in value &&
    typeof value.error_code === "string" &&
    typeof value.message === "string"
  );
}

// Turns a non-2xx response into an ApiError, using the API's error body when it has one.
export async function errorFromResponse(response: Response): Promise<ApiError> {
  const retryAfter = response.headers.get("Retry-After");
  const retryAfterSeconds = retryAfter === null ? null : Number(retryAfter);
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // not JSON (a proxy error page, for example)
  }
  if (isErrorBody(body)) {
    return new ApiError(response.status, body.error_code, body.message, retryAfterSeconds);
  }
  return new ApiError(response.status, "HTTP_ERROR", `Request failed (${response.status}).`, retryAfterSeconds);
}

export interface RequestOptions {
  method?: "GET" | "POST" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}

export interface ApiClient {
  request<T>(path: string, options?: RequestOptions): Promise<T>;
  // A raw response for streaming (Server-Sent Events). Throws ApiError on a non-2xx status.
  open(path: string, signal: AbortSignal): Promise<Response>;
}

// `getToken` is read on every call, so a logout takes effect at once. `onUnauthorized` runs
// on any 401: the session is over (expired or revoked token), so the app logs out.
export function createApiClient(
  getToken: () => string | null,
  onUnauthorized: () => void,
  baseUrl: string = API_BASE_URL,
): ApiClient {
  function headers(json: boolean): Headers {
    const result = new Headers();
    const token = getToken();
    if (token !== null) result.set("Authorization", `Bearer ${token}`);
    if (json) result.set("Content-Type", "application/json");
    return result;
  }

  async function check(response: Response): Promise<Response> {
    if (response.ok) return response;
    if (response.status === 401) onUnauthorized();
    throw await errorFromResponse(response);
  }

  return {
    async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
      const hasBody = options.body !== undefined;
      const init: RequestInit = { method: options.method ?? "GET", headers: headers(hasBody) };
      if (hasBody) init.body = JSON.stringify(options.body);
      if (options.signal) init.signal = options.signal;
      const response = await check(await fetch(`${baseUrl}${path}`, init));
      return (await response.json()) as T;
    },
    async open(path: string, signal: AbortSignal): Promise<Response> {
      const init: RequestInit = { headers: headers(false), signal };
      return check(await fetch(`${baseUrl}${path}`, init));
    },
  };
}

// A message a person can act on, for any error a call may throw.
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429 && error.retryAfterSeconds !== null) {
      return `${error.message} Try again in ${Math.ceil(error.retryAfterSeconds)} seconds.`;
    }
    return error.message;
  }
  return "The service could not be reached. Check your connection and try again.";
}
