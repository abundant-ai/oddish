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
import { TagColorBar } from "@/components/tag-color-bar";
import { fetcher } from "@/lib/api";
import { tagColor } from "@/lib/tag-colors";
import type { UserTagRef } from "@/lib/types";

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
  // Fires with the server's final key/color (it may normalize the typed
  // name) so the parent can patch its optimistic chip list instantly.
  onEdited?: (patch: Pick<UserTagRef, "key" | "color">) => void;
  // Unassigns the tag from the current target (not a vocabulary delete).
  onRemove?: () => void;
}

/**
 * A TagChip that opens a small editor on click: rename + palette recolor.
 * Edits go to the single `tags` vocabulary row, so every chip referencing
 * the tag updates org-wide. Rename/recolor are definition-plane operations
 * — the backend enforces owner/admin/grant permissions and optimistic
 * concurrency (`expected_row_version`), and rejections surface inline.
 */
export function TagChipEditor({
  tag,
  onSaved,
  onEdited,
  onRemove,
}: TagChipEditorProps) {
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
    const updated = (await res.json().catch(() => null)) as TagListItem | null;
    onEdited?.({
      key: updated?.key ?? name.trim(),
      color: updated?.color ?? effectiveColor,
    });
    setOpen(false);
    onSaved();
    void mutateTags();
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
        <TagColorBar value={effectiveColor} onChange={setColor} />
        {error ? <p className="text-xs text-destructive">{error}</p> : null}
        <div className="flex items-center justify-between gap-2">
          {onRemove ? (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs text-destructive hover:bg-destructive/15 hover:text-destructive"
              onClick={() => {
                setOpen(false);
                onRemove();
              }}
            >
              Remove
            </Button>
          ) : (
            <span />
          )}
          <div className="flex gap-2">
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
        </div>
      </PopoverContent>
    </Popover>
  );
}
