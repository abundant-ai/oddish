"use client";

import { useState } from "react";
import useSWR from "swr";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetcher } from "@/lib/api";

type ExcludedExperiment = {
  id: string;
  experiment_id: string;
  experiment_name: string;
  label: string;
  created_by: string | null;
  created_at: string;
};

// The proxy wraps backend failures as { error, details } where details is the
// raw FastAPI response text; unwrap its "detail" message for display.
function backendDetail(body: unknown): string | null {
  const details = (body as { details?: string } | null)?.details;
  if (!details) return null;
  try {
    const parsed = JSON.parse(details);
    if (typeof parsed?.detail === "string") return parsed.detail;
  } catch {
    // not JSON — fall through to the raw text
  }
  return details;
}

export function CostExcludedExperimentsCard() {
  const { data, error, isLoading, mutate } = useSWR<ExcludedExperiment[]>(
    "/api/admin/cost-excluded-experiments",
    fetcher
  );

  const [experiment, setExperiment] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function add() {
    if (!experiment.trim()) return;
    setBusy(true);
    setFormError(null);
    try {
      const res = await fetch("/api/admin/cost-excluded-experiments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          experiment: experiment.trim(),
          label: label.trim(),
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setFormError(
          backendDetail(body) || body?.error || "Failed to add experiment."
        );
        return;
      }
      setExperiment("");
      setLabel("");
      await mutate();
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    setFormError(null);
    const res = await fetch(`/api/admin/cost-excluded-experiments/${id}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      setFormError(
        backendDetail(body) || body?.error || "Failed to remove experiment."
      );
    }
    await mutate();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Cost-excluded experiments</CardTitle>
        <p className="text-muted-foreground text-sm">
          Trials homed in these experiments are ignored by cost accounting:
          quota enforcement and the admin cost dashboards. Experiment pages
          still show the trials&apos; raw compute cost. Add an experiment by
          name or id.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-start gap-2">
          <Input
            placeholder="Experiment name or id"
            value={experiment}
            onChange={(e) => setExperiment(e.target.value)}
            className="w-72"
          />
          <Input
            placeholder="Label (optional)"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            className="w-48"
          />
          <Button onClick={add} disabled={busy || !experiment.trim()}>
            Add
          </Button>
        </div>
        {formError && <p className="text-destructive text-sm">{formError}</p>}

        {isLoading && <p className="text-muted-foreground text-sm">Loading…</p>}
        {error && (
          <p className="text-destructive text-sm">
            Failed to load experiments.
          </p>
        )}
        {data && data.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No experiments are excluded from cost.
          </p>
        )}
        {data && data.length > 0 && (
          <div className="space-y-2">
            {data.map((row) => (
              <div
                key={row.id}
                className="flex items-center justify-between rounded-md border px-3 py-2"
              >
                <div className="flex items-center gap-3">
                  <span className="text-sm font-medium">
                    {row.experiment_name || row.experiment_id}
                  </span>
                  <code className="text-muted-foreground text-xs">
                    {row.experiment_id}
                  </code>
                  {row.label && (
                    <span className="text-muted-foreground text-sm">
                      {row.label}
                    </span>
                  )}
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => remove(row.id)}
                >
                  Remove
                </Button>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
