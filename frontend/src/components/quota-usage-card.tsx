"use client";

import useSWR from "swr";
import { Gauge } from "lucide-react";
import { fetcher } from "@/lib/api";
import type { QuotaUsage } from "@/lib/types";

const formatDollars = (value: number) =>
  value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

export function QuotaUsageCard() {
  const { data, error, isLoading } = useSWR<QuotaUsage>("/api/quotas/me", fetcher);

  const used = data?.used_usd ?? 0;
  const reserved = data?.reserved_usd ?? 0;
  const limit = data?.limit_usd ?? 0;
  const bump = data?.bump_usd ?? 0;
  const total = used + reserved;
  const pct = limit > 0 ? Math.min(100, (total / limit) * 100) : data ? 100 : 0;
  const over = limit <= 0 || total >= limit;
  const blocked = over && data?.enforced === true;

  return (
    <div className="border-border bg-muted/30 rounded-lg border p-4">
      <div className="flex items-center gap-2">
        <Gauge className="text-muted-foreground h-4 w-4" />
        <p className="text-foreground text-sm font-medium">Usage (last 24h)</p>
      </div>

      <div className="mt-3">
        {error ? (
          <p className="text-muted-foreground text-sm">Could not load usage.</p>
        ) : isLoading || !data ? (
          <p className="text-muted-foreground text-sm">Loading usage…</p>
        ) : (
          <div className="space-y-2">
            <p className="text-foreground text-sm">
              <span className="font-semibold">{formatDollars(used)}</span> of{" "}
              <span className="font-semibold">{formatDollars(limit)}</span> used
              in the last 24h
              {reserved > 0 ? (
                <span className="text-muted-foreground">
                  {" "}
                  ({formatDollars(reserved)} reserved)
                </span>
              ) : null}
            </p>
            {bump > 0 && data.bump_expires_at ? (
              <p className="text-muted-foreground text-xs">
                Includes a temporary +{formatDollars(bump)} boost until{" "}
                {new Date(data.bump_expires_at).toLocaleString()}
              </p>
            ) : null}
            <div className="bg-muted-foreground/20 h-2 w-full overflow-hidden rounded-full">
              <div
                className={
                  blocked
                    ? "bg-destructive h-full"
                    : "h-full bg-[color:var(--paper-running)]"
                }
                style={{ width: `${pct}%` }}
              />
            </div>
            {blocked ? (
              <p className="text-destructive text-xs">
                You&rsquo;ve reached your 24-hour limit. New billable runs are
                blocked until older spend ages out of the window.
              </p>
            ) : over ? (
              <p className="text-muted-foreground text-xs">
                You&rsquo;re over your 24-hour budget. Runs aren&rsquo;t blocked
                yet, but usage is being tracked.
              </p>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}
