// The customer's reply from the assistant, as a structured card: a greeting, the verified reply
// text, and a compact "What we found" summary of verified facts about the order. The reply was
// checked by the backend's output guardrail before it was saved; the facts come from our
// records (RunStatusEvent.facts). Both are shown as plain text (ADR-023).

import { MessageSquareText, Package, ShieldCheck, Truck, Undo2, Wallet, type LucideIcon } from "lucide-react";

import type { RunFacts } from "../../api/types";
import { foundFacts, type FoundIcon } from "../../lib/foundFacts";
import { Badge } from "../ui/badge";

const ICONS: Record<FoundIcon, LucideIcon> = { order: Package, delivery: Truck, return: Undo2, refund: Wallet };

function WhatWeFound({ facts }: { facts: RunFacts | null }) {
  const found = foundFacts(facts);
  if (found.length === 0) return null;
  return (
    <div data-testid="what-we-found" className="mt-5 rounded-lg border border-gray-200 bg-gray-50 px-4 py-3.5">
      <h3 className="text-xs font-semibold tracking-wide text-gray-600 uppercase">What we found</h3>
      <dl className="mt-2.5 space-y-2.5">
        {found.map((fact) => {
          const Icon = ICONS[fact.icon];
          return (
            <div key={fact.key} className="flex items-start gap-3">
              <Icon className="mt-0.5 size-4 shrink-0 text-primary-600" aria-hidden="true" />
              <div className="min-w-0 sm:flex sm:gap-3">
                <dt className="text-xs font-medium text-gray-600 sm:w-16 sm:shrink-0 sm:pt-px">{fact.label}</dt>
                <dd className="text-sm text-gray-900">{fact.value}</dd>
              </div>
            </div>
          );
        })}
      </dl>
    </div>
  );
}

export function ReplyCard({ reply, firstName, facts }: { reply: string; firstName: string; facts: RunFacts | null }) {
  return (
    <section aria-labelledby="reply-heading" className="animate-slide-up overflow-hidden rounded-xl border border-primary-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-primary-100 bg-primary-50/70 px-6 py-3">
        <h2 id="reply-heading" className="flex items-center gap-2 text-sm font-semibold text-primary-900">
          <MessageSquareText className="size-4 text-primary-600" aria-hidden="true" />
          Latest update from Support
        </h2>
        <Badge tone="primary" title="Checked against your records before it was sent">
          <ShieldCheck aria-hidden="true" />
          Verified
        </Badge>
      </div>
      <div className="px-6 py-5">
        {firstName !== "" && <p className="text-base font-medium text-gray-900">Hi {firstName},</p>}
        {/* Verified by the backend before it was saved; shown as plain text. */}
        <p className="mt-2 text-base leading-relaxed whitespace-pre-line text-gray-900" data-testid="verified-reply">
          {reply}
        </p>
        <WhatWeFound facts={facts} />
      </div>
    </section>
  );
}
