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
import { parseTaskSearch } from "@/lib/tag-query";
import type { DashboardResponse } from "@/lib/types";
import { DashboardClient, type ExperimentsResult } from "./dashboard-client";

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

// Server-side, experiments-only fetch. Started (not awaited) in the page so the
// promise can stream to the client and resolve inside a Suspense boundary.
async function fetchExperiments(
  requestParams: DashboardRequestParams,
): Promise<ExperimentsResult> {
  const data = await getInitialDashboardData({
    ...requestParams,
    include_queues: false,
    include_usage: false,
    include_tasks: false,
  });
  if (!data) {
    return { experiments: [], hasMore: false, ok: false };
  }
  return {
    experiments: data.experiments ?? [],
    hasMore: data.experiments_has_more ?? false,
    ok: true,
  };
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

  // Mirror the client's query so the server fetch returns the same filtered
  // experiments. The promise is NOT awaited here — it streams to the client and
  // resolves inside the keyed <Suspense> boundary (skeleton while pending).
  const parsedQuery = parseTaskSearch(initialQuery);
  const experimentsPromise = fetchExperiments({
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
      experimentsPromise={experimentsPromise}
      initialAuthor={initialAuthor}
      initialStatus={initialStatus}
      initialQuery={initialQuery}
      initialOffset={initialOffset}
    />
  );
}
