"use client";

import { useEffect, useRef, useState } from "react";
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

const PILL_BUTTON_CLASS =
  "h-8 select-none gap-[7px] rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px] leading-none text-[color:var(--paper-ink)] transition-colors hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-[color:var(--paper-line)] disabled:hover:bg-[color:var(--paper-surface)]";

// Matches the meta strip (created · by · id) font so the "…more"/"see less"
// control and the "add a description" affordance sit naturally beneath it.
const META_TEXT_CLASS =
  "w-fit cursor-pointer font-mono text-[11.5px] text-[color:var(--paper-ink-3)] transition-colors hover:text-[color:var(--paper-ink)]";

const COLLAPSED_MAX_HEIGHT_PX = 104;

function extractErrorMessage(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const { detail, error } = data as Record<string, unknown>;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) =>
        d && typeof d === "object" && typeof (d as { msg?: unknown }).msg === "string"
          ? (d as { msg: string }).msg
          : null,
      )
      .filter((m): m is string => Boolean(m));
    if (msgs.length) return msgs.join("; ");
  }
  if (typeof error === "string") return error;
  return null;
}

export function ExperimentDescription({
  experimentId,
  description,
  readOnly = false,
  onSaved,
}: ExperimentDescriptionProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);
  const [isOverflowing, setIsOverflowing] = useState(false);
  const [draft, setDraft] = useState(description ?? "");
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);

  const canEdit = !readOnly && Boolean(experimentId);

  // Decide whether the rendered description overflows the collapsed height.
  useEffect(() => {
    const el = cardRef.current;
    if (!el) return;
    const measure = () => {
      if (!el.isConnected) return;
      setIsOverflowing(el.scrollHeight > COLLAPSED_MAX_HEIGHT_PX);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [description, isEditing]);

  const startEditing = () => {
    setDraft(description ?? "");
    setError(null);
    setIsExpanded(true);
    setIsEditing(true);
  };

  const cancelEditing = () => {
    setDraft(description ?? "");
    setError(null);
    setIsEditing(false);
  };

  const handleSave = async () => {
    if (!experimentId) return;
    const trimmed = draft.trim();

    setIsSaving(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/experiments/${encodeExperimentRouteParam(experimentId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ description: trimmed }),
        },
      );

      if (!res.ok) {
        const data = await res.json().catch(() => null);
        throw new Error(
          extractErrorMessage(data) || "Failed to save description",
        );
      }

      setIsEditing(false);
      onSaved?.(trimmed || null);
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
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              cancelEditing();
            }
          }}
          placeholder="add a description (markdown supported)…"
          rows={6}
          autoFocus
          disabled={isSaving}
          className="min-h-[140px] rounded-[10px] border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] font-mono text-[13px] leading-relaxed"
        />
        {error ? (
          <span className="text-xs text-destructive">{error}</span>
        ) : null}
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            onClick={handleSave}
            disabled={isSaving}
            className={PILL_BUTTON_CLASS}
          >
            {isSaving ? "Saving..." : "Save"}
          </Button>
          <Button
            type="button"
            variant="ghost"
            onClick={cancelEditing}
            disabled={isSaving}
            className={PILL_BUTTON_CLASS}
          >
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  // Empty state: offer an inline affordance to editors; render nothing for
  // viewers so the public page has no empty box.
  if (!description) {
    if (!canEdit) return null;
    return (
      <button
        type="button"
        onClick={startEditing}
        className={`${META_TEXT_CLASS} inline-flex items-center gap-1`}
      >
        <Pencil className="h-3 w-3" />
        add a description
      </button>
    );
  }

  // Has a description: render the markdown, truncated to ~3 lines of height with
  // a bottom fade + a "see more"/"see less" control inside the card when it
  // overflows that threshold.
  const collapsed = !isExpanded && isOverflowing;
  return (
    <div className="flex flex-col gap-2">
      <div className="group relative rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)]">
        {/* Only the markdown is height-clamped; the toggle below stays visible. */}
        <div
          ref={cardRef}
          className="relative"
          style={
            collapsed
              ? { maxHeight: COLLAPSED_MAX_HEIGHT_PX, overflow: "hidden" }
              : undefined
          }
        >
          <MarkdownRenderer content={description} />
          {collapsed ? (
            <div
              aria-hidden
              className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-[color:var(--paper-surface)] to-transparent"
            />
          ) : null}
        </div>
        {isOverflowing ? (
          <button
            type="button"
            onClick={() => setIsExpanded((value) => !value)}
            aria-expanded={isExpanded}
            className={`${META_TEXT_CLASS} mb-3 ml-4 mt-1`}
          >
            {collapsed ? "see more" : "see less"}
          </button>
        ) : null}
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
    </div>
  );
}
