"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { FileText, FlaskConical, Search, Tags as TagsIcon } from "lucide-react";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TagChip } from "@/components/tag-chip";
import { fetcher } from "@/lib/api";
import type { TagListResponse, TagSummary, UserTagRef } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Build the `tag:` search token used by the Tasks/Dashboard search bars. */
export function tagSearchQuery(tag: Pick<TagSummary, "key" | "value">): string {
  const name = tag.value ? `${tag.key}:${tag.value}` : tag.key;
  return `tag:${name}`;
}

/** Adapt a list row to the shape TagChip expects (an effective UserTagRef). */
function asChip(tag: TagSummary): UserTagRef {
  return {
    tag_id: tag.id,
    key: tag.key,
    value: tag.value,
    color: tag.color,
    visibility: tag.visibility,
    current: true,
    older: false,
  };
}

/**
 * A clickable association count that doubles as the filter for that scope,
 * styled as a small rounded pill to match the tag chip. Zero renders dimmed and
 * non-clickable (no dead links, no `NaN`). The two destinations are the only
 * places you can filter by tag: `/tasks` and the experiments dashboard.
 */
function AssociationCount({
  icon: Icon,
  count,
  singular,
  plural,
  href,
}: {
  icon: typeof FileText;
  count: number;
  singular: string;
  plural: string;
  href: string;
}) {
  const n = count ?? 0;
  const label = `${n} ${n === 1 ? singular : plural}`;
  const pill = (
    <Badge
      variant="outline"
      className={cn(
        // Fixed min-width + justify-start so the pills are a uniform size and
        // the two count columns line up vertically across rows.
        "w-[7.5rem] justify-start gap-1 px-2 py-0.5 text-xs font-normal whitespace-nowrap",
        n === 0
          ? "text-muted-foreground opacity-60"
          : "hover:bg-muted cursor-pointer transition-colors",
      )}
    >
      <Icon className="h-3 w-3 shrink-0" />
      {label}
    </Badge>
  );
  return n === 0 ? (
    pill
  ) : (
    <Link href={href} className="inline-flex">
      {pill}
    </Link>
  );
}

export function TagsPageClient({
  initialData,
}: {
  initialData: TagListResponse | null;
}) {
  const { data, error, isLoading } = useSWR<TagListResponse>(
    "/api/tags",
    fetcher,
    {
      fallbackData: initialData ?? undefined,
      // Refetch on navigation/focus so counts aren't stale from the SSR seed
      // after a tag is assigned on another page.
      revalidateOnMount: true,
      revalidateOnFocus: true,
    },
  );
  const [search, setSearch] = useState("");

  const tags = useMemo(() => {
    const items = data?.items ?? [];
    const q = search.trim().toLowerCase();
    const filtered = q
      ? items.filter(
          (t) =>
            t.key.toLowerCase().includes(q) ||
            (t.value ?? "").toLowerCase().includes(q) ||
            (t.owner_label ?? "").toLowerCase().includes(q),
        )
      : items;
    // Keep the backend's alphabetical order (ORDER BY normalized_key).
    return filtered;
  }, [data, search]);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <TagsIcon className="h-5 w-5" />
            Tags
          </h1>
          <p className="text-muted-foreground text-sm">
            Every tag in your workspace and where it&apos;s used. Click a count
            to see the matching tasks or experiments.
          </p>
        </div>
        <div className="relative w-full sm:w-72">
          <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 h-4 w-4 -translate-y-1/2" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search tags…"
            className="pl-8"
          />
        </div>
      </div>

      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Tag</TableHead>
                <TableHead>Associations</TableHead>
                <TableHead>Visibility</TableHead>
                <TableHead>Creator</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tags.map((tag) => (
                <TableRow key={tag.id}>
                  <TableCell>
                    <TagChip tag={asChip(tag)} />
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1.5">
                      <AssociationCount
                        icon={FileText}
                        count={tag.task_count}
                        singular="task"
                        plural="tasks"
                        href={`/tasks?query=${encodeURIComponent(
                          tagSearchQuery(tag),
                        )}`}
                      />
                      <AssociationCount
                        icon={FlaskConical}
                        count={tag.experiment_count}
                        singular="experiment"
                        plural="experiments"
                        href={`/dashboard?author=all&q=${encodeURIComponent(
                          tagSearchQuery(tag),
                        )}`}
                      />
                    </div>
                  </TableCell>
                  <TableCell>
                    <span className="text-muted-foreground text-xs">
                      {tag.visibility === "PUBLIC" ? "Public" : "Private"}
                    </span>
                  </TableCell>
                  <TableCell>
                    {tag.owner_label ? (
                      <span className="inline-flex items-center gap-1.5">
                        <Avatar className="h-5 w-5">
                          <AvatarImage
                            src={tag.owner_avatar_url ?? undefined}
                          />
                          <AvatarFallback className="text-[10px]">
                            {tag.owner_label[0]?.toUpperCase() ?? "?"}
                          </AvatarFallback>
                        </Avatar>
                        <span className="text-sm">{tag.owner_label}</span>
                      </span>
                    ) : (
                      <span className="text-muted-foreground text-xs">—</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          {isLoading && !data ? (
            <div className="text-muted-foreground p-6 text-center text-sm">
              Loading tags…
            </div>
          ) : null}
          {error ? (
            <div className="text-destructive p-6 text-center text-sm">
              Failed to load tags.
            </div>
          ) : null}
          {!isLoading && !error && tags.length === 0 ? (
            <div className="text-muted-foreground p-6 text-center text-sm">
              {search.trim()
                ? "No tags match your filter."
                : "No tags yet. Tags you create on tasks and experiments show up here."}
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
