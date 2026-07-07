"use client";

import useSWR from "swr";
import { Wallet } from "lucide-react";
import { fetcher } from "@/lib/api";
import type { OrgQuotaUsage } from "@/lib/types";

const formatDollars = (value: number) =>
  value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

export function OrgBudgetCard() {
  const { data, error } = useSWR<OrgQuotaUsage>("/api/quotas/org", fetcher);

  // Only orgs with a configured monthly cap get this card. Render nothing
  // while loading, on error, or when there's no cap — no layout placeholder.
  if (error || !data || data.org_limit_usd == null) return null;

  const limit = data.org_limit_usd;
  const monthUsed = data.org_used_month_usd;
  const reserved = data.org_reserved_usd;
  const today = data.org_used_today_usd;
  const goal = data.daily_goal_usd ?? 0;

  const monthExhausted = monthUsed + reserved >= limit;
  const overGoal = goal > 0 && today > goal;
  const pct =
    goal > 0 ? Math.min(100, (today / goal) * 100) : today > 0 ? 100 : 0;

  const barColor = monthExhausted
    ? "bg-destructive h-full"
    : overGoal
      ? "h-full bg-[color:var(--paper-partial)]"
      : "h-full bg-[color:var(--paper-running)]";

  return (
    <div className="border-border bg-muted/30 rounded-lg border p-4">
      <div className="flex items-center gap-2">
        <Wallet className="text-muted-foreground h-4 w-4" />
        <p className="text-foreground text-sm font-medium">Org budget</p>
      </div>

      <div className="mt-3 space-y-2">
        <p className="text-foreground text-sm">
          Today: <span className="font-semibold">{formatDollars(today)}</span>{" "}
          of <span className="font-semibold">{formatDollars(goal)}</span> goal
        </p>
        <div className="bg-muted-foreground/20 h-2 w-full overflow-hidden rounded-full">
          <div className={barColor} style={{ width: `${pct}%` }} />
        </div>
        <p className="text-muted-foreground text-xs">
          Month: {formatDollars(monthUsed)} of {formatDollars(limit)}
          {reserved > 0 ? <> · {formatDollars(reserved)} reserved</> : null} ·
          resets on the 1st (UTC)
        </p>
        {monthExhausted ? (
          data.enforced ? (
            <p className="text-destructive text-xs">
              Org budget reached. New billable runs are blocked until the 1st
              (UTC).
            </p>
          ) : (
            <p className="text-muted-foreground text-xs">
              Org budget reached. Runs aren&rsquo;t blocked yet, but usage is
              tracked.
            </p>
          )
        ) : overGoal ? (
          <p className="text-muted-foreground text-xs">
            Ahead of pace — the daily goal adjusts as the month is spent.
          </p>
        ) : null}
      </div>
    </div>
  );
}
