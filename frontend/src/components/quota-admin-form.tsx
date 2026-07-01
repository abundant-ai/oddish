"use client";

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
import { fetcher } from "@/lib/api";
import type { QuotaList, QuotaMember } from "@/lib/types";

const formatDollars = (value: number) =>
  value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

const memberLabel = (member: QuotaMember) =>
  member.name || member.email || member.github_username || member.user_id;

// Read-only admin view of per-member daily quota usage. S4 extends this same
// file with editable `limit_usd` inputs + a Save flow (PUT /api/quotas/{id});
// keep the table/form shape so that upgrade is additive.
export function QuotaAdminForm() {
  const { data, error } = useSWR<QuotaList>("/api/quotas", fetcher);

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
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.map((member) => {
              const over =
                member.limit_usd > 0 && member.used_usd >= member.limit_usd;
              return (
                <TableRow key={member.user_id} className="border-border/70">
                  <TableCell className="py-2.5">
                    <div className="min-w-0">
                      <p className="text-foreground truncate text-sm font-medium">
                        {member.name ?? member.email}
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
                  <TableCell className="text-muted-foreground py-2.5 text-right font-mono text-xs">
                    {formatDollars(member.limit_usd)}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
      <p className="text-muted-foreground text-xs">
        Effective daily limit per member. Members without an explicit override
        show the workspace default.
      </p>
    </div>
  );
}
