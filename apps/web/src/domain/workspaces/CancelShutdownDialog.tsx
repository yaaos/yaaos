/**
 * CancelShutdownDialog — confirm before canceling shutdown on selected
 * draining agents.
 */

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@shared/components/ui/alert-dialog";

export interface CancelShutdownDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  selectionCount: number;
}

export function CancelShutdownDialog({
  open,
  onOpenChange,
  onConfirm,
  selectionCount: _selectionCount,
}: CancelShutdownDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent data-testid="workspaces-cancel-shutdown-dialog">
        <AlertDialogHeader>
          <AlertDialogTitle>Cancel shutdown?</AlertDialogTitle>
          <AlertDialogDescription>
            Selected agents resume accepting new review work on their next intake cycle.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            data-testid="workspaces-cancel-shutdown-dialog-confirm"
          >
            Cancel shutdown
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
