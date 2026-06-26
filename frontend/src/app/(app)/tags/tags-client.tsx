"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import {
  FileText,
  FlaskConical,
  Plus,
  Search,
  Tags as TagsIcon,
} from "lucide-react";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TagChip } from "@/components/tag-chip";
import { TagColorBar } from "@/components/tag-color-bar";
import { fetcher } from "@/lib/api";
import { tagColor } from "@/lib/tag-colors";
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

/** Define a new tag (without assigning it). Refreshes the list on success. */
function CreateTagDialog({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [color, setColor] = useState<string | null>(null);
  const [visibility, setVisibility] = useState<"PRIVATE" | "PUBLIC">("PRIVATE");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const key = name.trim();
  const effectiveColor = color ?? tagColor(key || "tag");

  function reset() {
    setName("");
    setColor(null);
    setVisibility("PRIVATE");
    setError(null);
  }

  async function submit() {
    if (!key || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch("/api/tags", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, color: effectiveColor, visibility }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(body?.detail ?? body?.error ?? "Failed to create tag.");
        return;
      }
      setOpen(false);
      reset();
      onCreated();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) reset();
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" className="gap-1.5">
          <Plus className="h-4 w-4" />
          New tag
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Create tag</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="new-tag-name">Name</Label>
            <Input
              id="new-tag-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. smoke-test"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
              }}
            />
          </div>
          <div className="space-y-1.5">
            <Label>Color</Label>
            <TagColorBar value={effectiveColor} onChange={setColor} />
          </div>
          <div className="space-y-1.5">
            <Label>Visibility</Label>
            <Select
              value={visibility}
              onValueChange={(v) => setVisibility(v as "PRIVATE" | "PUBLIC")}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="PRIVATE">Private</SelectItem>
                <SelectItem value="PUBLIC">Public</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {error ? <p className="text-destructive text-sm">{error}</p> : null}
        </div>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => setOpen(false)}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button onClick={submit} disabled={!key || submitting}>
            {submitting ? "Creating…" : "Create tag"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function TagsPageClient({
  initialData,
}: {
  initialData: TagListResponse | null;
}) {
  const { data, error, isLoading, mutate } = useSWR<TagListResponse>(
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
        <div className="flex w-full items-center gap-2 sm:w-auto">
          <div className="relative w-full sm:w-72">
            <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 h-4 w-4 -translate-y-1/2" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search tags…"
              className="pl-8"
            />
          </div>
          <CreateTagDialog onCreated={() => mutate()} />
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
