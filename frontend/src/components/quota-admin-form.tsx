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

  const orgLimit = data.org_limit_usd ?? null;
  const orgUsed = data.org_used_usd ?? 0;
  const orgReserved = data.org_reserved_usd ?? 0;
  const orgOver = orgLimit !== null && orgUsed + orgReserved >= orgLimit;
  const orgFieldValue =
    orgDraft ?? (orgLimit !== null ? orgLimit.toFixed(2) : "");
  const orgDirty =
    orgDraft !== null &&
    (orgDraft === "" ? orgLimit !== null : Number(orgDraft) !== orgLimit);

  const setOrgDraftValue = (value: string) => {
    setOrgError(undefined);
    setOrgDraft(value);
  };

  async function saveOrg() {
    const raw = (orgDraft ?? "").trim();
    let payload: QuotaUpdate;
    if (raw === "") {
      payload = { limit_usd: null };
    } else {
      const rounded = Number(Number(raw).toFixed(2));
      if (!Number.isFinite(rounded) || rounded <= 0 || rounded > MAX_LIMIT_USD) {
        setOrgError("Enter an amount greater than 0, or leave empty to clear.");
        return;
      }
      payload = { limit_usd: rounded.toFixed(2) };
    }

    setOrgSaving(true);
    setOrgError(undefined);
    const res = await fetch("/api/quotas/org", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(() => null);
    setOrgSaving(false);

    if (!res || !res.ok) {
      const body: unknown = await res?.json().catch(() => null);
      setOrgError(
        res?.status === 403
          ? "Admins only."
          : (errorMessage(body) ?? "Could not save org limit.")
      );
      return;
    }

    setOrgDraft(null);
    void mutate();
  }

  const orgSection = (
    <div className="border-border bg-muted/30 space-y-3 rounded-lg border p-4">
      <div>
        <p className="text-foreground text-sm font-medium">Organization</p>
        <p className="text-muted-foreground text-xs">
          Aggregate daily cap across all members (unattributed spend counts too).
        </p>
      </div>
      <div className="flex flex-wrap items-end justify-between gap-3">
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
            {orgLimit !== null ? formatDollars(orgLimit) : "no cap"}
          </span>{" "}
          used today
          {orgReserved > 0 ? (
            <span className="text-muted-foreground">
              {" "}
              ({formatDollars(orgReserved)} reserved)
            </span>
          ) : null}
        </p>
        <div className="flex items-end gap-2">
          <div className="flex flex-col items-end gap-1">
            <Input
              type="number"
              min={0}
              step="0.01"
              inputMode="decimal"
              placeholder="No cap"
              className="h-8 w-28 text-right font-mono text-xs"
              value={orgFieldValue}
              onChange={(e) => setOrgDraftValue(e.target.value)}
            />
            {orgError ? (
              <p className="text-destructive text-[11px]">{orgError}</p>
            ) : null}
          </div>
          <Button
            size="sm"
            variant="outline"
            disabled={orgSaving || !orgDirty}
            onClick={saveOrg}
          >
            {orgSaving ? "Saving…" : "Save"}
          </Button>
        </div>
      </div>
    </div>
  );

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
      {orgSection}
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
