// "Are you sure?" before an action that cannot be undone (approve or reject a refund, delete
// memory). The confirm button shows a spinner while the action runs, and the dialog cannot be
// dismissed meanwhile.

import type { ReactNode } from "react";

import { Button } from "./ui/button";
import { Dialog } from "./ui/dialog";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "danger";
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  tone = "primary",
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      title={title}
      description={description}
      dismissible={!busy}
      footer={
        <>
          <Button variant="outline" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant={tone} onClick={onConfirm} loading={busy}>
            {confirmLabel}
          </Button>
        </>
      }
    />
  );
}
