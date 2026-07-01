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
  const { data, error, isLoading } = useSWR<QuotaUsage>(
    "/api/quotas/me",
    fetcher
  );

  const used = data?.used_usd ?? 0;
  const limit = data?.limit_usd ?? 0;
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  const over = limit > 0 && used >= limit;

  return (
    <div className="border-border bg-muted/30 rounded-lg border p-4">
      <div className="flex items-center gap-2">
        <Gauge className="text-muted-foreground h-4 w-4" />
        <p className="text-foreground text-sm font-medium">Daily usage</p>
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
              today
            </p>
            <div className="bg-muted-foreground/20 h-2 w-full overflow-hidden rounded-full">
              <div
                className={
                  over
                    ? "bg-destructive h-full"
                    : "h-full bg-[color:var(--paper-running)]"
                }
                style={{ width: `${pct}%` }}
              />
            </div>
            {over ? (
              <p className="text-destructive text-xs">
                You&rsquo;ve reached today&rsquo;s limit. New billable runs are
                blocked until it resets.
              </p>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}
