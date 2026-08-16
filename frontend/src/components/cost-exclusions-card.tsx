"use client";

import { useState } from "react";
import useSWR from "swr";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetcher } from "@/lib/api";

type ExcludedModel = {
  id: string;
  model_name: string;
  label: string;
  created_by: string | null;
  created_at: string;
};

type ExcludedExperiment = {
  id: string;
  experiment_id: string;
  experiment_name: string;
  label: string;
  created_by: string | null;
  created_at: string;
};

type ExcludedRow = ExcludedModel | ExcludedExperiment;

function backendError(body: unknown, fallback: string): string {
  const { detail, error } = (body ?? {}) as {
    detail?: unknown;
    error?: unknown;
  };
  if (typeof detail === "string" && detail) return detail;
  if (typeof error === "string" && error) return error;
  return fallback;
}

function ExclusionList<T extends ExcludedRow>({
  endpoint,
  field,
  placeholder,
  addLabel,
  emptyLabel,
  primary,
  secondary,
}: {
  endpoint: string;
  field: string;
  placeholder: string;
  addLabel: string;
  emptyLabel: string;
  primary: (row: T) => string;
  secondary: (row: T) => string | null;
}) {
  const { data, error, isLoading, mutate } = useSWR<T[]>(endpoint, fetcher);

  const [reference, setReference] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function add() {
    if (!reference.trim()) return;
    setBusy(true);
    setFormError(null);
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          [field]: reference.trim(),
          label: label.trim(),
        }),
      });
      if (!res.ok) {
        setFormError(
          backendError(await res.json().catch(() => ({})), "Failed to add.")
        );
        return;
      }
      setReference("");
      setLabel("");
      await mutate();
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    setFormError(null);
    const res = await fetch(`${endpoint}/${id}`, { method: "DELETE" });
    if (!res.ok) {
      setFormError(
        backendError(await res.json().catch(() => ({})), "Failed to remove.")
      );
    }
    await mutate();
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start gap-2">
        <Input
          placeholder={placeholder}
          value={reference}
          onChange={(e) => setReference(e.target.value)}
          className="w-72"
        />
        <Input
          placeholder="Label (optional)"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          className="w-48"
        />
        <Button onClick={add} disabled={busy || !reference.trim()}>
          {addLabel}
        </Button>
      </div>
      {formError && <p className="text-destructive text-sm">{formError}</p>}

      {isLoading && <p className="text-muted-foreground text-sm">Loading…</p>}
      {error && <p className="text-destructive text-sm">Failed to load.</p>}
      {data && data.length === 0 && (
        <p className="text-muted-foreground text-sm">{emptyLabel}</p>
      )}
      {data && data.length > 0 && (
        <div className="space-y-2">
          {data.map((row) => (
            <div
              key={row.id}
              className="flex items-center justify-between rounded-md border px-3 py-2"
            >
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium">{primary(row)}</span>
                {secondary(row) && (
                  <code className="text-muted-foreground text-xs">
                    {secondary(row)}
                  </code>
                )}
                {row.label && (
                  <span className="text-muted-foreground text-sm">
                    {row.label}
                  </span>
                )}
              </div>
              <Button variant="ghost" size="sm" onClick={() => remove(row.id)}>
                Remove
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function CostExclusionsCard() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Spend that doesn&apos;t count
        </CardTitle>
        <p className="text-muted-foreground text-sm">
          Spend on these models and experiments is ignored by cost accounting:
          the admin cost dashboards above and quota enforcement. Experiment,
          task, and trial pages still show the money, marked as not real. Both
          lists are deployment-wide and apply retroactively — adding an entry
          removes spend already recorded, and removing one restores it.
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        <section className="space-y-2">
          <h3 className="text-sm font-medium">Models</h3>
          <p className="text-muted-foreground text-sm">
            Every trial that ever ran on the model stops counting — for
            sponsored capacity, free preview tiers, and vendor credits.
          </p>
          <ExclusionList<ExcludedModel>
            endpoint="/api/admin/cost-excluded-models"
            field="model_name"
            placeholder="Model id, e.g. xai/grok-4"
            addLabel="Exclude model"
            emptyLabel="No models are excluded from cost."
            primary={(row) => row.model_name}
            secondary={() => null}
          />
        </section>

        <section className="space-y-2">
          <h3 className="text-sm font-medium">Experiments</h3>
          <p className="text-muted-foreground text-sm">
            Only trials the experiment ran itself stop counting. Trials it
            merely gathered from elsewhere keep counting on the experiment that
            actually spent the money. Add one by name or id.
          </p>
          <ExclusionList<ExcludedExperiment>
            endpoint="/api/admin/cost-excluded-experiments"
            field="experiment"
            placeholder="Experiment name or id"
            addLabel="Exclude experiment"
            emptyLabel="No experiments are excluded from cost."
            primary={(row) => row.experiment_name || row.experiment_id}
            secondary={(row) => row.experiment_id}
          />
        </section>
      </CardContent>
    </Card>
  );
}
