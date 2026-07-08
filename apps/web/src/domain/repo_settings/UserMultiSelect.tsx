/**
 * Org-member multi-select — a `Popover` anchoring a filterable `Command`
 * list, checkmark per selected row. Shared by the schedule's notify picker
 * and the protected-path-set owner picker (see `architecture.md § Backend
 * ↔ web`).
 */

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@shared/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@shared/components/ui/popover";
import { Check, ChevronDown } from "lucide-react";
import type { OrgMemberSummary } from "./queries";

export interface UserMultiSelectProps {
  members: OrgMemberSummary[];
  selected: string[];
  onChange: (userIds: string[]) => void;
  placeholder?: string;
  "data-testid"?: string;
}

export function UserMultiSelect({
  members,
  selected,
  onChange,
  placeholder = "Choose members…",
  "data-testid": testId,
}: UserMultiSelectProps) {
  function toggle(userId: string) {
    onChange(
      selected.includes(userId) ? selected.filter((id) => id !== userId) : [...selected, userId],
    );
  }

  const selectedLabel = selected
    .map((id) => members.find((m) => m.user_id === id)?.handle ?? id)
    .join(", ");

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid={testId}
          className="flex w-full items-center justify-between gap-2 rounded-md border border-input bg-transparent px-3 py-2 text-sm"
        >
          <span className={selected.length === 0 ? "text-muted-foreground" : ""}>
            {selected.length === 0 ? placeholder : selectedLabel}
          </span>
          <ChevronDown className="h-4 w-4 shrink-0 opacity-50" aria-hidden />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-64 p-0" align="start">
        <Command>
          <CommandInput placeholder="Search members…" />
          <CommandList>
            <CommandEmpty>No members found.</CommandEmpty>
            <CommandGroup>
              {members.map((m) => {
                const isSelected = selected.includes(m.user_id);
                return (
                  <CommandItem
                    key={m.user_id}
                    data-testid={`${testId}-option-${m.user_id}`}
                    onSelect={() => toggle(m.user_id)}
                  >
                    <Check
                      className={`h-4 w-4 ${isSelected ? "opacity-100" : "opacity-0"}`}
                      aria-hidden
                    />
                    <span>{m.handle}</span>
                    <span className="ml-auto text-xs text-muted-foreground">{m.display_name}</span>
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
