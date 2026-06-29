"use client";

import { useState } from "react";
import type { DashboardResponse } from "@/lib/types";
import { DASHBOARD_DEFAULT_USAGE_MINUTES } from "@/lib/dashboard-request";
import {
  getMinutesFromTimeRange,
  UsageOverviewCard,
  useDashboardUsage,
  type TimeRangeKey,
} from "@/components/usage-overview";

type UsageClientProps = {
  initialUsageData?: DashboardResponse | null;
};

export function UsageClient({ initialUsageData = null }: UsageClientProps) {
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

  return (
    <div className="space-y-4">
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
    </div>
  );
}
