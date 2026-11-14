// Form controls: label, text input, text area, and a field hint or error under a control.
// `aria-invalid` turns a control red, so errors look the same everywhere.

import type { ComponentProps } from "react";

import { cn } from "../../lib/utils";

const controlClass =
  "w-full rounded-lg border border-gray-300 bg-white px-3 text-sm text-gray-900 shadow-xs transition-colors placeholder:text-gray-400 focus:border-primary-500 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-primary-500/40 disabled:cursor-not-allowed disabled:bg-gray-50 aria-[invalid=true]:border-danger-500 aria-[invalid=true]:focus-visible:outline-danger-500/40";

export function Label({ className, ...props }: ComponentProps<"label">) {
  return <label className={cn("mb-1.5 block text-sm font-medium text-gray-800", className)} {...props} />;
}

export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input className={cn(controlClass, "h-10", className)} {...props} />;
}

export function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  return <textarea className={cn(controlClass, "min-h-24 resize-y py-2.5 leading-6", className)} {...props} />;
}

export function FieldHint({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("mt-1.5 text-xs text-gray-500", className)} {...props} />;
}

export function FieldError({ className, children, ...props }: ComponentProps<"p">) {
  if (children === null || children === undefined || children === false) return null;
  return (
    <p className={cn("mt-1.5 text-xs font-medium text-danger-700", className)} {...props}>
      {children}
    </p>
  );
}
