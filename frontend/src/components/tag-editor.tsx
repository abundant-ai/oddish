"use client";

import { useEffect, useMemo, useState } from "react";
import { Tag } from "lucide-react";

import { TagChipEditor } from "@/components/tag-chip-editor";
import { TagPicker, type TagPickerItem } from "@/components/tag-picker";
import { Button } from "@/components/ui/button";
import type { UserTagRef } from "@/lib/types";

export type TagScope = "VERSION" | "TASK" | "EXPERIMENT";

interface TagEditorProps {
  scope: TagScope;
  targetId: string;
  taskId?: string | null;
  initialTags: UserTagRef[];
  experimentMode?: "snapshot" | "living";
  onMutate: () => void;
}

/**
 * Chips + add-tag picker with OPTIMISTIC local state: apply, remove, and
 * edit update the rendered chips immediately, the API call runs behind it,
 * and a failure reverts. `initialTags` re-syncs the list whenever the
 * parent's data actually changes content (keyed below, so unrelated parent
 * re-renders can't clobber an in-flight optimistic update).
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
  const initialKey = useMemo(
    () =>
      initialTags
        .map((t) => `${t.tag_id}:${t.key}:${t.color ?? ""}`)
        .sort()
        .join("|"),
    [initialTags],
  );
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setTags(initialTags), [initialKey]);

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
    const res = await fetch("/api/tags/assign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tag_id: item.id,
        scope,
        target_id: targetId,
        task_id: taskId,
        mode: scope === "EXPERIMENT" ? experimentMode : undefined,
      }),
    });
    if (!res.ok) {
      setTags((prev) => prev.filter((t) => t.tag_id !== item.id));
      return;
    }
    onMutate();
  }

  async function removeTag(tagId: string) {
    const removed = tags.find((t) => t.tag_id === tagId);
    setTags((prev) => prev.filter((t) => t.tag_id !== tagId));
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
      return;
    }
    onMutate();
  }

  function handleEdited(tagId: string, patch: Pick<UserTagRef, "key" | "color">) {
    setTags((prev) =>
      prev.map((t) => (t.tag_id === tagId ? { ...t, ...patch } : t)),
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-1">
      {tags.map((t) => (
        <span key={t.tag_id} className="flex items-center gap-1">
          <TagChipEditor
            tag={t}
            onSaved={onMutate}
            onEdited={(patch) => handleEdited(t.tag_id, patch)}
          />
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground"
            onClick={() => removeTag(t.tag_id)}
            aria-label={`Remove ${t.key}`}
          >
            ×
          </button>
        </span>
      ))}
      <TagPicker
        selectedTagIds={[]}
        onChange={() => {
          // Selection is applied via onSelectItem (full item needed for the
          // optimistic chip); the id-only callback is intentionally unused.
        }}
        onSelectItem={applyTag}
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
    </div>
  );
}
