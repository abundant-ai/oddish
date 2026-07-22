"use client";

import { useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { fetcher } from "@/lib/api";

interface SlackAlertSettings {
  trial_escalation_usd: number;
  always_ping_emails: string[];
  is_override: boolean;
}

const ENDPOINT = "/api/admin/slack-alert-settings";

const splitList = (raw: string) =>
  raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

export function SlackAlertSettingsForm() {
  const {
    data,
    mutate,
    error: loadError,
  } = useSWR<SlackAlertSettings>(ENDPOINT, fetcher);
  const [escalation, setEscalation] = useState<number | null>(null);
  const [pingsText, setPingsText] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (loadError) {
    const status = (loadError as { status?: number }).status;
    return (
      <p className="text-sm text-muted-foreground">
        {status === 403 ? "Admins only." : "Could not load alert settings."}
      </p>
    );
  }
  if (!data) {
    return <p className="text-sm text-muted-foreground">Loading settings…</p>;
  }

  const escalationUsd = escalation ?? data.trial_escalation_usd;
  // The field keeps its own raw string so a half-typed address survives a
  // keystroke; the parsed list is only derived on save.
  const pings = pingsText ?? data.always_ping_emails.join(", ");
  const dirty = escalation != null || pingsText != null;

  async function send(method: "PUT" | "DELETE") {
    setSaving(true);
    setError(null);
    const res = await fetch(ENDPOINT, {
      method,
      headers: { "Content-Type": "application/json" },
      body:
        method === "PUT"
          ? JSON.stringify({
              trial_escalation_usd: escalationUsd,
              always_ping_emails: splitList(pings),
            })
          : undefined,
    }).catch(() => null);
    setSaving(false);
    if (!res || !res.ok) {
      const body = (await res?.json().catch(() => null)) as {
        detail?: string | { msg?: string }[];
        error?: string;
      } | null;
      const detail = Array.isArray(body?.detail)
        ? body.detail[0]?.msg
        : body?.detail;
      setError(detail ?? body?.error ?? "Could not save alert settings.");
      return;
    }
    setEscalation(null);
    setPingsText(null);
    void mutate();
  }

  function save() {
    if (!Number.isFinite(escalationUsd) || escalationUsd <= 0) {
      setError("The escalation amount must be greater than $0.");
      return;
    }
    void send("PUT");
  }

  return (
    <div className="max-w-xl space-y-4">
      <div className="space-y-1">
        <Label>Escalate to channel above</Label>
        <Input
          type="number"
          min={1}
          step={50}
          value={escalationUsd}
          onChange={(e) => {
            setError(null);
            setEscalation(Number(e.target.value));
          }}
        />
        <p className="text-xs text-muted-foreground">
          Any single trial finished within the past 24 hours that costs more than
          this posts to the shared channel and pings the list below. Owner DMs are
          tuned per person in their own notification settings, not here.
        </p>
      </div>
      <div className="space-y-1">
        <Label>Always ping on escalation (comma-separated emails)</Label>
        <Input
          value={pings}
          onChange={(e) => {
            setError(null);
            setPingsText(e.target.value);
          }}
        />
        <p className="text-xs text-muted-foreground">
          Matched to Slack accounts by email, so these must be the addresses on
          their Slack profiles.
        </p>
      </div>
      <p className="text-xs text-muted-foreground">
        {data.is_override
          ? "Overriding the deploy-time defaults. Applies to every org, within 5 minutes."
          : "Currently on the deploy-time defaults. Saving overrides them for every org."}
      </p>
      <div className="flex items-center gap-3">
        <Button size="sm" disabled={saving || !dirty} onClick={save}>
          {saving ? "Saving…" : "Save alert settings"}
        </Button>
        {data.is_override ? (
          <Button
            size="sm"
            variant="outline"
            disabled={saving}
            onClick={() => void send("DELETE")}
          >
            Reset to defaults
          </Button>
        ) : null}
        {error ? <p className="text-xs text-destructive">{error}</p> : null}
      </div>
    </div>
  );
}
