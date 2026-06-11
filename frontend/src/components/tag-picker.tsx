"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { fetcher } from "@/lib/api";

interface BackendTagListItem {
  id: string;
  key: string;
  value: string | null;
  color: string | null;
  visibility: "PRIVATE" | "PUBLIC";
  state: string;
  usage_count: number;
}

interface TagListResponse {
  items: BackendTagListItem[];
}

export interface TagPickerProps {
  selectedTagIds: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  multi?: boolean;
  // Opt-in: offer "Create <query>" when the typed name matches no existing
  // tag. Only enable in assignment contexts, never in browse/filter ones.
  allowCreate?: boolean;
}

export function TagPicker({
  selectedTagIds,
  onChange,
  placeholder = "Select tag…",
  multi = true,
  allowCreate = false,
}: TagPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const { data, mutate } = useSWR<TagListResponse>("/api/tags", fetcher, {
    revalidateOnFocus: false,
  });
  const items = data?.items ?? [];
  const selected = useMemo(() => new Set(selectedTagIds), [selectedTagIds]);

  const normalizedQuery = query.trim().toLowerCase();
  const showCreate =
    allowCreate &&
    normalizedQuery.length > 0 &&
    !items.some((it) => it.key === normalizedQuery);

  function toggle(tagId: string) {
    if (multi) {
      const next = new Set(selected);
      if (next.has(tagId)) next.delete(tagId);
      else next.add(tagId);
      onChange(Array.from(next));
      return;
    }
    onChange([tagId]);
    setOpen(false);
  }

  async function createTag(rawKey: string) {
    const res = await fetch("/api/tags", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: rawKey, visibility: "PRIVATE" }),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const detail =
        body && typeof body === "object"
          ? ((body as { detail?: string; error?: string }).detail ??
            (body as { detail?: string; error?: string }).error)
          : null;
      setCreateError(detail ?? "Could not create tag.");
      return;
    }
    // Backend may normalize the typed key — select by the returned id.
    const created = body as BackendTagListItem;
    await mutate();
    toggle(created.id);
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm">
          {selected.size > 0 ? `Tags (${selected.size})` : placeholder}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-72 p-0">
        <Command>
          <CommandInput
            placeholder="Search tags…"
            value={query}
            onValueChange={(next) => {
              setQuery(next);
              setCreateError(null);
            }}
          />
          <CommandList>
            <CommandEmpty>No tags.</CommandEmpty>
            <CommandGroup>
              {items
                .filter((it) => it.state === "ACTIVE")
                .map((it) => (
                  <CommandItem
                    key={it.id}
                    value={it.key}
                    onSelect={() => toggle(it.id)}
                  >
                    <span className="mr-2">
                      {selected.has(it.id) ? "✓" : " "}
                    </span>
                    <span>{it.key}</span>
                  </CommandItem>
                ))}
              {showCreate ? (
                <CommandItem value={query} onSelect={() => createTag(query)}>
                  <span className="mr-2">+</span>
                  <span>Create &quot;{query}&quot;</span>
                </CommandItem>
              ) : null}
            </CommandGroup>
          </CommandList>
          {createError ? (
            <div className="border-t px-3 py-2 text-xs text-destructive">
              {createError}
            </div>
          ) : null}
        </Command>
      </PopoverContent>
    </Popover>
  );
}
