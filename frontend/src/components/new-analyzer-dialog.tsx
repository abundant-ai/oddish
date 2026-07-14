"use client";

import { useState } from "react";
import useSWR from "swr";
import { Loader2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { fetcher } from "@/lib/api";
import type { ExperimentOption } from "@/lib/types";

// The BFF forwards the backend/FastAPI body verbatim, so real errors arrive as
// `error` (our routes) or `detail` (FastAPI HTTPException / 422 validation).
// Reading only `error` collapsed messages like "unknown experiment id" to a
// generic status text.
function messageFromError(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const d = data as { error?: unknown; detail?: unknown };
  if (typeof d.error === "string" && d.error) return d.error;
  if (typeof d.detail === "string" && d.detail) return d.detail;
  if (Array.isArray(d.detail)) {
    const msgs = d.detail
      .map((item) =>
        item && typeof item === "object" && "msg" in item
          ? String((item as { msg: unknown }).msg)
          : typeof item === "string"
            ? item
            : undefined,
      )
      .filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  }
  return undefined;
}

export function NewAnalyzerDialog({ onCreated }: { onCreated?: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: options } = useSWR<ExperimentOption[]>(
    open ? "/api/analyzers/experiment-options" : null,
    fetcher,
  );

  const canSubmit = name.trim().length > 0 && selected.length > 0 && !submitting;

  function toggle(id: string) {
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }

  function reset() {
    setName("");
    setSelected([]);
    setError(null);
    setSubmitting(false);
  }

  async function handleSubmit() {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch("/api/analyzers", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), experiment_ids: selected }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(messageFromError(data) || res.statusText || "Create failed");
      }
      setOpen(false);
      reset();
      onCreated?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Create failed");
    } finally {
      setSubmitting(false);
    }
  }

  function handleOpenChange(o: boolean) {
    setOpen(o);
    if (!o) reset();
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button type="button" size="sm" className="h-8 gap-1 px-3 text-[12px]">
          <Plus className="h-3.5 w-3.5" /> New Analyzer
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>New analyzer</DialogTitle>
          <DialogDescription>
            Analyze trajectories across one or more experiments.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <Input
            placeholder="Analyzer name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <div className="text-muted-foreground text-xs">Experiments</div>
          <div className="max-h-56 space-y-1 overflow-y-auto rounded border p-2">
            {(options ?? []).map((o) => (
              <button
                key={o.id}
                type="button"
                onClick={() => toggle(o.id)}
                className={`flex w-full items-center justify-between rounded px-2 py-1 text-left text-sm hover:bg-muted ${
                  selected.includes(o.id) ? "bg-muted" : ""
                }`}
              >
                <span>{o.name}</span>
                {selected.includes(o.id) && (
                  <Badge variant="outline" className="text-[10px]">
                    selected
                  </Badge>
                )}
              </button>
            ))}
            {options && options.length === 0 && (
              <div className="text-muted-foreground px-2 py-1 text-xs">
                No experiments found.
              </div>
            )}
          </div>
          {error && <div className="text-xs text-red-500">{error}</div>}
        </div>

        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button size="sm" disabled={!canSubmit} onClick={handleSubmit}>
            {submitting && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
