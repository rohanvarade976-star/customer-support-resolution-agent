// A section that starts collapsed and opens on click, built on the native <details> element:
// keyboard and screen-reader support come from the browser, and it needs no script-injected
// styles, so it works under our strict Content-Security-Policy (ADR-020, ADR-022).
//
// Controlled: the page decides which sections are open (a needs-attention reason opens its
// section), and a click reports the new state back through onOpenChange.

import { ChevronDown, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "../../lib/utils";

interface CollapsibleSectionProps {
  id: string;
  title: string;
  summary?: ReactNode; // one line shown while collapsed, e.g. "6 agents · all passed"
  icon?: LucideIcon;
  badge?: ReactNode;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
  className?: string;
}

export function CollapsibleSection({
  id,
  title,
  summary,
  icon: Icon,
  badge,
  open,
  onOpenChange,
  children,
  className,
}: CollapsibleSectionProps) {
  return (
    <details
      id={id}
      open={open}
      onToggle={(event) => {
        // Fires for user clicks and for prop changes; only report real changes.
        if (event.currentTarget.open !== open) onOpenChange(event.currentTarget.open);
      }}
      className={cn("group scroll-mt-20 rounded-xl border border-gray-200 bg-white shadow-xs", className)}
      data-testid={`section-${id}`}
    >
      <summary className="flex cursor-pointer list-none items-center gap-3 rounded-xl px-5 py-3.5 hover:bg-gray-50 [&::-webkit-details-marker]:hidden">
        {Icon !== undefined && <Icon className="size-4.5 shrink-0 text-gray-500" aria-hidden="true" />}
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold text-gray-900">{title}</span>
          {summary !== undefined && <span className="mt-0.5 block truncate text-xs text-gray-500">{summary}</span>}
        </span>
        {/* Phones: the badge would squeeze the title, so it shows from the sm breakpoint up. */}
        {badge !== undefined && <span className="hidden shrink-0 sm:inline-flex">{badge}</span>}
        <ChevronDown
          className="size-4 shrink-0 text-gray-400 transition-transform group-open:rotate-180"
          aria-hidden="true"
        />
      </summary>
      <div className="border-t border-gray-100 p-4 sm:p-5">{children}</div>
    </details>
  );
}
