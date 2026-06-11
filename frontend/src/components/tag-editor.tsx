"use client";

import { useState } from "react";

import { TagChip } from "@/components/tag-chip";
import { TagPicker } from "@/components/tag-picker";
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

export function TagEditor({
  scope,
  targetId,
  taskId,
  initialTags,
  experimentMode,
  onMutate,
}: TagEditorProps) {
  const [pickerOpen, setPickerOpen] = useState(false);

  async function applyTag(tagId: string) {
    await fetch("/api/tags/assign", {
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
    onMutate();
  }

  async function removeTag(tagId: string) {
    await fetch("/api/tags/unassign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tag_id: tagId,
        scope,
        target_id: targetId,
        task_id: taskId,
      }),
    });
    onMutate();
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1">
        {initialTags.map((t) => (
          <span key={t.tag_id} className="flex items-center gap-1">
            <TagChip tag={t} />
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
      </div>
      <div>
        {pickerOpen ? (
          <TagPicker
            selectedTagIds={[]}
            onChange={(picked) => {
              if (picked[0]) applyTag(picked[0]);
              setPickerOpen(false);
            }}
            multi={false}
            allowCreate
          />
        ) : (
          <Button variant="outline" size="sm" onClick={() => setPickerOpen(true)}>
            + Tag
          </Button>
        )}
      </div>
    </div>
  );
}
