"use client";

import { useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { fetcher } from "@/lib/api";

interface SlackAlertSettings {
  experiment_milestone_usd: number;
  experiment_repeat_usd: number;
  trial_ping_usd: number;
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
  const [draft, setDraft] = useState<Partial<SlackAlertSettings> | null>(null);
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

  const value: SlackAlertSettings = { ...data, ...draft };
  const set = <K extends keyof SlackAlertSettings>(
    key: K,
    v: SlackAlertSettings[K],
  ) => {
    setError(null);
    setDraft((d) => ({ ...d, [key]: v }));
  };

  // The textarea keeps its own raw string so a half-typed address survives a
  // keystroke; the parsed list is only derived on save.
  const pings = pingsText ?? value.always_ping_emails.join(", ");

  const dirty = draft != null || pingsText != null;

  async function send(method: "PUT" | "DELETE") {
    setSaving(true);
    setError(null);
    const res = await fetch(ENDPOINT, {
      method,
      headers: { "Content-Type": "application/json" },
      body:
        method === "PUT"
          ? JSON.stringify({ ...value, always_ping_emails: splitList(pings) })
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
    setDraft(null);
    setPingsText(null);
    void mutate();
  }

  function save() {
    const amounts = [
      value.experiment_milestone_usd,
      value.experiment_repeat_usd,
      value.trial_ping_usd,
      value.trial_escalation_usd,
    ];
    if (amounts.some((n) => !Number.isFinite(n) || n <= 0)) {
      setError("Every amount must be greater than $0.");
      return;
    }
    if (value.trial_escalation_usd < value.trial_ping_usd) {
      setError("Escalation must be at or above the trial DM floor.");
      return;
    }
    void send("PUT");
  }

  const money = (key: keyof SlackAlertSettings, label: string, hint: string) => (
    <div className="space-y-1">
      <Label>{label}</Label>
      <Input
        type="number"
        min={1}
        step={50}
        value={value[key] as number}
        onChange={(e) => set(key, Number(e.target.value) as never)}
      />
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  );

  return (
    <div className="max-w-xl space-y-4">
      <div className="grid grid-cols-2 gap-4">
        {money(
          "experiment_milestone_usd",
          "First experiment milestone",
          "DMs the owner once an experiment's spend first clears this.",
        )}
        {money(
          "experiment_repeat_usd",
          "Then every",
          "And again on each further step of this size.",
        )}
        {money(
          "trial_ping_usd",
          "Trial DM floor",
          "Any single trial above this DMs its owner.",
        )}
        {money(
          "trial_escalation_usd",
          "Escalate to channel",
          "A trial above this also posts to the channel, pinging the list below.",
        )}
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
        {value.is_override
          ? "Overriding the deploy-time defaults. Applies to every org, within 5 minutes."
          : "Currently on the deploy-time defaults. Saving overrides them for every org."}
      </p>
      <div className="flex items-center gap-3">
        <Button size="sm" disabled={saving || !dirty} onClick={save}>
          {saving ? "Saving…" : "Save alert settings"}
        </Button>
        {value.is_override ? (
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
