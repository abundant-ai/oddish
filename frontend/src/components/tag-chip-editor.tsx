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
  usage_count: number;
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
  // Fires after the tag DEFINITION is deleted org-wide, so the parent can
  // drop the chip locally without issuing a redundant unassign call.
  onDeleted?: () => void;
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
  onDeleted,
}: TagChipEditorProps) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(tag.key);
  const [color, setColor] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
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

  async function deleteTag() {
    if (!listRow) return;
    setDeleting(true);
    setError(null);
    // Soft delete of the vocabulary row: reads everywhere drop the tag via
    // state filtering. cascade=true carries the confirm panel's consent to
    // flip the ACTIVE assignments too (the backend rejects assigned-tag
    // deletion without it). row_version guards against deleting over
    // someone's concurrent rename.
    const res = await fetch(
      `/api/tags/${encodeURIComponent(tag.tag_id)}?cascade=true`,
      {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_row_version: listRow.row_version }),
      },
    );
    setDeleting(false);
    if (!res.ok) {
      const body = (await res.json().catch(() => null)) as {
        detail?: string;
        error?: string;
      } | null;
      setError(body?.detail ?? body?.error ?? "Could not delete tag.");
      setConfirmingDelete(false);
      await mutateTags();
      return;
    }
    setOpen(false);
    onDeleted?.();
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
          setConfirmingDelete(false);
        }
      }}
    >
      <PopoverTrigger asChild>
        <button type="button" aria-label={`Edit tag ${tag.key}`}>
          <TagChip tag={tag} className="cursor-pointer hover:bg-accent/40" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-60 space-y-2 p-3">
        {confirmingDelete ? (
          <div className="space-y-2 rounded-md border border-destructive/30 bg-destructive/10 p-2">
            <p className="text-xs">
              {/* listRow first: after a 409 the list refetches, so a rename
                  that raced us shows its CURRENT name before the retry. */}
              Delete{" "}
              <span className="font-medium">{listRow?.key ?? tag.key}</span>{" "}
              for the entire org? Every item carrying this tag loses it.
            </p>
            {/* Usage shown before the destructive click — the Linear
                pattern: give the number that should inform the decision.
                usage_count = direct ACTIVE assignments (living-experiment
                fanout can touch more items than this). */}
            <p className="text-[11px] text-muted-foreground">
              Used by {listRow?.usage_count ?? 0} direct{" "}
              {(listRow?.usage_count ?? 0) === 1 ? "assignment" : "assignments"}.
            </p>
            {error ? <p className="text-xs text-destructive">{error}</p> : null}
            <div className="flex justify-end gap-2">
              <Button
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-xs"
                onClick={() => setConfirmingDelete(false)}
              >
                Keep
              </Button>
              <Button
                variant="destructive"
                size="sm"
                className="h-6 px-2 text-xs"
                disabled={deleting || !listRow}
                onClick={deleteTag}
              >
                {deleting ? "Deleting…" : "Delete tag"}
              </Button>
            </div>
          </div>
        ) : (
          <>
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
            <button
              type="button"
              className="w-full border-t pt-1.5 text-left text-[11px] text-muted-foreground transition-colors hover:text-destructive"
              onClick={() => {
                setError(null);
                setConfirmingDelete(true);
              }}
            >
              Delete tag for everyone…
            </button>
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}
