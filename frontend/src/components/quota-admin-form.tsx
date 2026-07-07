"use client";

import { useState } from "react";
import Link from "next/link";
import useSWR from "swr";

import { Loader2 } from "lucide-react";

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

function buildPayload(raw: string): QuotaUpdate | string {
  if (raw.trim() === "") return { limit_usd: null };
  const rounded = Number(Number(raw).toFixed(2));
  if (!Number.isFinite(rounded) || rounded <= 0 || rounded > MAX_LIMIT_USD) {
    return "Enter an amount greater than 0, or leave empty to reset.";
  }
  return { limit_usd: rounded.toFixed(2) };
}

export function QuotaAdminForm() {
  const { data, mutate, error } = useSWR<QuotaList>("/api/quotas", fetcher);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [rowError, setRowError] = useState<Record<string, string>>({});
  const [orgDraft, setOrgDraft] = useState<string | null>(null);
  const [orgSaving, setOrgSaving] = useState(false);
  const [orgError, setOrgError] = useState<string | undefined>(undefined);

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

  // Org fields are absent in a deploy-before-migrate window; hide the whole
  // section until the backend reports them.
  const hasOrgFields = data.org_used_usd !== undefined;
  const orgLimit = data.org_limit_usd ?? null;
  const orgUsed = data.org_used_usd ?? 0;
  const orgReserved = data.org_reserved_usd ?? 0;
  const orgDefault = data.org_default_limit_usd ?? null;
  const orgOver = orgLimit !== null && orgUsed + orgReserved >= orgLimit;
  const orgFieldValue =
    orgDraft ?? (orgLimit !== null ? orgLimit.toFixed(2) : "");
  const orgDirty =
    orgDraft !== null &&
    (orgDraft.trim() === ""
      ? orgLimit !== null
      : Number(orgDraft) !== orgLimit);

  const setOrgDraftValue = (value: string) => {
    setOrgError(undefined);
    setOrgDraft(value);
  };
  const revertOrgDraft = () => {
    setOrgDraft(null);
    setOrgError(undefined);
  };

  async function saveOrg() {
    if (orgSaving || !orgDirty) return;
    const result = buildPayload(orgDraft ?? "");
    if (typeof result === "string") {
      setOrgError(result);
      return;
    }
    setOrgError(undefined);
    setOrgSaving(true);
    const res = await fetch("/api/quotas/org", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result),
    }).catch(() => null);
    setOrgSaving(false);

    if (!res?.ok) {
      const body: unknown = await res?.json().catch(() => null);
      setOrgError(
        res?.status === 403
          ? "Admins only."
          : (errorMessage(body) ?? "Could not save org budget.")
      );
      return;
    }
    setOrgDraft(null);
    void mutate();
  }

  const orgSection = hasOrgFields ? (
    <div className="border-border bg-muted/30 space-y-3 rounded-lg border p-4">
      <div>
        <p className="text-foreground text-sm font-medium">
          Monthly org budget
        </p>
        <p className="text-muted-foreground text-xs">
          Aggregate cap across all members this calendar month (unattributed
          spend counts too). Resets on the 1st (UTC).
        </p>
      </div>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-1">
          <p className="text-foreground text-sm">
            <span
              className={
                orgOver ? "text-destructive font-semibold" : "font-semibold"
              }
            >
              {formatDollars(orgUsed)}
            </span>{" "}
            of{" "}
            <span className="font-semibold">
              {orgLimit !== null ? formatDollars(orgLimit) : "No org cap"}
            </span>{" "}
            used this month
            {orgReserved > 0 ? (
              <span className="text-muted-foreground">
                {" "}
                ({formatDollars(orgReserved)} reserved)
              </span>
            ) : null}
          </p>
          {orgDefault !== null ? (
            <p className="text-muted-foreground text-xs">
              Deploy default: {formatDollars(orgDefault)}
            </p>
          ) : null}
        </div>
        <div className="flex items-end gap-2">
          <div className="flex flex-col items-end gap-1">
            <Input
              type="number"
              min={0}
              step="0.01"
              inputMode="decimal"
              placeholder="No cap"
              aria-label="Monthly org budget"
              aria-invalid={orgError ? true : undefined}
              className={`h-8 w-28 text-right font-mono text-xs ${
                orgError
                  ? "border-destructive focus-visible:ring-destructive"
                  : orgDirty
                    ? "border-primary/50"
                    : ""
              }`}
              value={orgFieldValue}
              disabled={orgSaving}
              onChange={(e) => setOrgDraftValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void saveOrg();
                if (e.key === "Escape") revertOrgDraft();
              }}
            />
            {orgError ? (
              <p className="text-destructive text-right text-[11px]">
                {orgError}
              </p>
            ) : null}
          </div>
          <Button
            size="sm"
            variant="outline"
            className="w-[90px]"
            disabled={orgSaving || !orgDirty}
            onClick={() => saveOrg()}
          >
            {orgSaving ? (
              <Loader2 className="animate-spin" aria-label="Saving" />
            ) : (
              "Save"
            )}
          </Button>
        </div>
      </div>
      <p className="text-muted-foreground text-xs">
        Leave empty and save to clear the override — the org reverts to the
        deploy default.
      </p>
    </div>
  ) : null;

  if (members.length === 0) {
    return (
      <div className="space-y-3">
        {orgSection}
        <p className="text-muted-foreground text-sm">No members to show.</p>
      </div>
    );
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
  const dirtyMembers = members.filter(isDirty);

  const setDraft = (userId: string, value: string) => {
    setRowError(({ [userId]: _drop, ...rest }) => rest);
    setDrafts((d) => ({ ...d, [userId]: value }));
  };
  const revertDraft = (userId: string) => {
    setDrafts(({ [userId]: _drop, ...rest }) => rest);
    setRowError(({ [userId]: _drop, ...rest }) => rest);
  };

  async function saveAll() {
    if (saving || dirtyMembers.length === 0) return;

    const payloads = new Map<string, QuotaUpdate>();
    const errors: Record<string, string> = {};
    for (const member of dirtyMembers) {
      const result = buildPayload(draftValue(member));
      if (typeof result === "string") errors[member.user_id] = result;
      else payloads.set(member.user_id, result);
    }
    if (Object.keys(errors).length > 0) {
      setRowError((prev) => ({ ...prev, ...errors }));
      return;
    }
    setRowError({});

    setSaving(true);
    const results = await Promise.all(
      [...payloads.entries()].map(async ([id, payload]) => {
        const res = await fetch(`/api/quotas/${encodeURIComponent(id)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }).catch(() => null);
        if (res?.ok) return { id, error: null };
        const body: unknown = await res?.json().catch(() => null);
        const message =
          res?.status === 403
            ? "Admins only."
            : res?.status === 404
              ? "Member not found."
              : (errorMessage(body) ?? "Could not save limit.");
        return { id, error: message };
      })
    );
    setSaving(false);

    const failed: Record<string, string> = {};
    for (const { id, error: err } of results) {
      if (err) failed[id] = err;
    }
    setRowError(failed);
    setDrafts((d) =>
      Object.fromEntries(Object.entries(d).filter(([id]) => id in failed))
    );
    void mutate();
  }

  return (
    <div className="space-y-3">
      {orgSection}
      <div className="border-border overflow-hidden rounded-lg border">
        <Table className="table-fixed">
          <TableHeader>
            <TableRow className="bg-muted/40 hover:bg-muted/40">
              <TableHead className="h-9 text-xs">Member</TableHead>
              <TableHead className="h-9 w-24 text-xs">Role</TableHead>
              <TableHead className="h-9 w-44 text-right text-xs">
                Used (24h)
              </TableHead>
              <TableHead className="h-9 w-36 text-right text-xs">
                Limit (per 24h)
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.map((member) => {
              const id = member.user_id;
              const over =
                member.limit_usd > 0 && member.used_usd >= member.limit_usd;
              const usedFraction =
                member.limit_usd > 0
                  ? Math.min(1, member.used_usd / member.limit_usd)
                  : 0;
              const dirty = isDirty(member);
              const err = rowError[id];
              return (
                <TableRow key={id} className="border-border/70">
                  <TableCell className="py-2.5">
                    <div className="min-w-0">
                      <Link
                        href={`/admin/users/${encodeURIComponent(id)}`}
                        className="block truncate text-sm font-medium text-[#5d77a5] hover:underline dark:text-[#a8b8d2]"
                      >
                        {member.name || member.email}
                      </Link>
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
                  <TableCell className="py-2.5 text-right">
                    <div className="flex flex-col items-end gap-1.5">
                      <span
                        className={`font-mono text-xs ${
                          over ? "text-destructive font-medium" : ""
                        }`}
                      >
                        {formatDollars(member.used_usd)}
                        <span className="text-muted-foreground font-normal">
                          {" / "}
                          {formatDollars(member.limit_usd)}
                        </span>
                      </span>
                      <div className="bg-muted-foreground/15 h-1 w-24 overflow-hidden rounded-full">
                        <div
                          className={`h-full rounded-full ${
                            over
                              ? "bg-destructive"
                              : usedFraction >= 0.8
                                ? "bg-amber-500"
                                : "bg-primary/60"
                          }`}
                          style={{ width: `${usedFraction * 100}%` }}
                        />
                      </div>
                    </div>
                  </TableCell>
                  <TableCell className="py-2.5 text-right">
                    <div className="flex flex-col items-end gap-1">
                      <Input
                        type="number"
                        min={0}
                        step="0.01"
                        inputMode="decimal"
                        aria-label={`24-hour limit for ${memberLabel(member)}`}
                        aria-invalid={err ? true : undefined}
                        className={`h-8 w-28 text-right font-mono text-xs ${
                          err
                            ? "border-destructive focus-visible:ring-destructive"
                            : dirty
                              ? "border-primary/50"
                              : ""
                        }`}
                        value={draftValue(member)}
                        disabled={saving}
                        onChange={(e) => setDraft(id, e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") void saveAll();
                          if (e.key === "Escape") revertDraft(id);
                        }}
                      />
                      {err ? (
                        <p className="text-destructive text-right text-[11px]">
                          {err}
                        </p>
                      ) : null}
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
      <div className="flex items-center justify-between gap-3">
        <p className="text-muted-foreground text-xs">
          Leave a limit empty to revert that member to the workspace default.
        </p>
        <div className="flex shrink-0 items-center gap-3">
          {dirtyMembers.length > 0 && !saving ? (
            <span className="text-muted-foreground text-xs">
              {dirtyMembers.length} unsaved{" "}
              {dirtyMembers.length === 1 ? "change" : "changes"}
            </span>
          ) : null}
          <Button
            size="sm"
            className="w-[110px]"
            disabled={saving || dirtyMembers.length === 0}
            onClick={() => saveAll()}
          >
            {saving ? (
              <Loader2 className="animate-spin" aria-label="Saving" />
            ) : (
              "Save changes"
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
