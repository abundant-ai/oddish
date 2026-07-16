"use client";

import Link from "next/link";
import useSWR from "swr";
import { Trophy } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import type { CostLeaderboardResponse } from "@/lib/types";

function useCostLeaderboard(windowDays: number, limit: number) {
  return useSWR<CostLeaderboardResponse>(
    `/api/leaderboard?window_days=${windowDays}&limit=${limit}`,
    fetcher
  );
}

export function CostLeaderboardStrip() {
  const { data, isLoading } = useCostLeaderboard(7, 5);

  if (!isLoading && !data?.leaders.length) return null;

  return (
    <Card className="overflow-hidden border-[#6f88b4]/15 shadow-none">
      <CardContent className="flex min-w-0 items-center gap-3 p-2.5">
        <Link
          href="/leaderboard"
          className="text-muted-foreground hover:text-foreground flex shrink-0 items-center gap-1.5 text-xs font-medium transition-colors"
        >
          <Trophy className="h-3.5 w-3.5 text-[#85b85c]" />
          Top spenders · 7d
        </Link>
        <div className="divide-border/60 flex min-w-0 flex-1 flex-nowrap divide-x overflow-x-auto">
          {isLoading
            ? Array.from({ length: 5 }, (_, index) => (
                <div key={index} className="min-w-28 flex-1 px-3">
                  <Skeleton className="h-4 w-full" />
                </div>
              ))
            : data?.leaders.map((leader) => (
                <div
                  key={`${leader.rank}-${leader.name}`}
                  className="flex min-w-28 flex-1 items-baseline justify-between gap-2 px-3 text-xs"
                >
                  <span className="text-muted-foreground truncate">
                    {leader.rank}. {leader.name}
                  </span>
                  <span className="shrink-0 font-mono font-medium tabular-nums">
                    {formatCostUsd(leader.cost_usd)}
                  </span>
                </div>
              ))}
        </div>
      </CardContent>
    </Card>
  );
}
