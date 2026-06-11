"use client";

import { Tag } from "lucide-react";

import { TagChipEditor } from "@/components/tag-chip-editor";
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
    <div className="flex flex-wrap items-center gap-1">
      {initialTags.map((t) => (
        <span key={t.tag_id} className="flex items-center gap-1">
          <TagChipEditor tag={t} onSaved={onMutate} />
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
        onChange={(picked) => {
          if (picked[0]) applyTag(picked[0]);
        }}
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
