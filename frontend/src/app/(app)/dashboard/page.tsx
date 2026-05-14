import { Suspense } from "react";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import {
  buildDashboardBackendParams,
  DEFAULT_DASHBOARD_REQUEST_PARAMS,
} from "@/lib/dashboard-request";
import type { DashboardResponse } from "@/lib/types";
import { DashboardClient } from "./dashboard-client";
import DashboardLoading from "./loading";

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

    const url = getBackendUrl(
      "dashboard",
      "",
      buildDashboardBackendParams(DEFAULT_DASHBOARD_REQUEST_PARAMS),
    );
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

async function DashboardData() {
  const initialDashboardData = await getInitialDashboardData();
  return <DashboardClient initialDashboardData={initialDashboardData} />;
}

export default function DashboardPage() {
  // Stream the skeleton immediately and resolve the hydrated client in a
  // later chunk so TTFB isn't blocked on the backend assembling the full
  // dashboard payload.
  return (
    <Suspense fallback={<DashboardLoading />}>
      <DashboardData />
    </Suspense>
  );
}
