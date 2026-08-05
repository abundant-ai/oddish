"use client";

import { useState } from "react";
import useSWR from "swr";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetcher } from "@/lib/api";

type ModelDisplayName = {
  id: string;
  model_name: string;
  display_name: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
};

const ENDPOINT = "/api/admin/model-display-names";

export function ModelDisplayNamesCard() {
  const { data, error, isLoading, mutate } = useSWR<ModelDisplayName[]>(
    ENDPOINT,
    fetcher
  );

  const [modelName, setModelName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

  const status = (error as (Error & { status?: number }) | undefined)?.status;

  async function report(res: Response, fallback: string) {
    const body = await res.json().catch(() => ({}));
    setFormError(body?.detail || body?.error || fallback);
  }

  async function add() {
    if (!modelName.trim() || !displayName.trim()) return;
    setBusy(true);
    setFormError(null);
    try {
      const res = await fetch(ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model_name: modelName.trim(),
          display_name: displayName.trim(),
        }),
      });
      if (!res.ok) {
        await report(res, "Failed to save display name.");
        return;
      }
      setModelName("");
      setDisplayName("");
      await mutate();
    } finally {
      setBusy(false);
    }
  }

  async function save(id: string) {
    if (!editValue.trim()) return;
    setBusy(true);
    setFormError(null);
    try {
      const res = await fetch(`${ENDPOINT}/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: editValue.trim() }),
      });
      if (!res.ok) {
        await report(res, "Failed to update display name.");
        return;
      }
      setEditingId(null);
      await mutate();
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    setFormError(null);
    const res = await fetch(`${ENDPOINT}/${id}`, { method: "DELETE" });
    if (!res.ok) await report(res, "Failed to remove display name.");
    await mutate();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Model display names</CardTitle>
        <p className="text-muted-foreground text-sm">
          Rename a model for public consumption. Published experiments — the
          pages behind an <code>oddish publish</code> share link — show the
          display name in place of the real model id. This dashboard, cost
          accounting, and queue routing are unaffected and keep using the real
          id. Enter the model exactly as it appears on a trial.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-start gap-2">
          <Input
            placeholder="Model id (e.g. spiffy-balloon)"
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            className="w-72"
          />
          <Input
            placeholder="Shown publicly as (e.g. bananas)"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className="w-64"
          />
          <Button
            onClick={add}
            disabled={busy || !modelName.trim() || !displayName.trim()}
          >
            Save
          </Button>
        </div>
        {formError && <p className="text-destructive text-sm">{formError}</p>}

        {isLoading && <p className="text-muted-foreground text-sm">Loading…</p>}
        {error && (
          <p className="text-destructive text-sm">
            {status === 403
              ? "You do not have access to model display names."
              : "Failed to load display names."}
          </p>
        )}
        {data && data.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No models are renamed. Published pages show the real model id.
          </p>
        )}
        {data && data.length > 0 && (
          <div className="space-y-2">
            {data.map((row) => (
              <div
                key={row.id}
                className="flex items-center justify-between gap-3 rounded-md border px-3 py-2"
              >
                <div className="flex min-w-0 flex-1 items-center gap-3">
                  <code className="truncate text-sm">{row.model_name}</code>
                  <span className="text-muted-foreground text-sm">→</span>
                  {editingId === row.id ? (
                    <Input
                      value={editValue}
                      onChange={(e) => setEditValue(e.target.value)}
                      className="w-56"
                      autoFocus
                    />
                  ) : (
                    <code className="truncate text-sm">{row.display_name}</code>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  {editingId === row.id ? (
                    <>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => save(row.id)}
                        disabled={busy || !editValue.trim()}
                      >
                        Save
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setEditingId(null)}
                      >
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setEditingId(row.id);
                        setEditValue(row.display_name);
                      }}
                    >
                      Edit
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => remove(row.id)}
                  >
                    Remove
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
