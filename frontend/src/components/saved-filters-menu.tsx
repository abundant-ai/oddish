"use client";

import { useState } from "react";
import { Bookmark, Tag } from "lucide-react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { fetcher } from "@/lib/api";
import { tagColor } from "@/lib/tag-colors";
import { parseTaskSearch, serializeTaskSearch } from "@/lib/tag-query";
import type { TagFilterAST } from "@/lib/types";
import { cn } from "@/lib/utils";

interface SavedFilterItem {
  id: string;
  name: string;
  filter_ast: TagFilterAST;
  visibility: "PRIVATE" | "ORG";
}

interface SavedFilterListResponse {
  items: SavedFilterItem[];
}

interface TagListResponse {
  items: { id: string; key: string }[];
}

interface SavedFiltersMenuProps {
  // The current search-bar text; saving snapshots its tag tokens.
  query: string;
  // Applying a filter writes serialized `tag:` syntax back into the bar.
  onApply: (queryText: string) => void;
}

/**
 * Bookmark dropdown beside the tasks search bar: lists org-shared and
 * private saved filters, applies one as search text, and saves the current
 * query under a name. Filters persist STABLE TAG IDS (so they survive
 * renames/merges); names are resolved both ways through the tag list.
 */
export function SavedFiltersMenu({ query, onApply }: SavedFiltersMenuProps) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [visibility, setVisibility] = useState<"PRIVATE" | "ORG">("PRIVATE");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const { data: filters, mutate } = useSWR<SavedFilterListResponse>(
    open ? "/api/tag-filters" : null,
    fetcher,
    { revalidateOnFocus: false },
  );
  const { data: tags } = useSWR<TagListResponse>(
    open ? "/api/tags" : null,
    fetcher,
    { revalidateOnFocus: false },
  );

  const idToKey = new Map((tags?.items ?? []).map((t) => [t.id, t.key]));
  const keyToId = new Map(
    (tags?.items ?? []).map((t) => [t.key.toLowerCase(), t.id]),
  );

  const parsed = parseTaskSearch(query);
  const hasTagTokens =
    parsed.all.length > 0 || parsed.any.length > 0 || parsed.none.length > 0;

  function applyFilter(filter: SavedFilterItem) {
    // Stored tokens are stable ids (or legacy names); render ids back to
    // the tag's CURRENT name so the search bar stays human-readable.
    const toNames = (tokens: string[]) =>
      tokens.map((t) => idToKey.get(t) ?? t);
    onApply(
      serializeTaskSearch({
        text: "",
        all: toNames(filter.filter_ast.all ?? []),
        any: toNames(filter.filter_ast.any ?? []),
        none: toNames(filter.filter_ast.none ?? []),
      }),
    );
    setOpen(false);
  }

  async function saveCurrent() {
    if (!name.trim() || !hasTagTokens) return;
    setSaving(true);
    setError(null);
    // Persist ids where the name resolves; unknown tokens stay as names
    // (the browse resolver accepts both).
    const toIds = (tokens: string[]) =>
      tokens.map((t) => keyToId.get(t.toLowerCase()) ?? t);
    const res = await fetch("/api/tag-filters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name.trim(),
        visibility,
        filter_ast: {
          all: toIds(parsed.all),
          any: toIds(parsed.any),
          none: toIds(parsed.none),
        },
      }),
    });
    setSaving(false);
    if (!res.ok) {
      const body = (await res.json().catch(() => null)) as {
        detail?: string;
        error?: string;
      } | null;
      setError(body?.detail ?? body?.error ?? "Could not save filter.");
      return;
    }
    setName("");
    await mutate();
  }

  const items = filters?.items ?? [];
  const orgFilters = items.filter((f) => f.visibility === "ORG");
  const myFilters = items.filter((f) => f.visibility === "PRIVATE");

  function renderSection(label: string, section: SavedFilterItem[]) {
    if (section.length === 0) return null;
    return (
      <div>
        <p className="px-3 pt-2 pb-1 text-[10px] font-semibold tracking-wider text-muted-foreground uppercase">
          {label}
        </p>
        {section.map((f) => (
          <button
            key={f.id}
            type="button"
            className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-sm hover:bg-accent"
            onClick={() => applyFilter(f)}
          >
            <Tag
              className="h-3.5 w-3.5 shrink-0"
              style={{ color: tagColor(f.name) }}
              fill={tagColor(f.name)}
              fillOpacity={0.25}
            />
            <span className="truncate">{f.name}</span>
          </button>
        ))}
      </div>
    );
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 text-muted-foreground hover:text-foreground"
          aria-label="Saved filters"
          title="Saved filters"
        >
          <Bookmark className="h-4 w-4" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-0">
        {items.length === 0 ? (
          <p className="px-3 py-2 text-xs text-muted-foreground">
            No saved filters yet.
          </p>
        ) : (
          <div className="max-h-64 overflow-y-auto pb-1">
            {renderSection("Org filters", orgFilters)}
            {renderSection("My filters", myFilters)}
          </div>
        )}
        <div className="space-y-2 border-t p-3">
          {hasTagTokens ? (
            <>
              <p className="truncate font-mono text-[11px] text-muted-foreground">
                Save “{serializeTaskSearch({ ...parsed, text: "" })}”
              </p>
              <Input
                value={name}
                onChange={(event) => {
                  setName(event.target.value);
                  setError(null);
                }}
                placeholder="Filter name"
                className="h-7 text-xs"
                aria-label="Filter name"
              />
              <div className="flex items-center justify-between gap-2">
                <div className="flex gap-1">
                  {(["PRIVATE", "ORG"] as const).map((v) => (
                    <button
                      key={v}
                      type="button"
                      className={cn(
                        "rounded-md border px-2 py-0.5 text-[11px]",
                        visibility === v
                          ? "border-foreground/40 bg-accent"
                          : "border-transparent text-muted-foreground hover:bg-accent/50",
                      )}
                      onClick={() => setVisibility(v)}
                    >
                      {v === "PRIVATE" ? "Private" : "Org"}
                    </button>
                  ))}
                </div>
                <Button
                  size="sm"
                  className="h-6 px-2 text-xs"
                  disabled={saving || name.trim().length === 0}
                  onClick={saveCurrent}
                >
                  {saving ? "Saving…" : "Save"}
                </Button>
              </div>
              {error ? (
                <p className="text-xs text-destructive">{error}</p>
              ) : null}
            </>
          ) : (
            <p className="text-xs text-muted-foreground">
              Type a tag filter (e.g. <code>tag:flaky</code>) to save it.
            </p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
