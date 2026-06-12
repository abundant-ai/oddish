"use client";

import { useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { fetcher } from "@/lib/api";

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

export function TagAdminPolicyForm() {
  const { data, mutate } = useSWR<TagPolicy>("/api/tag-policy", fetcher);
  const [draft, setDraft] = useState<TagPolicy | null>(null);
  const value = draft ?? data ?? null;

  if (!value) return <div>Loading policy…</div>;

  async function save() {
    if (!value) return;
    await fetch("/api/tag-policy", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(value),
    });
    mutate();
    setDraft(null);
  }

  return (
    <div className="space-y-3">
      <div>
        <Label>Max tags per entity</Label>
        <Input
          type="number"
          value={value.max_tags_per_entity}
          onChange={(e) => setDraft({ ...value, max_tags_per_entity: Number(e.target.value) })}
        />
      </div>
      <div>
        <Label>Profanity mode</Label>
        <Input
          value={value.profanity_mode}
          onChange={(e) => setDraft({ ...value, profanity_mode: e.target.value })}
        />
      </div>
      <Button onClick={save}>Save policy</Button>
    </div>
  );
}
