import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import type { DashboardResponse } from "@/lib/types";
import { DashboardClient } from "./dashboard-client";

async function getInitialDashboardData(): Promise<DashboardResponse | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) {
      return null;
    }

    const token = await getClerkToken(authObj.getToken);
    if (!token) {
      return null;
    }

    const url = getBackendUrl("dashboard", "", {
      include_tasks: "false",
      usage_minutes: "1440",
      experiments_limit: "25",
      experiments_offset: "0",
      experiments_status: "all",
    });
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!response.ok) {
      console.error(
        `[dashboard/page] Failed initial dashboard fetch: ${response.status}`,
      );
      return null;
    }
    return (await response.json()) as DashboardResponse;
  } catch (error) {
    console.error("[dashboard/page] Initial dashboard fetch failed", error);
    return null;
  }
}

export default async function DashboardPage() {
  const initialDashboardData = await getInitialDashboardData();
  return <DashboardClient initialDashboardData={initialDashboardData} />;
}
