"use client";

import type { DashboardResponse } from "@/lib/types";
import { AdminDashboard } from "../admin/page";

type UsageClientProps = {
  initialUsageData?: DashboardResponse | null;
};

export function UsageClient({ initialUsageData = null }: UsageClientProps) {
  return <AdminDashboard initialUsageData={initialUsageData} />;
}
