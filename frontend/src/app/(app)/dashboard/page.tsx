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
  DASHBOARD_SSR_FETCH_TIMEOUT_MS,
  DEFAULT_DASHBOARD_REQUEST_PARAMS,
} from "@/lib/dashboard-request";
import { parseTaskSearch } from "@/lib/tag-query";
import type { DashboardResponse } from "@/lib/types";
import { DashboardClient } from "./dashboard-client";

type DashboardRequestParams = Parameters<typeof buildDashboardBackendParams>[0];

function firstParam(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

async function getInitialDashboardData(
  requestParams: DashboardRequestParams,
): Promise<DashboardResponse | null> {
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
      buildDashboardBackendParams(requestParams),
    );
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
      signal: AbortSignal.timeout(DASHBOARD_SSR_FETCH_TIMEOUT_MS),
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
  const params = (await searchParams) ?? {};

  const initialAuthor =
    firstParam(params.author) || DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR;
  const initialStatus = firstParam(params.status) || "all";
  const initialQuery = firstParam(params.q);
  const pageNumber = Math.max(
    1,
    Number.parseInt(firstParam(params.page), 10) || 1
  );
  const initialOffset = (pageNumber - 1) * DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT;

  // Mirror the client's query so the SSR fetch returns the same filtered experiments the client would request
  const parsedQuery = parseTaskSearch(initialQuery);
  const initialDashboardData = await getInitialDashboardData({
    ...DEFAULT_DASHBOARD_REQUEST_PARAMS,
    experiments_offset: initialOffset,
    experiments_author: initialAuthor,
    experiments_status: initialStatus,
    experiments_query: parsedQuery.text,
    experiments_tags: parsedQuery.all.join(","),
    experiments_tags_any: parsedQuery.any.join(","),
    experiments_tags_none: parsedQuery.none.join(","),
    experiments_author_query: parsedQuery.authors.join(","),
  });

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
