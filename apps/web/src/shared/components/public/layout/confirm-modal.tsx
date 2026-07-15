/**
 * Confirm modal — short decisive actions (delete, re-run, …).
 *
 * Two locked copy patterns from requirements.md § D3:
 *   - Destructive: "Delete [thing]?" / "[Thing] will be removed permanently.
 *     This cannot be undone." / destructive variant button.
 *   - Cost-protective: "Re-run this review?" / "Running again will spend
 *     roughly N tokens." / default variant button.
 */

import { Button } from "@shared/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@shared/components/ui/dialog";
import type { ReactNode } from "react";

export type ConfirmTone = "destructive" | "default";

interface ConfirmModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  tone?: ConfirmTone;
  onConfirm: () => void;
  pending?: boolean;
  /** Applied as `data-testid` on the dialog content element. */
  testId?: string;
}

export function ConfirmModal({
  open,
  onOpenChange,
  title,
  body,
  confirmLabel,
  tone = "default",
  onConfirm,
  pending,
  testId,
}: ConfirmModalProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid={testId}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{body}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancel
          </Button>
          <Button
            variant={tone === "destructive" ? "destructive" : "default"}
            onClick={onConfirm}
            disabled={pending}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
