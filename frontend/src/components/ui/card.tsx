// A white surface with a border and a soft shadow, plus its header, content and footer parts.

import type { ComponentProps } from "react";

import { cn } from "../../lib/utils";

// `interactive` adds a hover lift and a focus ring around whatever inside is focused, for cards
// that are (or contain) links or buttons.
export function Card({ className, interactive = false, ...props }: ComponentProps<"section"> & { interactive?: boolean }) {
  return (
    <section
      className={cn(
        "rounded-xl border border-gray-200 bg-white shadow-sm",
        interactive &&
          "transition-[box-shadow,border-color,transform] duration-200 hover:-translate-y-0.5 hover:border-gray-300 hover:shadow-md focus-within:border-primary-300 focus-within:ring-2 focus-within:ring-primary-100",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn("flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-5 pt-5", className)}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }: ComponentProps<"h2">) {
  return <h2 className={cn("text-base font-semibold text-gray-900", className)} {...props} />;
}

export function CardDescription({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("mt-1 text-sm text-gray-600", className)} {...props} />;
}

export function CardContent({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("px-5 py-5", className)} {...props} />;
}

export function CardFooter({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-3 rounded-b-xl border-t border-gray-100 bg-gray-50/60 px-5 py-3",
        className,
      )}
      {...props}
    />
  );
}
