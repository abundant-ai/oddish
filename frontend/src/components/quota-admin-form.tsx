"use client";

import { useState } from "react";
import Link from "next/link";
import useSWR from "swr";

import { Loader2, Zap } from "lucide-react";

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
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { fetcher } from "@/lib/api";
import type {
  QuotaBumpCreate,
  QuotaList,
  QuotaMember,
  QuotaUpdate,
} from "@/lib/types";

const formatDollars = (value: number) =>
  value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

const formatBumpExpiry = (iso: string) =>
  new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });

const memberLabel = (member: QuotaMember) =>
  member.name || member.email || member.github_username || member.user_id;

const MAX_LIMIT_USD = 99999999.9999;

const DEFAULT_BUMP_DURATION = "24";
const BUMP_DURATIONS = [
  { value: "6", label: "6 hours" },
  { value: "24", label: "24 hours" },
  { value: "48", label: "48 hours" },
  { value: "168", label: "7 days" },
];

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

async function requestQuotaError(
  res: Response | null,
  fallback: string
): Promise<string | null> {
  if (res?.ok) return null;
  if (res?.status === 403) return "Admins only.";
  if (res?.status === 404) return "Member not found.";
  const body: unknown = await res?.json().catch(() => null);
  return errorMessage(body) ?? fallback;
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
  const [bumpDraft, setBumpDraft] = useState<
    Record<string, { amount?: string; duration?: string }>
  >({});
  const [bumpBusy, setBumpBusy] = useState<Record<string, boolean>>({});
  const [bumpError, setBumpError] = useState<Record<string, string>>({});
  const [bumpOpen, setBumpOpen] = useState<Record<string, boolean>>({});
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

  const baseLimit = (member: QuotaMember) =>
    member.base_limit_usd ?? member.limit_usd - (member.bump_usd ?? 0);
  const draftValue = (member: QuotaMember) =>
    drafts[member.user_id] ?? baseLimit(member).toFixed(2);
  const isDirty = (member: QuotaMember) => {
    const draft = drafts[member.user_id];
    return (
      draft !== undefined &&
      (draft === "" ? true : Number(draft) !== baseLimit(member))
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
        return { id, error: await requestQuotaError(res, "Could not save limit.") };
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

  async function submitBump(
    id: string,
    init: RequestInit,
    fallback: string
  ): Promise<boolean> {
    if (bumpBusy[id]) return false;
    setBumpBusy((p) => ({ ...p, [id]: true }));
    setBumpError(({ [id]: _drop, ...rest }) => rest);
    const res = await fetch(
      `/api/quotas/${encodeURIComponent(id)}/bumps`,
      init
    ).catch(() => null);
    setBumpBusy((p) => ({ ...p, [id]: false }));

    const message = await requestQuotaError(res, fallback);
    if (message) {
      setBumpError((p) => ({ ...p, [id]: message }));
      return false;
    }
    void mutate();
    return true;
  }

  async function grantBump(member: QuotaMember) {
    const id = member.user_id;
    const rounded = Number(Number(bumpDraft[id]?.amount ?? "").toFixed(2));
    if (!Number.isFinite(rounded) || rounded <= 0 || rounded > MAX_LIMIT_USD) {
      setBumpError((p) => ({
        ...p,
        [id]: "Enter a boost amount greater than 0.",
      }));
      return;
    }
    const payload: QuotaBumpCreate = {
      amount_usd: rounded.toFixed(2),
      duration_hours: Number(bumpDraft[id]?.duration ?? DEFAULT_BUMP_DURATION),
    };
    const ok = await submitBump(
      id,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
      "Could not grant boost."
    );
    if (ok) {
      setBumpDraft(({ [id]: _drop, ...rest }) => rest);
      setBumpOpen((p) => ({ ...p, [id]: false }));
    }
  }

  const revokeBump = (member: QuotaMember) =>
    submitBump(member.user_id, { method: "DELETE" }, "Could not revoke boost.");

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
              <TableHead className="h-9 w-56 text-right text-xs">
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
              const busy = bumpBusy[id] ?? false;
              const bumpUsd = member.bump_usd ?? 0;
              return (
                <TableRow key={id} className="border-border/70">
                  <TableCell className="py-2.5 align-top">
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
                  <TableCell className="py-2.5 align-top">
                    <Badge variant="outline" className="text-[11px] capitalize">
                      {member.role.replace(/^org:/, "")}
                    </Badge>
                  </TableCell>
                  <TableCell className="py-2.5 text-right align-top">
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
                  <TableCell className="py-2.5 text-right align-top">
                    <div className="flex flex-col items-end gap-1">
                      <div className="flex items-center justify-end gap-1">
                        <Input
                          type="number"
                          min={0}
                          step="0.01"
                          inputMode="decimal"
                          aria-label={`24-hour base limit for ${memberLabel(member)}`}
                          aria-invalid={err ? true : undefined}
                          className={`h-8 w-24 text-right font-mono text-xs ${
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
                        <Popover
                          open={bumpOpen[id] ?? false}
                          onOpenChange={(open) =>
                            setBumpOpen((p) => ({ ...p, [id]: open }))
                          }
                        >
                          <PopoverTrigger asChild>
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              className="h-8 px-2 text-xs"
                              disabled={saving}
                            >
                              <Zap className="mr-1 size-3" />
                              Boost
                            </Button>
                          </PopoverTrigger>
                          <PopoverContent align="end" className="w-64 space-y-3">
                            <div className="space-y-0.5">
                              <p className="text-sm font-medium">
                                Temporary boost
                              </p>
                              <p className="text-muted-foreground text-xs">
                                Adds to {memberLabel(member)}&rsquo;s base limit
                                until it expires.
                              </p>
                            </div>
                            <div className="space-y-2">
                              <div className="space-y-1">
                                <Label className="text-xs">Amount (USD)</Label>
                                <Input
                                  type="number"
                                  min={0}
                                  step="0.01"
                                  inputMode="decimal"
                                  placeholder="50.00"
                                  className="h-8 text-right font-mono text-xs"
                                  value={bumpDraft[id]?.amount ?? ""}
                                  disabled={busy}
                                  onChange={(e) => {
                                    const value = e.target.value;
                                    setBumpError(
                                      ({ [id]: _drop, ...rest }) => rest
                                    );
                                    setBumpDraft((p) => ({
                                      ...p,
                                      [id]: { ...p[id], amount: value },
                                    }));
                                  }}
                                  onKeyDown={(e) => {
                                    if (e.key === "Enter") void grantBump(member);
                                  }}
                                />
                              </div>
                              <div className="space-y-1">
                                <Label className="text-xs">Duration</Label>
                                <Select
                                  value={bumpDraft[id]?.duration ?? DEFAULT_BUMP_DURATION}
                                  disabled={busy}
                                  onValueChange={(value) =>
                                    setBumpDraft((p) => ({
                                      ...p,
                                      [id]: { ...p[id], duration: value },
                                    }))
                                  }
                                >
                                  <SelectTrigger className="h-8 text-xs">
                                    <SelectValue />
                                  </SelectTrigger>
                                  <SelectContent>
                                    {BUMP_DURATIONS.map((d) => (
                                      <SelectItem
                                        key={d.value}
                                        value={d.value}
                                        className="text-xs"
                                      >
                                        {d.label}
                                      </SelectItem>
                                    ))}
                                  </SelectContent>
                                </Select>
                              </div>
                            </div>
                            {bumpError[id] ? (
                              <p className="text-destructive text-xs">
                                {bumpError[id]}
                              </p>
                            ) : null}
                            <Button
                              type="button"
                              size="sm"
                              className="w-full"
                              disabled={busy}
                              onClick={() => grantBump(member)}
                            >
                              {busy ? (
                                <Loader2
                                  className="animate-spin"
                                  aria-label="Saving"
                                />
                              ) : (
                                "Grant boost"
                              )}
                            </Button>
                          </PopoverContent>
                        </Popover>
                      </div>
                      {err ? (
                        <p className="text-destructive text-right text-[11px]">
                          {err}
                        </p>
                      ) : null}
                      {bumpUsd > 0 && member.bump_expires_at ? (
                        <div className="flex items-center justify-end gap-1.5">
                          <span className="text-[11px] text-emerald-600 dark:text-emerald-500">
                            +{formatDollars(bumpUsd)} until{" "}
                            {formatBumpExpiry(member.bump_expires_at)}
                          </span>
                          <button
                            type="button"
                            className="text-muted-foreground hover:text-destructive text-[11px] underline underline-offset-2 disabled:opacity-50"
                            disabled={busy}
                            onClick={() => revokeBump(member)}
                          >
                            Revoke
                          </button>
                        </div>
                      ) : null}
                      {!(bumpOpen[id] ?? false) && bumpError[id] ? (
                        <p className="text-destructive text-right text-[11px]">
                          {bumpError[id]}
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
