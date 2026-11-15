// The navigation for the user's role: brand at the top, one link per area, the active one
// highlighted. Shown fixed on wide screens and inside the slide-over menu on small ones.

import { LifeBuoy } from "lucide-react";
import { Link, useLocation } from "react-router-dom";

import { isReviewer, useAuth } from "../auth/auth";
import { useEscalationCount } from "../hooks/useEscalationCount";
import { CUSTOMER_NAV, isActive, REVIEWER_NAV } from "../lib/navigation";
import { cn } from "../lib/utils";

export function Brand() {
  return (
    <span className="flex items-center gap-2.5">
      <span className="grid size-8 place-items-center rounded-lg bg-primary-600 text-white shadow-xs">
        <LifeBuoy className="size-4.5" aria-hidden="true" />
      </span>
      <span className="text-[0.9375rem] font-semibold tracking-tight text-gray-900">Support Resolution</span>
    </span>
  );
}

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { user } = useAuth();
  const { pathname } = useLocation();
  const reviewer = isReviewer(user);
  const items = reviewer ? REVIEWER_NAV : CUSTOMER_NAV;
  const escalations = useEscalationCount(reviewer, pathname);

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-14 shrink-0 items-center border-b border-gray-200 px-5">
        <Link to={reviewer ? "/approvals" : "/home"} onClick={onNavigate} className="rounded-lg">
          <Brand />
        </Link>
      </div>
      <nav aria-label="Main" className="flex-1 overflow-y-auto px-3 py-4">
        <p className="px-3 pb-2 text-xs font-medium tracking-wide text-gray-500 uppercase">
          {reviewer ? "Reviewer" : "Customer"}
        </p>
        <ul className="space-y-0.5">
          {items.map((item) => {
            const active = isActive(item, pathname);
            const Icon = item.icon;
            return (
              <li key={item.to}>
                <Link
                  to={item.to}
                  onClick={onNavigate}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                    active
                      ? "bg-primary-50 text-primary-700"
                      : "text-gray-700 hover:bg-gray-100 hover:text-gray-900",
                  )}
                >
                  <Icon
                    className={cn("size-4.5 shrink-0", active ? "text-primary-600" : "text-gray-500")}
                    aria-hidden="true"
                  />
                  <span className="flex-1">{item.label}</span>
                  {item.count === "escalations" && escalations !== null && escalations > 0 && (
                    <span
                      className="min-w-5 rounded-full bg-warning-100 px-1.5 py-0.5 text-center text-xs font-semibold text-warning-800 tabular-nums"
                      aria-label={`${String(escalations)} open`}
                      data-testid="escalation-count"
                    >
                      {escalations}
                    </span>
                  )}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      <div className="border-t border-gray-200 px-5 py-4 text-xs leading-relaxed text-gray-500">
        Every refund is checked by code and approved by a person.
      </div>
    </div>
  );
}
