"use client";

import { useState } from "react";
import useSWR from "swr";
import { Trophy } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import type { CostLeaderboardResponse } from "@/lib/types";
import { AnalyzerCostsPanel } from "../analyzers/reports-client";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

const WINDOWS = [
  { value: "1", label: "Last 24 hours" },
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
  { value: "90", label: "Last 90 days" },
  { value: "0", label: "All time" },
] as const;

export function CostLeaderboardPage() {
  const [windowDays, setWindowDays] = useState("7");
  const { data, error, isLoading } = useSWR<CostLeaderboardResponse>(
    `/api/leaderboard?window_days=${windowDays}&limit=100`,
    fetcher
  );

  return (
    <Tabs defaultValue="people" className="space-y-4">
      <TabsList>
        <TabsTrigger value="people">People</TabsTrigger>
        <TabsTrigger value="analyzers">Analyzers</TabsTrigger>
      </TabsList>
      <TabsContent value="people" className="mx-auto max-w-3xl">
        <Card className="border-[#6f88b4]/20 shadow-xs">
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle className="flex items-center gap-2 text-base">
              <Trophy className="h-4 w-4 text-[#85b85c]" />
              Cost Leaderboard
            </CardTitle>
            <Select value={windowDays} onValueChange={setWindowDays}>
              <SelectTrigger className="h-8 w-40 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {WINDOWS.map((window) => (
                  <SelectItem key={window.value} value={window.value}>
                    {window.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </CardHeader>
          <CardContent className="p-0">
            {isLoading ? (
              <div className="space-y-1 px-3 pb-3">
                {Array.from({ length: 8 }, (_, index) => (
                  <Skeleton key={index} className="h-11 w-full" />
                ))}
              </div>
            ) : error ? (
              <div className="text-destructive px-3 pb-3 text-sm">
                Unable to load leaderboard.
              </div>
            ) : data?.leaders.length ? (
              <ol className="divide-border/70 divide-y">
                {data.leaders.map((leader) => (
                  <li
                    key={`${leader.rank}-${leader.name}`}
                    className="grid grid-cols-[2.5rem_1fr_auto] items-center gap-3 px-4 py-3"
                  >
                    <span className="text-muted-foreground font-mono text-sm tabular-nums">
                      {leader.rank}
                    </span>
                    <span className="truncate text-sm font-medium">
                      {leader.name}
                    </span>
                    <span className="font-mono text-sm font-semibold tabular-nums">
                      {formatCostUsd(leader.cost_usd)}
                    </span>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="text-muted-foreground px-4 pb-4 text-sm">
                No spend in this window.
              </div>
            )}
          </CardContent>
        </Card>
      </TabsContent>
      <TabsContent value="analyzers">
        <AnalyzerCostsPanel />
      </TabsContent>
    </Tabs>
  );
}
