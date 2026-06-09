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
}

export function TagPicker({
  selectedTagIds,
  onChange,
  placeholder = "Select tag…",
  multi = true,
}: TagPickerProps) {
  const [open, setOpen] = useState(false);
  const { data } = useSWR<TagListResponse>("/api/tags", fetcher, {
    revalidateOnFocus: false,
  });
  const items = data?.items ?? [];
  const selected = useMemo(() => new Set(selectedTagIds), [selectedTagIds]);

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

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm">
          {selected.size > 0 ? `Tags (${selected.size})` : placeholder}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-72 p-0">
        <Command>
          <CommandInput placeholder="Search tags…" />
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
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
