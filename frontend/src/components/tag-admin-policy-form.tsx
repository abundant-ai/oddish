"use client";

import { useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { fetcher } from "@/lib/api";
import { cn } from "@/lib/utils";

interface TagPolicy {
  max_tags_per_entity: number;
  name_max_len: number;
  name_charset: string;
  reserved_prefixes: string[];
  who_can_create: string;
  profanity_mode: string;
  profanity_allowlist: string[];
  profanity_denylist: string[];
}

const WHO_CAN_CREATE = [
  { value: "ANY_MEMBER", label: "Any member" },
  { value: "ADMIN_ONLY", label: "Admins only" },
] as const;
// Backed by tag_profanity.py: OFF skips checks, ENFORCE blocks the write,
// any other value records a report without blocking.
// Values are the TagPolicyProfanityMode DB enum members — anything else is
// rejected by the column type.
const PROFANITY_MODES = [
  { value: "ENFORCE", label: "Block" },
  { value: "REPORT", label: "Report only" },
  { value: "OFF", label: "Off" },
] as const;

const splitList = (raw: string) =>
  raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

export function TagAdminPolicyForm() {
  const {
    data,
    mutate,
    error: loadError,
  } = useSWR<TagPolicy>("/api/tag-policy", fetcher);
  const [draft, setDraft] = useState<Partial<TagPolicy> | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (loadError) {
    const status = (loadError as { status?: number }).status;
    return (
      <p className="text-sm text-muted-foreground">
        {status === 403 ? "Admins only." : "Could not load tag policy."}
      </p>
    );
  }
  if (!data) {
    return <p className="text-sm text-muted-foreground">Loading policy…</p>;
  }

  const value: TagPolicy = { ...data, ...draft };
  const set = <K extends keyof TagPolicy>(key: K, v: TagPolicy[K]) => {
    setError(null);
    setDraft((d) => ({ ...d, [key]: v }));
  };

  async function save() {
    if (value.max_tags_per_entity < 1 || value.name_max_len < 1) {
      setError("Limits must be at least 1.");
      return;
    }
    setSaving(true);
    setError(null);
    const res = await fetch("/api/tag-policy", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(value),
    }).catch(() => null);
    setSaving(false);
    if (!res || !res.ok) {
      const body = (await res?.json().catch(() => null)) as {
        detail?: string;
        error?: string;
      } | null;
      setError(body?.detail ?? body?.error ?? "Could not save policy.");
      return;
    }
    setDraft(null);
    void mutate();
  }

  const toggleRow = (
    label: string,
    options: readonly { value: string; label: string }[],
    current: string,
    onPick: (v: string) => void,
  ) => (
    <div className="space-y-1">
      <Label>{label}</Label>
      <div className="flex gap-1">
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            className={cn(
              "rounded-md border px-2 py-0.5 text-xs",
              current === o.value
                ? "border-foreground/40 bg-accent"
                : "border-transparent text-muted-foreground hover:bg-accent/50",
            )}
            onClick={() => onPick(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <div className="max-w-xl space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1">
          <Label>Max tags per entity</Label>
          <Input
            type="number"
            min={1}
            value={value.max_tags_per_entity}
            onChange={(e) => set("max_tags_per_entity", Number(e.target.value))}
          />
        </div>
        <div className="space-y-1">
          <Label>Max name length</Label>
          <Input
            type="number"
            min={1}
            value={value.name_max_len}
            onChange={(e) => set("name_max_len", Number(e.target.value))}
          />
        </div>
      </div>
      {toggleRow(
        "Who can create tags",
        WHO_CAN_CREATE,
        value.who_can_create,
        (v) => set("who_can_create", v),
      )}
      {toggleRow(
        "Profanity handling",
        PROFANITY_MODES,
        value.profanity_mode,
        (v) => set("profanity_mode", v),
      )}
      <div className="space-y-1">
        <Label>Reserved prefixes (comma-separated)</Label>
        <Input
          value={value.reserved_prefixes.join(", ")}
          onChange={(e) => set("reserved_prefixes", splitList(e.target.value))}
        />
      </div>
      <div className="space-y-1">
        <Label>Profanity allowlist (comma-separated)</Label>
        <Input
          value={value.profanity_allowlist.join(", ")}
          onChange={(e) =>
            set("profanity_allowlist", splitList(e.target.value))
          }
        />
      </div>
      <div className="space-y-1">
        <Label>Profanity denylist (comma-separated)</Label>
        <Input
          value={value.profanity_denylist.join(", ")}
          onChange={(e) => set("profanity_denylist", splitList(e.target.value))}
        />
      </div>
      <p className="text-xs text-muted-foreground">
        Name charset: <code>{value.name_charset}</code> (fixed — changing it can
        strand existing tag names)
      </p>
      <div className="flex items-center gap-3">
        <Button size="sm" disabled={saving || draft == null} onClick={save}>
          {saving ? "Saving…" : "Save policy"}
        </Button>
        {error ? <p className="text-xs text-destructive">{error}</p> : null}
      </div>
    </div>
  );
}
