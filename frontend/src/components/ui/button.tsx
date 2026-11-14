// The button, in the shadcn/ui style: one component, variants picked by props.
// `asChild` renders the child element instead (e.g. a router <Link>) with the button's look.
// `loading` shows a spinner and disables the button, so an action cannot be sent twice.

import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { LoaderCircle } from "lucide-react";
import type { ComponentProps } from "react";

import { cn } from "../../lib/utils";

const buttonVariants = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-lg font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: "bg-primary-600 text-white shadow-xs hover:bg-primary-700",
        outline: "border border-gray-300 bg-white text-gray-800 shadow-xs hover:bg-gray-50",
        ghost: "text-gray-700 hover:bg-gray-100 hover:text-gray-900",
        danger: "bg-danger-600 text-white shadow-xs hover:bg-danger-700",
        "danger-outline":
          "border border-danger-200 bg-white text-danger-700 shadow-xs hover:bg-danger-50",
        link: "h-auto px-0 text-primary-700 underline-offset-4 hover:underline",
      },
      size: {
        sm: "h-8 px-3 text-sm",
        md: "h-9 px-4 text-sm",
        lg: "h-11 px-5 text-base",
        icon: "size-9",
      },
    },
    // A link-style button lines up with the text around it, whatever its size.
    compoundVariants: [{ variant: "link", class: "h-auto px-0" }],
    defaultVariants: { variant: "primary", size: "md" },
  },
);

type ButtonProps = ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & { asChild?: boolean; loading?: boolean };

export function Button({
  className,
  variant,
  size,
  asChild = false,
  loading = false,
  disabled,
  children,
  type = "button",
  ...props
}: ButtonProps) {
  const classes = cn(buttonVariants({ variant, size }), className);
  if (asChild) {
    return (
      <Slot className={classes} {...props}>
        {children}
      </Slot>
    );
  }
  return (
    <button
      type={type}
      className={classes}
      disabled={disabled === true || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading && <LoaderCircle className="animate-spin" aria-hidden="true" />}
      {children}
    </button>
  );
}
