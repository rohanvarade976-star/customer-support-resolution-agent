// The toast context and the hook pages use: `const toast = useToast(); toast.success("Saved")`.
// The provider and the on-screen list are in toast.tsx.

import { createContext, useContext } from "react";

export type ToastTone = "success" | "error" | "info";

export interface ToastInput {
  title: string;
  description?: string;
}

export interface ToastApi {
  success: (title: string, description?: string) => void;
  error: (title: string, description?: string) => void;
  info: (title: string, description?: string) => void;
}

export const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const value = useContext(ToastContext);
  if (value === null) throw new Error("useToast must be used inside <ToastProvider>");
  return value;
}
