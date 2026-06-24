import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import {
  buildDashboardBackendParams,
  DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR,
  DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT,
  DEFAULT_DASHBOARD_REQUEST_PARAMS,
} from "@/lib/dashboard-request";
import type { DashboardResponse } from "@/lib/types";
import { DashboardClient } from "./dashboard-client";

function firstParam(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

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
      signal: AbortSignal.timeout(5_000),
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

export default async function DashboardPage({
  searchParams,
}: {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  const initialDashboardData = await getInitialDashboardData();
  const params = (await searchParams) ?? {};

  const initialAuthor =
    firstParam(params.author) || DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR;
  const initialStatus = firstParam(params.status) || "all";
  const initialQuery = firstParam(params.q);
  const pageNumber = Math.max(
    1,
    Number.parseInt(firstParam(params.page), 10) || 1,
  );
  const initialOffset = (pageNumber - 1) * DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT;

  return (
    <DashboardClient
      initialDashboardData={initialDashboardData}
      initialAuthor={initialAuthor}
      initialStatus={initialStatus}
      initialQuery={initialQuery}
      initialOffset={initialOffset}
    />
  );
}
