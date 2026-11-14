// A menu that opens from a button (the user menu), on Radix's DropdownMenu: arrow keys move
// between items, Enter picks one, Esc closes, focus returns to the button.
//
// Always non-modal: Radix's modal mode locks page scroll by adding a <style> tag, which our
// Content-Security-Policy blocks (ADR-020). A menu does not need it.

import * as Menu from "@radix-ui/react-dropdown-menu";
import type { ComponentProps, ReactNode } from "react";

import { cn } from "../../lib/utils";

export function DropdownMenu({ children }: { children: ReactNode }) {
  return <Menu.Root modal={false}>{children}</Menu.Root>;
}

export const DropdownMenuTrigger = Menu.Trigger;

export function DropdownMenuContent({
  className,
  align = "end",
  sideOffset = 6,
  ...props
}: ComponentProps<typeof Menu.Content>) {
  return (
    <Menu.Portal>
      <Menu.Content
        align={align}
        sideOffset={sideOffset}
        className={cn(
          "z-50 min-w-56 rounded-lg border border-gray-200 bg-white p-1 text-sm text-gray-800 shadow-lg data-[state=open]:animate-scale-in",
          className,
        )}
        {...props}
      />
    </Menu.Portal>
  );
}

export function DropdownMenuItem({ className, ...props }: ComponentProps<typeof Menu.Item>) {
  return (
    <Menu.Item
      className={cn(
        "flex cursor-pointer items-center gap-2 rounded-md px-2.5 py-2 outline-none select-none data-[highlighted]:bg-gray-100 data-[highlighted]:text-gray-900 [&_svg]:size-4 [&_svg]:text-gray-500",
        className,
      )}
      {...props}
    />
  );
}

export function DropdownMenuLabel({ className, ...props }: ComponentProps<typeof Menu.Label>) {
  return <Menu.Label className={cn("px-2.5 py-2", className)} {...props} />;
}

export function DropdownMenuSeparator({ className, ...props }: ComponentProps<typeof Menu.Separator>) {
  return <Menu.Separator className={cn("-mx-1 my-1 h-px bg-gray-100", className)} {...props} />;
}
