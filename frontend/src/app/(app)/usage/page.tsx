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
import type { CostBreakdownResponse, DashboardResponse } from "@/lib/types";
import { UsageClient } from "./usage-client";

// Default window/limits used for the SSR cost fetch. These MUST match the
// CostBreakdownCard's initial SWR key (window "7", limit 500) so the fetched
// payload is used as the fallback rather than triggering a client refetch.
const COST_SSR_WINDOW_DAYS = "7";
const COST_SSR_LIMIT = "500";

// Mirror the backend's resolve_role(): owner/admin (with or without the Clerk
// "org:" prefix) are admins. The backend's require_admin is still the real
// gate on /admin/costs; this only decides whether to render the tab + SSR-fetch.
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

async function getInitialCostData(
  token: string,
): Promise<CostBreakdownResponse | null> {
  try {
    const url = getBackendUrl("admin/costs", "", {
      window_days: COST_SSR_WINDOW_DAYS,
      experiment_limit: COST_SSR_LIMIT,
      user_limit: COST_SSR_LIMIT,
    });
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!response.ok) {
      console.error(
        `[usage/page] Failed initial cost fetch: ${response.status}`
      );
      return null;
    }
    return (await response.json()) as CostBreakdownResponse;
  } catch (error) {
    console.error("[usage/page] Initial cost fetch failed", error);
    return null;
  }
}

export default async function UsagePage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const { tab } = await searchParams;
  const authObj = await auth();
  const token = authObj?.userId ? await getClerkToken(authObj.getToken) : null;
  const isAdmin = isAdminRole(authObj?.orgRole);

  // Only SSR-fetch the (heavy, global) cost breakdown when the Costing tab is
  // the active one — so a normal /usage load isn't blocked on it. Switching to
  // the tab changes ?tab=costing, which re-renders this server component and
  // triggers the fetch on demand.
  const wantCosting = isAdmin && tab === "costing";

  if (!token) {
    return (
      <Suspense fallback={null}>
        <UsageClient
          initialUsageData={null}
          isAdmin={false}
          initialCostData={null}
        />
      </Suspense>
    );
  }

  const [initialUsageData, initialCostData] = await Promise.all([
    getInitialUsageData(token),
    wantCosting ? getInitialCostData(token) : Promise.resolve(null),
  ]);

  return (
    <Suspense fallback={null}>
      <UsageClient
        initialUsageData={initialUsageData}
        isAdmin={isAdmin}
        initialCostData={initialCostData}
      />
    </Suspense>
  );
}
