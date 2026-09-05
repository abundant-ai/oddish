"use client";

import { useState } from "react";
import { QA_ISSUE_LABELS } from "@/lib/deliveries";
import type { QAIssueCategory, QAWorkMetadata } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";

export function DeliveryQAWorkEditor({
  taskName,
  work,
  onClose,
  onSave,
}: {
  taskName: string;
  work: QAWorkMetadata;
  onClose: () => void;
  onSave: (patch: {
    issue_categories: QAIssueCategory[];
    note: string;
  }) => Promise<void>;
}) {
  const [categories, setCategories] = useState(work.issue_categories);
  const [note, setNote] = useState(work.note);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !saving) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>QA work · {taskName}</DialogTitle>
        </DialogHeader>
        <p className="text-muted-foreground text-sm">
          Choose issue categories. The first selected category is used for
          grouping.
        </p>
        {(Object.entries(QA_ISSUE_LABELS) as [QAIssueCategory, string][]).map(
          ([key, label]) => (
            <label key={key} className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={categories.includes(key)}
                onCheckedChange={(checked) =>
                  setCategories(
                    checked
                      ? [...categories, key]
                      : categories.filter((value) => value !== key)
                  )
                }
              />
              {label}
              {categories[0] === key && (
                <span className="text-muted-foreground text-xs">Primary</span>
              )}
            </label>
          )
        )}
        <label className="space-y-2 text-sm">
          Handoff note
          <Textarea
            value={note}
            maxLength={4000}
            onChange={(event) => setNote(event.target.value)}
            rows={4}
          />
        </label>
        {error && (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button
            disabled={saving}
            onClick={async () => {
              setSaving(true);
              setError(null);
              try {
                await onSave({ issue_categories: categories, note });
                onClose();
              } catch (err) {
                setError(
                  err instanceof Error ? err.message : "Failed to save QA work"
                );
              } finally {
                setSaving(false);
              }
            }}
          >
            {saving ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
