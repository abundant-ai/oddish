"use client";

import { useState } from "react";
import useSWR from "swr";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetcher } from "@/lib/api";

type ExcludedKey = {
  id: string;
  key_hint: string;
  label: string;
  created_by: string | null;
  created_at: string;
};

export function CostExcludedKeysCard() {
  const { data, error, isLoading, mutate } = useSWR<ExcludedKey[]>(
    "/api/admin/cost-excluded-keys",
    fetcher
  );

  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function add() {
    if (!key.trim()) return;
    setBusy(true);
    setFormError(null);
    try {
      const res = await fetch("/api/admin/cost-excluded-keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: key.trim(), label: label.trim() }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setFormError(
          res.status === 409
            ? "That key is already excluded."
            : body?.details || body?.error || "Failed to add key."
        );
        return;
      }
      setKey("");
      setLabel("");
      await mutate();
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    setFormError(null);
    const res = await fetch(`/api/admin/cost-excluded-keys/${id}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      setFormError(
        body?.details || body?.error || "Failed to remove key."
      );
    }
    await mutate();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Cost-excluded LLM keys</CardTitle>
        <p className="text-muted-foreground text-sm">
          Spend on these LLM provider API keys (e.g. sponsored or free keys) is
          ignored by cost accounting: quota enforcement and the admin cost
          dashboards. Experiment pages still show the trials&apos; raw compute
          cost. Only a one-way hash is stored — the key itself is never saved.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-start gap-2">
          <Input
            type="password"
            placeholder="Paste an LLM API key"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            className="w-72"
          />
          <Input
            placeholder="Label (optional)"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            className="w-48"
          />
          <Button onClick={add} disabled={busy || !key.trim()}>
            Add
          </Button>
        </div>
        {formError && <p className="text-destructive text-sm">{formError}</p>}

        {isLoading && <p className="text-muted-foreground text-sm">Loading…</p>}
        {error && (
          <p className="text-destructive text-sm">Failed to load keys.</p>
        )}
        {data && data.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No keys are excluded from cost.
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
                  <code className="text-sm">••••{row.key_hint}</code>
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
