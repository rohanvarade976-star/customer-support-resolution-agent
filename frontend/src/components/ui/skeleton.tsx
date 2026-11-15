// A gray block with a light band sweeping across it, standing in for content while it loads.
// The sweep is a CSS animation (switched off by prefers-reduced-motion, which leaves plain gray).

import { cn } from "../../lib/utils";

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "animate-shimmer rounded-md bg-gray-200 bg-linear-to-r from-gray-200 via-gray-100 to-gray-200 bg-[length:200%_100%]",
        className,
      )}
      aria-hidden="true"
    />
  );
}
