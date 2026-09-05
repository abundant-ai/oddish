"use client";

import { useState } from "react";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { Check, ChevronDown, Loader2 } from "lucide-react";
import { fetcher } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
} from "@/components/ui/command";

type Member = { id: string; name: string | null; email: string };

export function DeliveryOwnerPicker({
  taskName,
  ownerId,
  ownerName,
  disabled,
  onAssign,
}: {
  taskName: string;
  ownerId: string | null;
  ownerName: string | null;
  disabled: boolean;
  onAssign: (userId: string) => Promise<void>;
}) {
  const { orgId } = useAuth();
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const {
    data: members,
    error: loadError,
    mutate,
  } = useSWR<Member[]>(
    open && orgId ? ["/api/users", orgId] : null,
    ([url]: [string, string]) => fetcher<Member[]>(url)
  );

  return (
    <Popover
      open={open}
      onOpenChange={(value) => {
        if (!saving) {
          setOpen(value);
          setError(null);
        }
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          disabled={disabled || saving}
          className={
            ownerId ? "max-w-44 gap-2" : "rounded-l-none border-l-0 px-2"
          }
          aria-label={`Assign ${taskName}${ownerName ? `, currently ${ownerName}` : ""}`}
        >
          {ownerId && <span className="truncate">{ownerName ?? ownerId}</span>}
          <ChevronDown className="h-4 w-4 shrink-0" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-80 max-w-[calc(100vw-2rem)] p-0"
        onClick={(event) => event.stopPropagation()}
        aria-label={`Assign ${taskName}`}
      >
        <div className="border-b px-3 py-2 text-sm font-medium">
          Assign owner
        </div>
        <Command>
          <CommandInput
            placeholder="Search name or email…"
            disabled={saving}
            aria-label="Search organization members"
          />
          <CommandList aria-busy={!members && !loadError}>
            {loadError ? (
              <div role="alert" className="p-3 text-sm">
                Could not load organization members.
                <Button variant="ghost" size="sm" onClick={() => void mutate()}>
                  Retry
                </Button>
              </div>
            ) : !members ? (
              <p role="status" className="text-muted-foreground p-3 text-sm">
                Loading members…
              </p>
            ) : (
              <>
                <CommandEmpty>No matching members.</CommandEmpty>
                <CommandGroup>
                  {[...members]
                    .sort((a, b) =>
                      (a.name ?? a.email).localeCompare(b.name ?? b.email)
                    )
                    .map((member) => (
                      <CommandItem
                        key={member.id}
                        value={member.id}
                        keywords={[member.name ?? "", member.email]}
                        disabled={saving || member.id === ownerId}
                        onSelect={async () => {
                          setSaving(true);
                          setError(null);
                          try {
                            await onAssign(member.id);
                            setOpen(false);
                          } catch (err) {
                            setError(
                              err instanceof Error
                                ? err.message
                                : "Assignment failed"
                            );
                          } finally {
                            setSaving(false);
                          }
                        }}
                        className="gap-2 py-2"
                      >
                        <span
                          className="bg-muted flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs"
                          aria-hidden="true"
                        >
                          {(member.name ?? member.email)
                            .slice(0, 1)
                            .toUpperCase()}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate">
                            {member.name ?? member.email}
                          </span>
                          <span className="text-muted-foreground block truncate text-xs">
                            {member.email}
                          </span>
                        </span>
                        {member.id === ownerId && (
                          <Check
                            className="h-4 w-4"
                            aria-label="Current owner"
                          />
                        )}
                      </CommandItem>
                    ))}
                </CommandGroup>
              </>
            )}
          </CommandList>
        </Command>
        {ownerId && (
          <p className="text-muted-foreground border-t px-3 py-2 text-xs">
            Choosing someone replaces the current owner.
          </p>
        )}
        {saving && (
          <p
            role="status"
            className="flex items-center gap-2 px-3 py-2 text-sm"
          >
            <Loader2 className="h-4 w-4 animate-spin" />
            Assigning…
          </p>
        )}
        {error && (
          <p role="alert" className="text-destructive px-3 py-2 text-sm">
            {error}
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}
