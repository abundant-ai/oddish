import { Suspense } from "react";
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

// Mirror the backend's resolve_role(): owner/admin (with or without the Clerk
// "org:" prefix) are admins. The backend's require_admin is still the real gate
// on /admin/costs; this only decides whether to render the Costing tab.
function isAdminRole(orgRole: string | null | undefined): boolean {
  const role = (orgRole ?? "").toLowerCase();
  return ["org:admin", "org:owner", "admin", "owner"].includes(role);
}

async function getInitialUsageData(
  token: string,
): Promise<DashboardResponse | null> {
  try {
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
  const authObj = await auth();
  const token = authObj?.userId ? await getClerkToken(authObj.getToken) : null;
  const isAdmin = isAdminRole(authObj?.orgRole);

  // Only Queue State is SSR'd. The Costing tab is admin-only and fetches its
  // (heavy, global) data client-side via SWR when opened, so a normal /usage
  // load is never blocked on it.
  const initialUsageData = token ? await getInitialUsageData(token) : null;

  return (
    <Suspense fallback={null}>
      <UsageClient initialUsageData={initialUsageData} isAdmin={isAdmin} />
    </Suspense>
  );
}
