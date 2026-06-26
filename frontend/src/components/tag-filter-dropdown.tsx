"use client";

import Link from "next/link";
import { useMemo } from "react";
import useSWR from "swr";
import { ArrowRight, Check, ChevronDown, Tag as TagIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandList,
  CommandItem,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { fetcher } from "@/lib/api";
import { tagColor } from "@/lib/tag-colors";
import { parseTaskSearch, serializeTaskSearch } from "@/lib/tag-query";
import type { TagListResponse, TagSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

/** The `tag:` token form the search grammar expects for a tag. */
function tagToken(tag: Pick<TagSummary, "key" | "value">): string {
  return tag.value ? `${tag.key}:${tag.value}` : tag.key;
}

/**
 * Multi-select tag picker that filters the page by editing the `tag:` tokens in
 * the existing search query. The query is the single source of truth: selected
 * state is derived from it, and toggling a tag rewrites it via
 * serialize/parse — so free text, author, and OR/NOT tokens are preserved.
 *
 * `countField` is the context count shown per row (task vs experiment).
 */
export function TagFilterDropdown({
  query,
  onQueryChange,
  countField,
  className,
}: {
  query: string;
  onQueryChange: (next: string) => void;
  countField: "task_count" | "experiment_count";
  className?: string;
}) {
  const { data } = useSWR<TagListResponse>("/api/tags", fetcher, {
    revalidateOnFocus: false,
  });
  const tags = useMemo(
    () => (data?.items ?? []).filter((t) => t.state === "ACTIVE"),
    [data],
  );

  // Any tag token currently in the query counts as selected (all/any/none).
  const selected = useMemo(() => {
    const p = parseTaskSearch(query);
    return new Set([...p.all, ...p.any, ...p.none]);
  }, [query]);

  function toggle(tag: TagSummary) {
    const name = tagToken(tag);
    const p = parseTaskSearch(query);
    const has =
      p.all.includes(name) || p.any.includes(name) || p.none.includes(name);
    const next = has
      ? {
          ...p,
          all: p.all.filter((n) => n !== name),
          any: p.any.filter((n) => n !== name),
          none: p.none.filter((n) => n !== name),
        }
      : { ...p, all: [...p.all, name] };
    onQueryChange(serializeTaskSearch(next).trim());
  }

  const selectedCount = selected.size;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={cn(
            "h-8 justify-between gap-1.5 border-[#6f88b4]/20",
            className,
          )}
        >
          <span className="flex items-center gap-1.5">
            <TagIcon className="h-3.5 w-3.5 opacity-60" />
            {selectedCount > 0
              ? `${selectedCount} tag${selectedCount === 1 ? "" : "s"}`
              : "Tags"}
          </span>
          <ChevronDown className="h-4 w-4 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-64 p-0" align="end">
        <Command>
          <CommandInput placeholder="Filter tags…" />
          <CommandList>
            <CommandEmpty>No tags.</CommandEmpty>
            <CommandGroup>
              {tags.map((t) => {
                const name = tagToken(t);
                const color = tagColor(t.key, t.color);
                return (
                  <CommandItem
                    key={t.id}
                    value={name}
                    onSelect={() => toggle(t)}
                  >
                    <Check
                      className={cn(
                        "mr-1.5 h-3.5 w-3.5 shrink-0",
                        selected.has(name) ? "opacity-100" : "opacity-0",
                      )}
                    />
                    <TagIcon
                      className="mr-1.5 h-3.5 w-3.5 shrink-0"
                      style={{ color }}
                      fill={color}
                      fillOpacity={0.25}
                    />
                    <span className="truncate">{name}</span>
                    <span className="text-muted-foreground ml-auto pl-2 text-xs tabular-nums">
                      {t[countField]}
                    </span>
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </CommandList>
          <div className="border-t p-1">
            <Button
              variant="ghost"
              size="sm"
              asChild
              className="text-muted-foreground h-7 w-full justify-between text-xs"
            >
              <Link href="/tags">
                See all tags
                <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </Button>
          </div>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
