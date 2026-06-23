import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import {
  buildDashboardBackendParams,
  DASHBOARD_DEFAULT_USAGE_MINUTES,
} from "@/lib/dashboard-request";
import type { DashboardResponse } from "@/lib/types";
import { UsageClient } from "./usage-client";

async function getInitialUsageData(): Promise<DashboardResponse | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) {
      return null;
    }

    const token = await getClerkToken(authObj.getToken);
    if (!token) {
      return null;
    }

    const url = getBackendUrl(
      "dashboard",
      "",
      buildDashboardBackendParams({
        include_tasks: false,
        include_experiments: false,
        usage_minutes: DASHBOARD_DEFAULT_USAGE_MINUTES,
      })
    );
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
      signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) {
      console.error(
        `[usage/page] Failed initial usage fetch: ${response.status}`
      );
      return null;
    }
    return (await response.json()) as DashboardResponse;
  } catch (error) {
    console.error("[usage/page] Initial usage fetch failed", error);
    return null;
  }
}

export default async function UsagePage() {
  const initialUsageData = await getInitialUsageData();
  return <UsageClient initialUsageData={initialUsageData} />;
}
