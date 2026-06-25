"use client";

import { useEffect, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import type { DashboardResponse } from "@/lib/types";
import { DASHBOARD_DEFAULT_USAGE_MINUTES } from "@/lib/dashboard-request";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  getMinutesFromTimeRange,
  UsageOverviewCard,
  useDashboardUsage,
  type TimeRangeKey,
} from "@/components/usage-overview";
import { CostBreakdownCard } from "@/components/cost-breakdown-card";

type Tab = "queue-state" | "cost";

type UsageClientProps = {
  initialUsageData?: DashboardResponse | null;
  isAdmin?: boolean;
};

export function UsageClient({
  initialUsageData = null,
  isAdmin = false,
}: UsageClientProps) {
  const [timeRange, setTimeRange] = useState<TimeRangeKey>("24h");
  const usageMinutes = getMinutesFromTimeRange(timeRange);
  const usageFallbackData =
    usageMinutes === DASHBOARD_DEFAULT_USAGE_MINUTES ? initialUsageData : null;
  const {
    queues,
    modelUsage,
    jobUsage,
    error: usageError,
    isLoading: usageIsLoading,
    isRefreshing: usageIsRefreshing,
  } = useDashboardUsage(usageMinutes, usageFallbackData);

  // The tab is reflected in ?tab= so it's deep-linkable and survives
  // back/forward, but switching is purely client-side (like the admin tabs) —
  // we update the URL via the History API so there's no server round-trip and
  // the toggle responds instantly. Local state is the source of truth for the
  // active tab; an effect re-syncs it from the URL on deep-link / back-forward.
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const urlTab: Tab =
    isAdmin && searchParams.get("tab") === "cost" ? "cost" : "queue-state";
  const [tab, setTab] = useState<Tab>(urlTab);

  useEffect(() => {
    setTab(urlTab);
  }, [urlTab]);

  const handleTabChange = (value: string) => {
    const next = (value === "cost" ? "cost" : "queue-state") as Tab;
    setTab(next);
    const params = new URLSearchParams(searchParams.toString());
    if (next === "cost") params.set("tab", "cost");
    else params.delete("tab");
    const qs = params.toString();
    window.history.replaceState(null, "", qs ? `${pathname}?${qs}` : pathname);
  };

  const queueState = (
    <UsageOverviewCard
      queues={queues}
      modelUsage={modelUsage}
      jobUsage={jobUsage}
      error={usageError}
      isLoading={usageIsLoading}
      isRefreshing={usageIsRefreshing}
      timeRange={timeRange}
      onTimeRangeChange={setTimeRange}
    />
  );

  // Cost is admin-only; for everyone else show queue state without a tab strip.
  if (!isAdmin) {
    return <div className="space-y-4">{queueState}</div>;
  }

  return (
    <Tabs value={tab} onValueChange={handleTabChange} className="space-y-4">
      <TabsList>
        <TabsTrigger value="queue-state">Queue State</TabsTrigger>
        <TabsTrigger value="cost">Cost</TabsTrigger>
      </TabsList>
      <TabsContent value="queue-state" className="space-y-4">
        {queueState}
      </TabsContent>
      <TabsContent value="cost" className="space-y-4">
        {/* Client-fetched (no SSR); the card shows its own skeleton while the
            heavy /admin/costs query loads, and SWR caches it for revisits. */}
        <CostBreakdownCard
          showChart={false}
          enableSearch
          experimentLimit={500}
          userLimit={500}
        />
      </TabsContent>
    </Tabs>
  );
}
