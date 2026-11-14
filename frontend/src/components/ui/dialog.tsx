// Modal dialog and slide-over sheet, built on the browser's own <dialog> element.
//
// Why not Radix Dialog: its scroll lock adds a <style> tag from JavaScript, which our strict
// Content-Security-Policy (style-src 'self') blocks (ADR-020). `showModal()` gives us what we
// need natively: focus moves into the dialog and stays there, Esc closes it, the page behind
// becomes inert, and focus returns to the opening button afterwards. index.css stops the page
// from scrolling while a dialog is open.

import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode, type RefObject } from "react";

import { cn } from "../../lib/utils";

// Opens or closes the <dialog> to match `open`.
function useNativeModal(ref: RefObject<HTMLDialogElement | null>, open: boolean) {
  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [ref, open]);
}

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  // False while an action is running: Esc and clicks outside do nothing.
  dismissible?: boolean;
}

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  dismissible = true,
}: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  useNativeModal(ref, open);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={description === undefined ? undefined : descriptionId}
      className="m-auto w-[calc(100%-2rem)] max-w-md rounded-xl bg-white p-0 text-gray-900 shadow-lg open:animate-scale-in"
      onClose={onClose}
      onCancel={(event) => {
        if (!dismissible) event.preventDefault();
      }}
      onClick={(event) => {
        // A click on the dialog element itself (not its content) is a click on the backdrop.
        if (dismissible && event.target === event.currentTarget) onClose();
      }}
    >
      {open && (
        <div className="p-6">
          <h2 id={titleId} className="pr-8 text-lg font-semibold text-gray-900">
            {title}
          </h2>
          {description !== undefined && (
            <div id={descriptionId} className="mt-2 text-sm leading-relaxed text-gray-600">
              {description}
            </div>
          )}
          {children}
          {footer !== undefined && (
            <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">{footer}</div>
          )}
        </div>
      )}
    </dialog>
  );
}

interface SheetProps {
  open: boolean;
  onClose: () => void;
  label: string;
  children: ReactNode;
}

// A panel that slides in from the left edge: the navigation menu on small screens.
export function Sheet({ open, onClose, label, children }: SheetProps) {
  const ref = useRef<HTMLDialogElement>(null);
  useNativeModal(ref, open);

  return (
    <dialog
      ref={ref}
      aria-label={label}
      className={cn(
        "m-0 h-dvh max-h-dvh w-72 max-w-[85vw] bg-white p-0 shadow-lg",
        "open:animate-slide-in-left",
      )}
      onClose={onClose}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      {open && (
        <div className="relative flex h-full flex-col">
          <button
            type="button"
            onClick={onClose}
            aria-label="Close menu"
            className="absolute top-3 right-3 z-10 grid size-9 cursor-pointer place-items-center rounded-lg text-gray-500 hover:bg-gray-100 hover:text-gray-900"
          >
            <X className="size-5" aria-hidden="true" />
          </button>
          {children}
        </div>
      )}
    </dialog>
  );
}
