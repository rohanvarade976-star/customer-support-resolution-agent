// The bar above every page: the menu button (small screens), breadcrumbs, and the user menu
// with the user's name, role and "Log out".

import { ChevronDown, ChevronRight, LogOut, Menu } from "lucide-react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { isReviewer, useAuth } from "../auth/auth";
import { humanize } from "../lib/labels";
import { breadcrumbsFor } from "../lib/navigation";
import { cn } from "../lib/utils";
import { Badge } from "./ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return parts
    .slice(0, 2)
    .map((part) => part.charAt(0).toUpperCase())
    .join("");
}

export function TopBar({ onOpenMenu }: { onOpenMenu: () => void }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const crumbs = breadcrumbsFor(pathname, isReviewer(user));
  const name = user?.name ?? "";

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-gray-200 bg-white/95 px-4 backdrop-blur sm:px-6 lg:px-8">
      <button
        type="button"
        onClick={onOpenMenu}
        aria-label="Open menu"
        className="-ml-2 grid size-9 cursor-pointer place-items-center rounded-lg text-gray-600 hover:bg-gray-100 hover:text-gray-900 lg:hidden"
      >
        <Menu className="size-5" aria-hidden="true" />
      </button>

      <nav aria-label="Breadcrumb" className="min-w-0 flex-1">
        <ol className="flex items-center gap-1.5 text-sm">
          {crumbs.map((crumb, i) => (
            // On phones only the current page is shown; wider screens show the whole trail.
            <li
              key={crumb.label}
              className={cn("min-w-0 items-center gap-1.5", i < crumbs.length - 1 ? "hidden sm:flex" : "flex")}
            >
              {i > 0 && <ChevronRight className="hidden size-4 shrink-0 text-gray-400 sm:block" aria-hidden="true" />}
              {crumb.to === undefined ? (
                <span aria-current="page" className="truncate font-medium text-gray-900">
                  {crumb.label}
                </span>
              ) : (
                <Link to={crumb.to} className="truncate rounded text-gray-600 hover:text-gray-900">
                  {crumb.label}
                </Link>
              )}
            </li>
          ))}
        </ol>
      </nav>

      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label={`Account menu: ${name}`}
          className="flex cursor-pointer items-center gap-2 rounded-lg py-1 pr-1.5 pl-1 text-sm hover:bg-gray-100 data-[state=open]:bg-gray-100"
        >
          <span
            className="grid size-8 place-items-center rounded-full bg-primary-100 text-xs font-semibold text-primary-800"
            aria-hidden="true"
          >
            {initials(name)}
          </span>
          <span className="hidden font-medium text-gray-800 sm:inline">{name}</span>
          <ChevronDown className="size-4 text-gray-500" aria-hidden="true" />
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuLabel>
            <span className="flex items-center justify-between gap-3">
              <span className="truncate font-medium text-gray-900">{name}</span>
              {user !== null && <Badge tone="primary">{humanize(user.role)}</Badge>}
            </span>
            <span className="mt-0.5 block truncate text-xs font-normal text-gray-500">{user?.email}</span>
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onSelect={() => {
              logout();
              void navigate("/login");
            }}
          >
            <LogOut aria-hidden="true" />
            Log out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </header>
  );
}
