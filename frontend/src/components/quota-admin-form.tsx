"use client";

import { useState } from "react";
import useSWR from "swr";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetcher } from "@/lib/api";
import type { QuotaList, QuotaMember, QuotaUpdate } from "@/lib/types";

const formatDollars = (value: number) =>
  value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

const memberLabel = (member: QuotaMember) =>
  member.name || member.email || member.github_username || member.user_id;

const MAX_LIMIT_USD = 99999999.9999;

// Collapse backend error shapes (string detail, FastAPI detail array, error) into one string.
function errorMessage(body: unknown): string | undefined {
  if (!body || typeof body !== "object") return undefined;
  const b = body as { detail?: unknown; error?: unknown };
  if (typeof b.detail === "string") return b.detail;
  if (Array.isArray(b.detail)) {
    const parts = b.detail
      .map((d) =>
        d && typeof d === "object" && "msg" in d
          ? String((d as { msg: unknown }).msg)
          : String(d)
      )
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  if (typeof b.error === "string") return b.error;
  return undefined;
}

export function QuotaAdminForm() {
  const { data, mutate, error } = useSWR<QuotaList>("/api/quotas", fetcher);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [savingId, setSavingId] = useState<string | null>(null);
  const [rowError, setRowError] = useState<Record<string, string>>({});

  if (error) {
    const status = (error as { status?: number }).status;
    return (
      <p className="text-muted-foreground text-sm">
        {status === 403 ? "Admins only." : "Could not load quotas."}
      </p>
    );
  }
  if (!data) {
    return <p className="text-muted-foreground text-sm">Loading quotas…</p>;
  }

  const members = [...data.members].sort((a, b) =>
    memberLabel(a).localeCompare(memberLabel(b))
  );

  if (members.length === 0) {
    return <p className="text-muted-foreground text-sm">No members to show.</p>;
  }

  const draftValue = (member: QuotaMember) =>
    drafts[member.user_id] ?? member.limit_usd.toFixed(2);
  const isDirty = (member: QuotaMember) => {
    const draft = drafts[member.user_id];
    return (
      draft !== undefined &&
      (draft === "" ? true : Number(draft) !== member.limit_usd)
    );
  };

  const setDraft = (userId: string, value: string) => {
    setRowError(({ [userId]: _drop, ...rest }) => rest);
    setDrafts((d) => ({ ...d, [userId]: value }));
  };

  async function save(member: QuotaMember) {
    const raw = draftValue(member).trim();
    const id = member.user_id;
    let payload: QuotaUpdate;

    if (raw === "") {
      payload = { limit_usd: null };
    } else {
      // Validate the 2dp value actually sent, not the raw parse (e.g. 0.001
      // parses > 0 but rounds to "0.00", which the backend rejects).
      const rounded = Number(Number(raw).toFixed(2));
      if (!Number.isFinite(rounded) || rounded <= 0 || rounded > MAX_LIMIT_USD) {
        setRowError((e) => ({
          ...e,
          [id]: "Enter an amount greater than 0, or leave empty to reset.",
        }));
        return;
      }
      payload = { limit_usd: rounded.toFixed(2) };
    }

    setSavingId(id);
    setRowError(({ [id]: _drop, ...rest }) => rest);
    const res = await fetch(`/api/quotas/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(() => null);
    setSavingId(null);

    if (!res || !res.ok) {
      const body: unknown = await res?.json().catch(() => null);
      const message =
        res?.status === 403
          ? "Admins only."
          : res?.status === 404
            ? "Member not found."
            : (errorMessage(body) ?? "Could not save limit.");
      setRowError((e) => ({ ...e, [id]: message }));
      return;
    }

    setDrafts(({ [id]: _drop, ...rest }) => rest);
    void mutate();
  }

  return (
    <div className="space-y-3">
      <div className="border-border overflow-hidden rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow className="bg-muted/40 hover:bg-muted/40">
              <TableHead className="h-9 text-xs">Member</TableHead>
              <TableHead className="h-9 text-xs">Role</TableHead>
              <TableHead className="h-9 text-right text-xs">
                Used today
              </TableHead>
              <TableHead className="h-9 text-right text-xs">
                Daily limit
              </TableHead>
              <TableHead className="h-9 text-xs"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.map((member) => {
              const id = member.user_id;
              const over = member.limit_usd > 0 && member.used_usd >= member.limit_usd;
              const saving = savingId === id;
              const err = rowError[id];
              return (
                <TableRow key={id} className="border-border/70">
                  <TableCell className="py-2.5">
                    <div className="min-w-0">
                      <p className="text-foreground truncate text-sm font-medium">
                        {member.name || member.email}
                      </p>
                      <p className="text-muted-foreground truncate text-xs">
                        {member.email}
                      </p>
                    </div>
                  </TableCell>
                  <TableCell className="py-2.5">
                    <Badge variant="outline" className="text-[11px] capitalize">
                      {member.role.replace(/^org:/, "")}
                    </Badge>
                  </TableCell>
                  <TableCell className="py-2.5 text-right font-mono text-xs">
                    <span className={over ? "text-destructive" : undefined}>
                      {formatDollars(member.used_usd)}
                    </span>
                  </TableCell>
                  <TableCell className="py-2.5 text-right">
                    <div className="flex flex-col items-end gap-1">
                      <Input
                        type="number"
                        min={0}
                        step="0.01"
                        inputMode="decimal"
                        className="h-8 w-28 text-right font-mono text-xs"
                        value={draftValue(member)}
                        onChange={(e) => setDraft(id, e.target.value)}
                      />
                      {err ? (
                        <p className="text-destructive text-[11px]">{err}</p>
                      ) : null}
                    </div>
                  </TableCell>
                  <TableCell className="py-2.5">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={saving || !isDirty(member)}
                      onClick={() => save(member)}
                    >
                      {saving ? "Saving…" : "Save"}
                    </Button>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
      <p className="text-muted-foreground text-xs">
        Effective daily limit per member. Leave a limit empty and save to clear
        the override — that member reverts to the workspace default.
      </p>
    </div>
  );
}
