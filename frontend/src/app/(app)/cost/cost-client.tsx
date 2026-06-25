"use client";

import { useState } from "react";
import { useCostBreakdown } from "@/lib/cost-breakdown";
import { CostBreakdownView } from "@/components/cost/cost-breakdown-view";
import {
  getBoundedMinutes,
  type TimeRangeKey,
} from "@/components/cost/time-range-picker";

export function CostClient() {
  const [timeRange, setTimeRange] = useState<TimeRangeKey>("24h");
  const usageMinutes = getBoundedMinutes(timeRange);
  const { rows, error, isLoading, isRefreshing } = useCostBreakdown(usageMinutes);

  return (
    <div className="space-y-4">
      <CostBreakdownView
        rows={rows}
        error={error}
        isLoading={isLoading}
        isRefreshing={isRefreshing}
        timeRange={timeRange}
        onTimeRangeChange={setTimeRange}
      />
    </div>
  );
}
