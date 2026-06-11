"use client";

import { useState } from "react";
import useSWR from "swr";

import { TagChip } from "@/components/tag-chip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { fetcher } from "@/lib/api";
import { TAG_COLOR_PALETTE, tagColor } from "@/lib/tag-colors";
import type { UserTagRef } from "@/lib/types";
import { cn } from "@/lib/utils";

interface TagListItem {
  id: string;
  key: string;
  color: string | null;
  row_version: number;
}

interface TagListResponse {
  items: TagListItem[];
}

interface TagChipEditorProps {
  tag: UserTagRef;
  onSaved: () => void;
}

/**
 * A TagChip that opens a small editor on click: rename + palette recolor.
 * Edits go to the single `tags` vocabulary row, so every chip referencing
 * the tag updates org-wide. Rename/recolor are definition-plane operations
 * — the backend enforces owner/admin/grant permissions and optimistic
 * concurrency (`expected_row_version`), and rejections surface inline.
 */
export function TagChipEditor({ tag, onSaved }: TagChipEditorProps) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(tag.key);
  const [color, setColor] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { data, mutate: mutateTags } = useSWR<TagListResponse>(
    open ? "/api/tags" : null,
    fetcher,
    { revalidateOnFocus: false },
  );

  const listRow = data?.items.find((it) => it.id === tag.tag_id);
  const effectiveColor = color ?? tagColor(tag.key, tag.color);

  async function save() {
    if (!listRow) return;
    setSaving(true);
    setError(null);
    const res = await fetch(`/api/tags/${encodeURIComponent(tag.tag_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: name.trim() !== tag.key ? name.trim() : undefined,
        color: effectiveColor,
        expected_row_version: listRow.row_version,
      }),
    });
    setSaving(false);
    if (!res.ok) {
      const body = (await res.json().catch(() => null)) as {
        detail?: string;
        error?: string;
      } | null;
      setError(body?.detail ?? body?.error ?? "Could not update tag.");
      await mutateTags();
      return;
    }
    await mutateTags();
    onSaved();
    setOpen(false);
  }

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) {
          setName(tag.key);
          setColor(null);
          setError(null);
        }
      }}
    >
      <PopoverTrigger asChild>
        <button type="button" aria-label={`Edit tag ${tag.key}`}>
          <TagChip tag={tag} className="cursor-pointer hover:bg-accent/40" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-60 space-y-2 p-3">
        <Input
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            setError(null);
          }}
          className="h-7 text-xs"
          aria-label="Tag name"
        />
        <div className="flex items-center gap-1.5">
          {TAG_COLOR_PALETTE.map((swatch) => (
            <button
              key={swatch}
              type="button"
              aria-label={`Tag color ${swatch}`}
              className={cn(
                "h-4 w-4 rounded-full transition-transform hover:scale-110",
                swatch === effectiveColor &&
                  "ring-2 ring-ring ring-offset-1 ring-offset-popover",
              )}
              style={{ backgroundColor: swatch }}
              onClick={() => setColor(swatch)}
            />
          ))}
        </div>
        {error ? <p className="text-xs text-destructive">{error}</p> : null}
        <div className="flex justify-end gap-2">
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-xs"
            onClick={() => setOpen(false)}
          >
            Cancel
          </Button>
          <Button
            size="sm"
            className="h-6 px-2 text-xs"
            disabled={saving || !listRow || name.trim().length === 0}
            onClick={save}
          >
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
