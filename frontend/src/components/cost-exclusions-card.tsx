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

type ExcludedKey = {
  id: string;
  key_hint: string;
  label: string;
  created_by: string | null;
  created_at: string;
};

type ExcludedRow = ExcludedModel | ExcludedExperiment | ExcludedKey;

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
  inputType = "text",
}: {
  endpoint: string;
  field: string;
  placeholder: string;
  addLabel: string;
  emptyLabel: string;
  primary: (row: T) => string;
  secondary: (row: T) => string | null;
  inputType?: "text" | "password";
}) {
  const { data, error, isLoading, mutate } = useSWR<T[]>(endpoint, fetcher);

  const [reference, setReference] = useState("");
  const [label, setLabel] = useState("");
  const [mutation, setMutation] = useState<
    { kind: "add" } | { kind: "remove"; id: string } | null
  >(null);
  const [formError, setFormError] = useState<string | null>(null);

  async function add() {
    if (!reference.trim() || mutation) return;
    setMutation({ kind: "add" });
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
      try {
        await mutate();
      } catch {
        setFormError("Added, but failed to refresh the list.");
      }
    } catch {
      setFormError("Failed to add.");
    } finally {
      setMutation(null);
    }
  }

  async function remove(id: string) {
    if (mutation) return;
    setMutation({ kind: "remove", id });
    setFormError(null);
    try {
      const res = await fetch(`${endpoint}/${id}`, { method: "DELETE" });
      if (!res.ok) {
        setFormError(
          backendError(await res.json().catch(() => ({})), "Failed to remove.")
        );
        return;
      }
      try {
        await mutate();
      } catch {
        setFormError("Removed, but failed to refresh the list.");
      }
    } catch {
      setFormError("Failed to remove.");
    } finally {
      setMutation(null);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start gap-2">
        <Input
          type={inputType}
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
        <Button onClick={add} disabled={mutation !== null || !reference.trim()}>
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
              <Button
                variant="ghost"
                size="sm"
                onClick={() => remove(row.id)}
                disabled={mutation !== null}
              >
                {mutation?.kind === "remove" && mutation.id === row.id
                  ? "Removing…"
                  : "Remove"}
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
        <CardTitle className="text-base">Remove Spend Tracking</CardTitle>
        <p className="text-muted-foreground text-sm">
          Spend listed here is hidden from the cost dashboards and does not
          count against quotas.
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        <section className="space-y-2">
          <h3 className="text-sm font-medium">Models</h3>
          <p className="text-muted-foreground text-sm">
            Every trial that used this model stops counting.
          </p>
          <ExclusionList<ExcludedModel>
            endpoint="/api/admin/cost-excluded-models"
            field="model_name"
            placeholder="Model id, e.g. xai/grok-4"
            addLabel="Exclude model"
            emptyLabel="No models excluded."
            primary={(row) => row.model_name}
            secondary={() => null}
          />
        </section>

        <section className="space-y-2">
          <h3 className="text-sm font-medium">Experiments</h3>
          <p className="text-muted-foreground text-sm">
            Trials this experiment ran itself stop counting.
          </p>
          <ExclusionList<ExcludedExperiment>
            endpoint="/api/admin/cost-excluded-experiments"
            field="experiment"
            placeholder="Experiment name or id"
            addLabel="Exclude experiment"
            emptyLabel="No experiments excluded."
            primary={(row) => row.experiment_name || row.experiment_id}
            secondary={(row) => row.experiment_id}
          />
        </section>

        <section className="space-y-2">
          <h3 className="text-sm font-medium">Provider keys</h3>
          <p className="text-muted-foreground text-sm">
            Trials funded by this provider key stop counting. Only a one-way
            hash and the last four characters are stored.
          </p>
          <ExclusionList<ExcludedKey>
            endpoint="/api/admin/cost-excluded-keys"
            field="key"
            placeholder="Provider API key"
            inputType="password"
            addLabel="Exclude key"
            emptyLabel="No provider keys excluded."
            primary={(row) => `••••${row.key_hint}`}
            secondary={() => null}
          />
        </section>
      </CardContent>
    </Card>
  );
}
