"use client";

import { useState } from "react";
import { Pencil } from "lucide-react";
import { encodeExperimentRouteParam } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { MarkdownRenderer } from "@/components/renderers/markdown-renderer";

interface ExperimentDescriptionProps {
  /** Required to edit; omit (or pass readOnly) for the public view. */
  experimentId?: string;
  /** Current description value (caller owns the read). */
  description: string | null;
  /** Hide all edit affordances (public share page / non-editors). */
  readOnly?: boolean;
  /** Called with the saved value so the caller can update its cache. */
  onSaved?: (next: string | null) => void;
}

export function ExperimentDescription({
  experimentId,
  description,
  readOnly = false,
  onSaved,
}: ExperimentDescriptionProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState(description ?? "");
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canEdit = !readOnly && Boolean(experimentId);

  const startEditing = () => {
    setDraft(description ?? "");
    setError(null);
    setIsEditing(true);
  };

  const cancelEditing = () => {
    setDraft(description ?? "");
    setError(null);
    setIsEditing(false);
  };

  const handleSave = async () => {
    if (!experimentId) return;
    // Send only `description` so the partial-update endpoint leaves the
    // name untouched. Blank/whitespace clears the description (NULL).
    const next = draft.trim() ? draft.trim() : null;

    setIsSaving(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/experiments/${encodeExperimentRouteParam(experimentId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ description: next }),
        },
      );

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail || data.error || "Failed to save description",
        );
      }

      setIsEditing(false);
      onSaved?.(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setIsSaving(false);
    }
  };

  if (isEditing) {
    return (
      <div className="flex flex-col gap-2">
        <Textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Add a description (markdown supported)…"
          rows={6}
          autoFocus
          disabled={isSaving}
          className="min-h-[140px] border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] font-mono text-[13px] leading-relaxed"
        />
        {error ? (
          <span className="text-xs text-destructive">{error}</span>
        ) : null}
        <div className="flex items-center gap-2">
          <Button
            type="button"
            size="sm"
            className="h-8"
            onClick={handleSave}
            disabled={isSaving}
          >
            {isSaving ? "Saving..." : "Save"}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8"
            onClick={cancelEditing}
            disabled={isSaving}
          >
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  // Empty state: offer an affordance to editors; render nothing for viewers.
  if (!description) {
    if (!canEdit) return null;
    return (
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={startEditing}
        className="h-7 w-fit gap-1.5 rounded-sm px-2 font-mono text-[12px] text-[color:var(--paper-ink-3)] transition hover:bg-[color:var(--paper-surface-2)] hover:text-[color:var(--paper-ink)]"
      >
        <Pencil className="h-3 w-3" />
        Add a description
      </Button>
    );
  }

  // Display mode with content.
  return (
    <div className="group relative rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)]">
      <MarkdownRenderer content={description} />
      {canEdit ? (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={startEditing}
          className="absolute right-2 top-2 h-6 w-6 rounded-sm text-[color:var(--paper-ink-3)] opacity-0 transition hover:bg-[color:var(--paper-surface-2)] hover:text-[color:var(--paper-ink)] group-hover:opacity-100"
          aria-label="Edit description"
          title="Edit description"
        >
          <Pencil className="h-3.5 w-3.5" />
        </Button>
      ) : null}
    </div>
  );
}
