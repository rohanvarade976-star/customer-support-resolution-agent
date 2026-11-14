// What a page shows when it is not showing data: loading skeletons, an empty state with a next
// step, and an error state with a retry button. Every page uses these, so they look the same.

import { CircleExclamationPoint, RefreshCw, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "../lib/utils";
import { Button } from "./ui/button";
import { Card } from "./ui/card";
import { Skeleton } from "./ui/skeleton";

interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description: string;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon: Icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={cn("flex flex-col items-center px-6 py-12 text-center", className)}>
      <div className="grid size-12 place-items-center rounded-full bg-primary-50 text-primary-600 ring-8 ring-primary-50/50">
        <Icon className="size-6" aria-hidden="true" />
      </div>
      <h2 className="mt-4 text-base font-semibold text-gray-900">{title}</h2>
      <p className="mt-1 max-w-sm text-sm text-gray-600">{description}</p>
      {action !== undefined && <div className="mt-5">{action}</div>}
    </div>
  );
}

interface ErrorStateProps {
  message: string;
  onRetry?: () => void;
  action?: ReactNode; // instead of a retry, e.g. a link back
}

// The message comes from the API (or a fixed "could not be reached" text): role="alert".
export function ErrorState({ message, onRetry, action }: ErrorStateProps) {
  return (
    <Card>
      <div role="alert" className="flex flex-col items-center px-6 py-12 text-center">
        <div className="grid size-12 place-items-center rounded-full bg-danger-50 text-danger-600 ring-8 ring-danger-50/50">
          <CircleExclamationPoint className="size-6" aria-hidden="true" />
        </div>
        <h2 className="mt-4 text-base font-semibold text-gray-900">Something went wrong</h2>
        <p className="mt-1 max-w-sm text-sm text-gray-600">{message}</p>
        {(onRetry !== undefined || action !== undefined) && (
          <div className="mt-5">
            {action ?? (
              <Button variant="outline" onClick={onRetry}>
                <RefreshCw aria-hidden="true" />
                Try again
              </Button>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

// Rows like a list inside a card: an icon square, two lines of text and a badge.
export function ListSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <Card aria-busy="true" aria-label="Loading">
      <ul className="divide-y divide-gray-100">
        {Array.from({ length: rows }, (_, i) => (
          <li key={i} className="flex items-center gap-4 px-5 py-4">
            <Skeleton className="size-10 shrink-0 rounded-lg" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-4 w-1/3" />
              <Skeleton className="h-3 w-2/3" />
            </div>
            <Skeleton className="hidden h-5 w-20 rounded-full sm:block" />
          </li>
        ))}
      </ul>
    </Card>
  );
}

// A two-column detail page: content on the left, a side panel on the right.
export function DetailSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading" className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="space-y-6">
        {[0, 1].map((i) => (
          <Card key={i} className="space-y-3 p-5">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-5/6" />
            <Skeleton className="h-3 w-2/3" />
          </Card>
        ))}
      </div>
      <Card className="space-y-4 p-5">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-9 w-40" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-9 w-full" />
      </Card>
    </div>
  );
}
