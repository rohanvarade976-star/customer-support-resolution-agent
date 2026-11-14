// A small label with a tone (soft tinted background). With `dot`, a coloured dot comes first;
// the text always says what the colour means, so colour is never the only signal.

import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";

import { cn } from "../../lib/utils";

const badgeVariants = cva(
  "inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset [&_svg]:size-3",
  {
    variants: {
      tone: {
        neutral: "bg-gray-100 text-gray-700 ring-gray-200",
        primary: "bg-primary-50 text-primary-700 ring-primary-200",
        success: "bg-success-50 text-success-800 ring-success-200",
        warning: "bg-warning-50 text-warning-800 ring-warning-200",
        danger: "bg-danger-50 text-danger-800 ring-danger-200",
        info: "bg-info-50 text-info-800 ring-info-200",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

const DOT: Record<string, string> = {
  neutral: "bg-gray-400",
  primary: "bg-primary-500",
  success: "bg-success-500",
  warning: "bg-warning-500",
  danger: "bg-danger-500",
  info: "bg-info-500",
};

export type BadgeTone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;

type BadgeProps = ComponentProps<"span"> & { tone?: BadgeTone; dot?: boolean };

export function Badge({ className, tone = "neutral", dot = false, children, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ tone }), className)} {...props}>
      {dot && <span className={cn("size-1.5 rounded-full", DOT[tone])} aria-hidden="true" />}
      {children}
    </span>
  );
}
