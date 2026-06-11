"use client";

import { useMemo, useState, type ReactNode } from "react";
import { Tag } from "lucide-react";
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
import { TAG_COLOR_PALETTE, tagColor } from "@/lib/tag-colors";
import { cn } from "@/lib/utils";

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
  // Replaces the default outline button as the popover trigger (e.g. a
  // compact icon button). Must accept a forwarded ref (PopoverTrigger asChild).
  trigger?: ReactNode;
}

export function TagPicker({
  selectedTagIds,
  onChange,
  placeholder = "Select tag…",
  multi = true,
  allowCreate = false,
  trigger,
}: TagPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [createColor, setCreateColor] = useState<string | null>(null);
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
  // Swatch selection wins; otherwise the name's deterministic palette color
  // (the same hue the chip would fall back to) is preselected.
  const effectiveCreateColor = createColor ?? tagColor(normalizedQuery);

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
      body: JSON.stringify({
        key: rawKey,
        color: effectiveCreateColor,
        visibility: "PRIVATE",
      }),
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
    setCreateColor(null);
    await mutate();
    toggle(created.id);
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        {trigger ?? (
          <Button variant="outline" size="sm">
            {selected.size > 0 ? `Tags (${selected.size})` : placeholder}
          </Button>
        )}
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
                    <span className="mr-1 w-3 text-xs">
                      {selected.has(it.id) ? "✓" : ""}
                    </span>
                    <Tag
                      className="mr-1.5 h-3.5 w-3.5 shrink-0"
                      style={{ color: tagColor(it.key, it.color) }}
                      fill={tagColor(it.key, it.color)}
                      fillOpacity={0.25}
                    />
                    <span>{it.key}</span>
                  </CommandItem>
                ))}
              {showCreate ? (
                <CommandItem value={query} onSelect={() => createTag(query)}>
                  <Tag
                    className="mr-1.5 h-3.5 w-3.5 shrink-0"
                    style={{ color: effectiveCreateColor }}
                    fill={effectiveCreateColor}
                    fillOpacity={0.25}
                  />
                  <span>Create &quot;{query}&quot;</span>
                </CommandItem>
              ) : null}
            </CommandGroup>
          </CommandList>
          {showCreate ? (
            <div className="flex items-center gap-1.5 border-t px-3 py-2">
              {TAG_COLOR_PALETTE.map((swatch) => (
                <button
                  key={swatch}
                  type="button"
                  aria-label={`Tag color ${swatch}`}
                  className={cn(
                    "h-4 w-4 rounded-full transition-transform hover:scale-110",
                    swatch === effectiveCreateColor &&
                      "ring-2 ring-ring ring-offset-1 ring-offset-popover",
                  )}
                  style={{ backgroundColor: swatch }}
                  onClick={() => setCreateColor(swatch)}
                />
              ))}
            </div>
          ) : null}
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
