// Holds the logged-in user and gives every page an API client that sends their token. The
// token lives in memory and is mirrored to sessionStorage (sessionStore.ts), so a reload keeps
// the session but closing the tab ends it.

import { useCallback, useMemo, useState, type ReactNode } from "react";

import { createApiClient } from "../api/client";
import type { Me, TokenResponse } from "../api/types";
import { AuthContext, type AuthValue } from "./auth";
import {
  clearSession,
  currentToken,
  loadSession,
  saveSession,
  setPendingToken,
  type Session,
} from "./sessionStore";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(() => loadSession());

  const logout = useCallback(() => {
    clearSession();
    setSession(null);
  }, []);

  // Any 401 means the token is no longer valid: log out (the routes then show the login page).
  const api = useMemo(() => createApiClient(currentToken, logout), [logout]);

  const login = useCallback(
    async (email: string, password: string): Promise<Me> => {
      const token = await api.request<TokenResponse>("/auth/login", {
        method: "POST",
        body: { email, password },
      });
      setPendingToken(token.access_token);
      try {
        // Who we are comes from the API (the token's subject), never from the form.
        const user = await api.request<Me>("/me");
        const next: Session = {
          token: token.access_token,
          user,
          expiresAt: Date.now() + token.expires_in * 1000,
        };
        saveSession(next);
        setSession(next);
        return user;
      } catch (error) {
        clearSession();
        throw error;
      }
    },
    [api],
  );

  const value = useMemo<AuthValue>(
    () => ({ user: session?.user ?? null, api, login, logout }),
    [session, api, login, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
