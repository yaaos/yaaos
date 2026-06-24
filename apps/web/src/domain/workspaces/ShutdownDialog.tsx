/**
 * ShutdownDialog — confirm before bulk-shutting down selected active agents.
 *
 * On confirm the parent calls the shutdown mutation and clears the selection.
 * The dialog does not know about selection state; it only surfaces the count
 * for future copy personalisation.
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

export interface ShutdownDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  selectionCount: number;
}

export function ShutdownDialog({
  open,
  onOpenChange,
  onConfirm,
  selectionCount: _selectionCount,
}: ShutdownDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent data-testid="workspaces-shutdown-dialog">
        <AlertDialogHeader>
          <AlertDialogTitle>Shut down selected agents?</AlertDialogTitle>
          <AlertDialogDescription>
            In-flight reviews finish before each agent exits. You can cancel before the agent exits.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm} data-testid="workspaces-shutdown-dialog-confirm">
            Shut down
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
