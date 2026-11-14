// Small shared pieces: an error banner, a status badge, a page heading, a card.

import type { ReactNode } from "react";

import { humanize } from "../lib/labels";

export function ErrorBanner({ message }: { message: string | null }) {
  if (message === null) return null;
  return (
    <p role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {message}
    </p>
  );
}

const TONES: Record<string, string> = {
  good: "bg-emerald-100 text-emerald-800",
  wait: "bg-amber-100 text-amber-800",
  bad: "bg-red-100 text-red-800",
  neutral: "bg-slate-200 text-slate-700",
};

const STATUS_TONE: Record<string, keyof typeof TONES> = {
  completed: "good",
  resolved: "good",
  succeeded: "good",
  approved: "good",
  delivered: "good",
  awaiting_customer: "wait",
  awaiting_approval: "wait",
  pending: "wait",
  in_progress: "wait",
  processing: "wait",
  submitted: "wait",
  pending_reconciliation: "wait",
  reserved: "wait",
  escalated: "bad",
  failed: "bad",
  rejected: "bad",
  expired: "bad",
};

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const tone = TONES[STATUS_TONE[status] ?? "neutral"];
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${tone ?? ""}`}>
      {label ?? humanize(status)}
    </span>
  );
}

export function PageTitle({ children }: { children: ReactNode }) {
  return <h1 className="mb-4 text-xl font-semibold">{children}</h1>;
}

export function Card({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <section className="mb-4 rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      {title !== undefined && <h2 className="mb-2 font-medium text-slate-800">{title}</h2>}
      {children}
    </section>
  );
}

export function Loading() {
  return <p className="text-sm text-slate-500">Loading…</p>;
}

export const buttonClass =
  "rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50";
export const secondaryButtonClass =
  "rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50";
export const inputClass =
  "w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none";
