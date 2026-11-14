// A gray pulsing block that stands in for content while it loads.

import { cn } from "../../lib/utils";

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-gray-200/80", className)} aria-hidden="true" />;
}
