// An inline message box with an icon. Danger alerts use role="alert" so screen readers
// announce them at once; the others are announced politely.

import { CircleCheck, CircleExclamationPoint, Info, TriangleExclamationPoint } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "../../lib/utils";

type AlertTone = "info" | "success" | "warning" | "danger";

const STYLES: Record<AlertTone, string> = {
  info: "border-info-200 bg-info-50 text-info-800",
  success: "border-success-200 bg-success-50 text-success-800",
  warning: "border-warning-200 bg-warning-50 text-warning-800",
  danger: "border-danger-200 bg-danger-50 text-danger-800",
};

const ICONS = {
  info: Info,
  success: CircleCheck,
  warning: TriangleExclamationPoint,
  danger: CircleExclamationPoint,
};

interface AlertProps {
  tone?: AlertTone;
  title?: string;
  children?: ReactNode;
  className?: string;
}

export function Alert({ tone = "info", title, children, className }: AlertProps) {
  const Icon = ICONS[tone];
  return (
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={cn("flex gap-3 rounded-lg border px-4 py-3 text-sm", STYLES[tone], className)}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 space-y-1">
        {title !== undefined && <p className="font-medium">{title}</p>}
        {children !== undefined && <div className="leading-relaxed">{children}</div>}
      </div>
    </div>
  );
}
