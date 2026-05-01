"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { encodeExperimentRouteParam } from "@/lib/utils";
import type { ResolvedExperiment } from "@/lib/types";

export function NewExperimentClient() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    setSubmitting(true);
    try {
      const res = await fetch("/api/experiments", {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: name.trim(), cells: [] }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Create failed (${res.status})`);
      }
      const data = (await res.json()) as ResolvedExperiment;
      router.push(
        `/experiments/${encodeExperimentRouteParam(data.experiment_id)}`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-xl space-y-4 p-4">
      <div>
        <h1 className="font-mono text-[26px] font-semibold tracking-[-0.02em]">
          New experiment
        </h1>
        <p className="text-sm text-muted-foreground">
          Create an empty experiment. You'll add task-version × agent cells on
          the experiment page, then click Backfill to enqueue trials.
        </p>
      </div>
      <form onSubmit={onSubmit} className="space-y-3">
        <div>
          <Label htmlFor="name">Experiment name</Label>
          <Input
            id="name"
            placeholder="e.g. swe-bench-vs-claude-code-2026-05"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
        </div>
        {error ? (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-sm">
            {error}
          </div>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="ghost"
            onClick={() => router.push("/dashboard")}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button type="submit" disabled={submitting}>
            {submitting ? "Creating…" : "Create"}
          </Button>
        </div>
      </form>
    </div>
  );
}
