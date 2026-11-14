// Toasts: short notes that confirm an action ("Reply sent"), bottom-right on desktop and
// bottom-centre on phones. Each closes itself after a few seconds or with its X button.
//
// Our own ~70 lines instead of a library: shadcn's toast (Sonner) adds its CSS with a
// <style> tag at runtime, which our Content-Security-Policy blocks (ADR-020).
// The list is an aria-live region, so screen readers read each new toast out.

import { CircleCheck, CircleExclamationPoint, Info, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { cn } from "../../lib/utils";
import { ToastContext, type ToastApi, type ToastTone } from "./toast-context";

const DURATION_MS = 5000;

interface ToastItem {
  id: number;
  tone: ToastTone;
  title: string;
  description: string | undefined;
}

const ICONS = { success: CircleCheck, error: CircleExclamationPoint, info: Info };
const ICON_COLOURS = { success: "text-success-600", error: "text-danger-600", info: "text-primary-600" };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    setToasts((list) => list.filter((toast) => toast.id !== id));
    clearTimeout(timers.current.get(id));
    timers.current.delete(id);
  }, []);

  const show = useCallback(
    (tone: ToastTone, title: string, description?: string) => {
      const id = nextId.current++;
      setToasts((list) => [...list.slice(-2), { id, tone, title, description }]); // at most 3
      timers.current.set(
        id,
        setTimeout(() => {
          dismiss(id);
        }, DURATION_MS),
      );
    },
    [dismiss],
  );

  // Stop any pending timers when the app unmounts.
  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) clearTimeout(timer);
    };
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      success: (title, description) => {
        show("success", title, description);
      },
      error: (title, description) => {
        show("error", title, description);
      },
      info: (title, description) => {
        show("info", title, description);
      },
    }),
    [show],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        role="status"
        aria-live="polite"
        className="pointer-events-none fixed inset-x-0 bottom-0 z-[60] flex flex-col items-center gap-2 p-4 sm:items-end sm:p-6"
      >
        {toasts.map((toast) => {
          const Icon = ICONS[toast.tone];
          return (
            <div
              key={toast.id}
              className="pointer-events-auto flex w-full max-w-sm animate-toast-in items-start gap-3 rounded-xl border border-gray-200 bg-white p-4 shadow-lg"
            >
              <Icon className={cn("mt-0.5 size-5 shrink-0", ICON_COLOURS[toast.tone])} aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-gray-900">{toast.title}</p>
                {toast.description !== undefined && (
                  <p className="mt-0.5 text-sm text-gray-600">{toast.description}</p>
                )}
              </div>
              <button
                type="button"
                onClick={() => {
                  dismiss(toast.id);
                }}
                aria-label="Dismiss notification"
                className="-m-1 grid size-7 shrink-0 cursor-pointer place-items-center rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-700"
              >
                <X className="size-4" aria-hidden="true" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
