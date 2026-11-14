// The auth context's shape and the helpers pages use. The provider is in AuthProvider.tsx.

import { createContext, useContext } from "react";

import type { ApiClient } from "../api/client";
import type { Me } from "../api/types";

export interface AuthValue {
  user: Me | null;
  api: ApiClient;
  login: (email: string, password: string) => Promise<Me>;
  logout: () => void;
}

export const AuthContext = createContext<AuthValue | null>(null);

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}

// Admins are the higher role, so they get the reviewer area too (as in the API).
export function isReviewer(user: Me | null): boolean {
  return user?.role === "reviewer" || user?.role === "admin";
}

export function homePathFor(reviewer: boolean): string {
  return reviewer ? "/approvals" : "/orders";
}
