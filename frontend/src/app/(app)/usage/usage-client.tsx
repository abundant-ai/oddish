"use client";

import { useState, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { DashboardResponse } from "@/lib/types";
import { DASHBOARD_DEFAULT_USAGE_MINUTES } from "@/lib/dashboard-request";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  getMinutesFromTimeRange,
  UsageOverviewCard,
  useDashboardUsage,
  type TimeRangeKey,
} from "@/components/usage-overview";

type UsageClientProps = {
  initialUsageData?: DashboardResponse | null;
  isAdmin?: boolean;
  // Server-rendered Costing panel (a <Suspense> boundary that streams in the
  // cost card), passed down so the heavy fetch stays on the server and the tab
  // switch shows a skeleton immediately.
  costingSlot?: ReactNode;
};

export function UsageClient({
  initialUsageData = null,
  isAdmin = false,
  costingSlot = null,
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

  // Tab is driven by the ?tab= query param so it's deep-linkable and survives
  // browser back/forward. Only "costing" is recognized (admins only); anything
  // else falls back to queue state.
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const activeTab =
    isAdmin && searchParams.get("tab") === "costing" ? "costing" : "queue-state";

  const handleTabChange = (value: string) => {
    const params = new URLSearchParams(searchParams.toString());
    if (value === "costing") params.set("tab", "costing");
    else params.delete("tab");
    const qs = params.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
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

  // Costing is admin-only; for everyone else show queue state without a lone tab.
  if (!isAdmin) {
    return <div className="space-y-4">{queueState}</div>;
  }

  return (
    <Tabs
      value={activeTab}
      onValueChange={handleTabChange}
      className="space-y-4"
    >
      <TabsList>
        <TabsTrigger value="queue-state">Queue State</TabsTrigger>
        <TabsTrigger value="costing">Costing</TabsTrigger>
      </TabsList>
      <TabsContent value="queue-state" className="space-y-4">
        {queueState}
      </TabsContent>
      <TabsContent value="costing" className="space-y-4">
        {costingSlot}
      </TabsContent>
    </Tabs>
  );
}
