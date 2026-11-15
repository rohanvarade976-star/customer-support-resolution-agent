// The sidebar's items for each role, which item is active for a URL, and the breadcrumbs.

import {
  FilePlus,
  Headset,
  House,
  Inbox,
  Package,
  ShieldCheck,
  Ticket,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  // Other URL prefixes that belong to this item (a run page belongs to "My tickets").
  also?: string[];
  // A live count shown next to the label (the sidebar fetches it).
  count?: "escalations";
}

export const CUSTOMER_NAV: NavItem[] = [
  { to: "/home", label: "Home", icon: House },
  { to: "/complaints/new", label: "New request", icon: FilePlus },
  { to: "/tickets", label: "My tickets", icon: Ticket, also: ["/runs/"] },
  { to: "/orders", label: "My orders", icon: Package },
  { to: "/memory", label: "Privacy and memory", icon: ShieldCheck },
];

export const REVIEWER_NAV: NavItem[] = [
  { to: "/approvals", label: "Approval queue", icon: Inbox, also: ["/runs/"] },
  { to: "/escalations", label: "Escalations", icon: Headset, count: "escalations" },
];

export function isActive(item: NavItem, pathname: string): boolean {
  if (pathname === item.to || pathname.startsWith(`${item.to}/`)) return true;
  return item.also?.some((prefix) => pathname.startsWith(prefix)) ?? false;
}

export interface Crumb {
  label: string;
  to?: string; // absent for the current page
}

// Breadcrumbs from the URL alone. The last crumb is the current page.
export function breadcrumbsFor(pathname: string, reviewer: boolean): Crumb[] {
  if (reviewer) {
    const queue = { label: "Approval queue", to: "/approvals" };
    if (pathname.startsWith("/approvals/")) return [queue, { label: "Refund case" }];
    if (pathname.startsWith("/escalations/")) {
      return [{ label: "Escalations", to: "/escalations" }, { label: "Escalated case" }];
    }
    if (pathname === "/escalations") return [{ label: "Escalations" }];
    if (pathname.startsWith("/runs/")) return [queue, { label: "Run timeline" }];
    return [{ label: "Approval queue" }];
  }
  const home = { label: "Home", to: "/home" };
  if (pathname.startsWith("/runs/")) {
    return [home, { label: "My tickets", to: "/tickets" }, { label: "Request" }];
  }
  const item = CUSTOMER_NAV.find((nav) => nav.to === pathname);
  if (item === undefined || item.to === "/home") return [{ label: "Home" }];
  return [home, { label: item.label }];
}
