/**
 * Picker modal — "Add X" flows (plugin type, integration provider, …).
 *
 * Per C1: click "Add X" → modal lists available X-types → user picks one →
 * modal closes, route push to the detail page. This component owns the
 * picker UI; callers wire the navigation in `onPick`.
 */

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@shared/components/ui/dialog";
import { cn } from "@shared/utils";
import type { LucideIcon } from "lucide-react";

export interface PickerOption {
  id: string;
  label: string;
  description?: string;
  icon?: LucideIcon;
  disabled?: boolean;
}

interface PickerModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  options: PickerOption[];
  onPick: (id: string) => void;
  emptyMessage?: string;
}

export function PickerModal({
  open,
  onOpenChange,
  title,
  description,
  options,
  onPick,
  emptyMessage,
}: PickerModalProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        <div className="flex flex-col gap-2 mt-2">
          {options.length === 0 ? (
            <p className="text-sm text-muted-foreground py-6 text-center">
              {emptyMessage ?? "Nothing available right now."}
            </p>
          ) : (
            options.map((opt) => {
              const Icon = opt.icon;
              return (
                <button
                  key={opt.id}
                  type="button"
                  disabled={opt.disabled}
                  onClick={() => onPick(opt.id)}
                  className={cn(
                    "flex items-start gap-3 p-3 text-left rounded-md border border-border",
                    "hover:bg-accent hover:text-accent-foreground transition-colors",
                    "disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent",
                  )}
                >
                  {Icon && <Icon className="w-5 h-5 mt-0.5 shrink-0" aria-hidden="true" />}
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium">{opt.label}</div>
                    {opt.description && (
                      <div className="text-xs text-muted-foreground mt-0.5">{opt.description}</div>
                    )}
                  </div>
                </button>
              );
            })
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
