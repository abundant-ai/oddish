"use client";

import { useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Bookmark, Tag, X } from "lucide-react";
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
import { FILTER_PARAM_KEYS } from "@/lib/tasks-filters";
import type { TagListResponse, TagSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

interface SavedFilterItem {
  id: string;
  name: string;
  filter_ast: Record<string, unknown>;
  visibility: "PRIVATE" | "ORG";
}

interface SavedFilterListResponse {
  items: SavedFilterItem[];
}

// Versioned blob stored in `filter_ast`. v2 captures the whole sidebar config;
// tags are persisted as STABLE IDS so they survive renames/merges. Legacy blobs
// have no `v` and only carry `{ all, any, none }` tag ids.
type SavedBlobV2 = {
  v: 2;
  q?: string;
  params?: Record<string, string>;
  tags?: { all?: string[]; any?: string[]; none?: string[] };
};
type LegacyBlob = { all?: string[]; any?: string[]; none?: string[] };

const TAG_PARAM_KEYS = ["tags", "tags_any", "tags_none"] as const;
const STRUCTURED_KEYS = FILTER_PARAM_KEYS.filter(
  (k) => !TAG_PARAM_KEYS.includes(k as (typeof TAG_PARAM_KEYS)[number])
);

function tagToken(tag: Pick<TagSummary, "key" | "value">): string {
  return tag.value ? `${tag.key}:${tag.value}` : tag.key;
}

function csv(value: string | null): string[] {
  return value ? value.split(",").filter(Boolean) : [];
}

/**
 * Bookmark dropdown in the filter sidebar: lists org-shared and private saved
 * filters, applies one by rewriting the URL, and saves the CURRENT full filter
 * config (search text + structured filters + tags). Tags persist as stable ids
 * (so they survive renames/merges); ids are resolved both ways via the tag list.
 */
export function SavedFiltersMenu() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [visibility, setVisibility] = useState<"PRIVATE" | "ORG">("PRIVATE");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const {
    data: filters,
    mutate,
    isLoading,
    error: loadError,
  } = useSWR<SavedFilterListResponse>(
    open ? "/api/tag-filters" : null,
    fetcher,
    { revalidateOnFocus: false }
  );
  const { data: tagData } = useSWR<TagListResponse>(
    open ? "/api/tags" : null,
    fetcher,
    { revalidateOnFocus: false }
  );

  const tags = tagData?.items ?? [];
  const idToToken = new Map(tags.map((t) => [t.id, tagToken(t)]));
  const tokenToId = new Map(tags.map((t) => [tagToken(t), t.id]));
  const toIds = (tokens: string[]) => tokens.map((t) => tokenToId.get(t) ?? t);
  const toTokens = (ids: string[]) => ids.map((id) => idToToken.get(id) ?? id);

  // Snapshot the current URL filter state.
  const currentQ = (searchParams.get("q") ?? "").trim();
  const currentParams: Record<string, string> = {};
  for (const key of STRUCTURED_KEYS) {
    const value = searchParams.get(key);
    if (value) currentParams[key] = value;
  }
  const currentTags = {
    all: csv(searchParams.get("tags")),
    any: csv(searchParams.get("tags_any")),
    none: csv(searchParams.get("tags_none")),
  };
  const hasActive =
    currentQ.length > 0 ||
    Object.keys(currentParams).length > 0 ||
    currentTags.all.length > 0 ||
    currentTags.any.length > 0 ||
    currentTags.none.length > 0;

  function applyFilter(filter: SavedFilterItem) {
    const ast = filter.filter_ast as SavedBlobV2 & LegacyBlob;
    const params = new URLSearchParams();
    const setTags = (
      tagIds: { all?: string[]; any?: string[]; none?: string[] } | undefined
    ) => {
      const all = toTokens(tagIds?.all ?? []);
      const any = toTokens(tagIds?.any ?? []);
      const none = toTokens(tagIds?.none ?? []);
      if (all.length) params.set("tags", all.join(","));
      if (any.length) params.set("tags_any", any.join(","));
      if (none.length) params.set("tags_none", none.join(","));
    };

    if (ast.v === 2) {
      if (ast.q) params.set("q", ast.q);
      for (const [key, value] of Object.entries(ast.params ?? {})) {
        params.set(key, String(value));
      }
      setTags(ast.tags);
    } else {
      // Legacy tag-only blob.
      setTags({ all: ast.all, any: ast.any, none: ast.none });
    }

    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
    setOpen(false);
  }

  async function saveCurrent() {
    if (!name.trim() || !hasActive) return;
    setSaving(true);
    setError(null);
    const filterAst: SavedBlobV2 = {
      v: 2,
      ...(currentQ ? { q: currentQ } : {}),
      ...(Object.keys(currentParams).length ? { params: currentParams } : {}),
      tags: {
        all: toIds(currentTags.all),
        any: toIds(currentTags.any),
        none: toIds(currentTags.none),
      },
    };
    const res = await fetch("/api/tag-filters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name.trim(),
        visibility,
        filter_ast: filterAst,
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

  async function deleteFilter(filterId: string) {
    setError(null);
    const withoutFilter = (current?: SavedFilterListResponse) => ({
      items: (current?.items ?? []).filter((f) => f.id !== filterId),
    });
    try {
      await mutate(
        async () => {
          const res = await fetch(
            `/api/tag-filters/${encodeURIComponent(filterId)}`,
            { method: "DELETE" }
          );
          if (!res.ok && res.status !== 404) {
            throw new Error(`Delete failed: ${res.status}`);
          }
          return undefined;
        },
        {
          optimisticData: withoutFilter,
          populateCache: (_res, current) => withoutFilter(current),
          rollbackOnError: true,
          revalidate: false,
        }
      );
    } catch {
      setError("Could not delete filter.");
    }
  }

  function renderSection(
    label: string,
    section: SavedFilterItem[],
    deletable: boolean
  ) {
    if (section.length === 0) return null;
    return (
      <div>
        <p className="text-muted-foreground px-3 pt-2 pb-1 text-[10px] font-semibold tracking-wider uppercase">
          {label}
        </p>
        {section.map((f) => (
          <div key={f.id} className="hover:bg-accent flex items-center">
            <button
              type="button"
              className="flex min-w-0 flex-1 items-center gap-1.5 px-3 py-1.5 text-left text-sm"
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
            {deletable ? (
              <button
                type="button"
                className="text-muted-foreground hover:bg-destructive/15 hover:text-destructive mr-2 rounded-full p-0.5"
                aria-label={`Delete filter ${f.name}`}
                title={`Delete ${f.name}`}
                onClick={() => deleteFilter(f.id)}
              >
                <X className="h-3 w-3" />
              </button>
            ) : null}
          </div>
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
          className="text-muted-foreground hover:text-foreground h-8 w-8"
          aria-label="Saved filters"
          title="Saved filters"
        >
          <Bookmark className="h-4 w-4" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="z-30 w-72 p-0">
        {isLoading ? (
          <p className="text-muted-foreground px-3 py-2 text-xs">
            Loading filters…
          </p>
        ) : loadError && !filters ? (
          <p className="text-destructive px-3 py-2 text-xs">
            Could not load filters.
          </p>
        ) : items.length === 0 ? (
          <p className="text-muted-foreground px-3 py-2 text-xs">
            No saved filters yet.
          </p>
        ) : (
          <div className="max-h-64 overflow-y-auto pb-1">
            {renderSection("Org filters", orgFilters, false)}
            {renderSection("My filters", myFilters, true)}
          </div>
        )}
        <div className="space-y-2 border-t p-3">
          {hasActive ? (
            <>
              <p className="text-muted-foreground text-[11px]">
                Save the current filters as a named view.
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
                          : "text-muted-foreground hover:bg-accent/50 border-transparent"
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
                <p className="text-destructive text-xs">{error}</p>
              ) : null}
            </>
          ) : (
            <p className="text-muted-foreground text-xs">
              Apply some filters to save them as a view.
            </p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
