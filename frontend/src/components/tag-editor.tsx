"use client";

import { useEffect, useMemo, useState } from "react";
import { Tag, X } from "lucide-react";

import { TagChip } from "@/components/tag-chip";
import { TagChipEditor } from "@/components/tag-chip-editor";
import { TagPicker, type TagPickerItem } from "@/components/tag-picker";
import { Button } from "@/components/ui/button";
import type { UserTagRef } from "@/lib/types";

export type TagScope = "VERSION" | "TASK" | "EXPERIMENT";

const PENDING_PREFIX = "pending:";

// Client-side approximation of the backend normalizer, used only to render
// the optimistic chip while the create call is in flight; the server's
// canonical key replaces it as soon as the response lands.
function previewKey(raw: string): string {
  return raw.trim().replace(/\s+/g, "-").toLowerCase();
}

interface TagEditorProps {
  scope: TagScope;
  targetId: string;
  taskId?: string | null;
  initialTags: UserTagRef[];
  experimentMode?: "snapshot" | "living";
  onMutate: () => void;
}

/**
 * Chips + add-tag picker with OPTIMISTIC local state: apply, create,
 * remove, and edit all update the rendered chips immediately; the API
 * calls run behind them and failures revert (with a brief inline error).
 * `initialTags` re-syncs the list only when the parent's data actually
 * changes content, so a slow refetch can't clobber an in-flight update.
 */
export function TagEditor({
  scope,
  targetId,
  taskId,
  initialTags,
  experimentMode,
  onMutate,
}: TagEditorProps) {
  const [tags, setTags] = useState<UserTagRef[]>(initialTags);
  const [error, setError] = useState<string | null>(null);
  const initialKey = useMemo(
    () =>
      initialTags
        .map(
          (t) =>
            `${t.tag_id}:${t.key}:${t.value ?? ""}:${t.color ?? ""}:` +
            `${t.visibility}:${t.current}:${t.older}`,
        )
        .sort()
        .join("|"),
    [initialTags],
  );
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setTags(initialTags), [initialKey]);

  function flashError(message: string) {
    setError(message);
    window.setTimeout(() => setError(null), 5000);
  }

  async function assign(tagId: string): Promise<boolean> {
    const res = await fetch("/api/tags/assign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tag_id: tagId,
        scope,
        target_id: targetId,
        task_id: taskId,
        mode: scope === "EXPERIMENT" ? experimentMode : undefined,
      }),
    });
    return res.ok;
  }

  async function applyTag(item: TagPickerItem) {
    const optimistic: UserTagRef = {
      tag_id: item.id,
      key: item.key,
      value: item.value,
      color: item.color,
      visibility: item.visibility,
      current: true,
      older: false,
    };
    setTags((prev) =>
      prev.some((t) => t.tag_id === item.id) ? prev : [...prev, optimistic],
    );
    if (!(await assign(item.id))) {
      setTags((prev) => prev.filter((t) => t.tag_id !== item.id));
      flashError(`Could not apply ${item.key}.`);
      return;
    }
    onMutate();
  }

  async function createAndApply(rawKey: string, color: string) {
    const pendingId = `${PENDING_PREFIX}${previewKey(rawKey)}`;
    const pending: UserTagRef = {
      tag_id: pendingId,
      key: previewKey(rawKey),
      value: null,
      color,
      visibility: "PRIVATE",
      current: true,
      older: false,
    };
    setTags((prev) =>
      prev.some((t) => t.tag_id === pendingId) ? prev : [...prev, pending],
    );

    const res = await fetch("/api/tags", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: rawKey, color, visibility: "PRIVATE" }),
    });
    const body = (await res.json().catch(() => null)) as
      | (TagPickerItem & { detail?: string; error?: string })
      | null;
    if (!res.ok || !body?.id) {
      setTags((prev) => prev.filter((t) => t.tag_id !== pendingId));
      flashError(body?.detail ?? body?.error ?? `Could not create ${rawKey}.`);
      return;
    }
    // Swap the pending chip for the canonical row, then attach it.
    setTags((prev) =>
      prev.map((t) =>
        t.tag_id === pendingId
          ? { ...t, tag_id: body.id, key: body.key, color: body.color }
          : t,
      ),
    );
    if (!(await assign(body.id))) {
      setTags((prev) => prev.filter((t) => t.tag_id !== body.id));
      flashError(`Could not apply ${body.key}.`);
      return;
    }
    onMutate();
  }

  async function removeTag(tagId: string) {
    const removed = tags.find((t) => t.tag_id === tagId);
    setTags((prev) => prev.filter((t) => t.tag_id !== tagId));
    if (tagId.startsWith(PENDING_PREFIX)) return;
    const res = await fetch("/api/tags/unassign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tag_id: tagId,
        scope,
        target_id: targetId,
        task_id: taskId,
      }),
    });
    if (!res.ok && removed) {
      setTags((prev) =>
        prev.some((t) => t.tag_id === tagId) ? prev : [...prev, removed],
      );
      flashError(`Could not remove ${removed.key}.`);
      return;
    }
    onMutate();
  }

  function handleEdited(
    tagId: string,
    patch: Pick<UserTagRef, "key" | "color">,
  ) {
    setTags((prev) =>
      prev.map((t) => (t.tag_id === tagId ? { ...t, ...patch } : t)),
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-1">
      {tags.map((t) => {
        const pending = t.tag_id.startsWith(PENDING_PREFIX);
        return (
          <span key={t.tag_id} className="flex items-center">
            {pending ? (
              <TagChip tag={t} className="opacity-70" />
            ) : (
              <TagChipEditor
                tag={t}
                onSaved={onMutate}
                onEdited={(patch) => handleEdited(t.tag_id, patch)}
                onRemove={() => removeTag(t.tag_id)}
                onDeleted={() =>
                  // Vocabulary delete: drop the chip locally; no unassign
                  // call — resolution already excludes DELETED tags.
                  setTags((prev) => prev.filter((x) => x.tag_id !== t.tag_id))
                }
              />
            )}
            <button
              type="button"
              className="ml-0.5 rounded-full p-0.5 text-muted-foreground transition-colors hover:bg-destructive/15 hover:text-destructive"
              onClick={() => removeTag(t.tag_id)}
              aria-label={`Remove ${t.key}`}
              title={`Remove ${t.key}`}
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        );
      })}
      <TagPicker
        selectedTagIds={[]}
        onChange={() => {
          // Selection is applied via onSelectItem (full item needed for the
          // optimistic chip); the id-only callback is intentionally unused.
        }}
        onSelectItem={applyTag}
        onCreateRequest={createAndApply}
        multi={false}
        allowCreate
        trigger={
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-muted-foreground hover:text-foreground"
            aria-label="Add tag"
            title="Add tag"
          >
            <Tag className="h-3.5 w-3.5" />
          </Button>
        }
      />
      {error ? <span className="text-xs text-destructive">{error}</span> : null}
    </div>
  );
}
