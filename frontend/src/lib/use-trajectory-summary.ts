"use client";

import useSWR from "swr";
import useSWRMutation from "swr/mutation";
import type { TrajectorySummary } from "@/lib/types";

type ActiveRefreshStatus = "queued" | "running" | "retrying" | "settling";

export type TrajectorySummaryResource = {
  summary: TrajectorySummary | null;
  refresh:
    | null
    | {
        status: ActiveRefreshStatus;
        jobId: string;
        retryAfterMs: number;
      }
    | {
        status: "failed";
        jobId: string;
        detail: string;
      };
};

const activeRefreshStatuses = new Set<ActiveRefreshStatus>([
  "queued",
  "running",
  "retrying",
  "settling",
]);

export function parseTrajectorySummaryResponse(
  response: Pick<Response, "ok" | "status" | "statusText">,
  body: unknown
): TrajectorySummaryResource {
  if (response.status === 404) return { summary: null, refresh: null };
  if (
    body &&
    typeof body === "object" &&
    !Array.isArray(body) &&
    "refresh" in body
  ) {
    const summary = "summary" in body ? body.summary : undefined;
    const refresh = body.refresh;
    if (
      (summary !== null &&
        (typeof summary !== "object" || Array.isArray(summary))) ||
      (refresh !== null &&
        (typeof refresh !== "object" || Array.isArray(refresh)))
    ) {
      throw new Error("Malformed trajectory summary response");
    }
    if (refresh === null) {
      if (summary === undefined) {
        throw new Error("Malformed trajectory summary response");
      }
      return { summary: summary as TrajectorySummary | null, refresh: null };
    }

    const status = "status" in refresh ? refresh.status : null;
    const jobId = "job_id" in refresh ? refresh.job_id : null;
    if (typeof status !== "string" || typeof jobId !== "string" || !jobId) {
      throw new Error("Malformed trajectory summary refresh response");
    }
    if (status === "failed") {
      const detail = "detail" in refresh ? refresh.detail : null;
      if (typeof detail !== "string" || !detail) {
        throw new Error("Malformed trajectory summary refresh response");
      }
      return {
        summary: summary as TrajectorySummary | null,
        refresh: { status, jobId, detail },
      };
    }
    const retryAfterMs =
      "retry_after_ms" in refresh ? refresh.retry_after_ms : null;
    if (
      !activeRefreshStatuses.has(status as ActiveRefreshStatus) ||
      typeof retryAfterMs !== "number" ||
      !Number.isFinite(retryAfterMs) ||
      retryAfterMs <= 0
    ) {
      throw new Error("Malformed trajectory summary refresh response");
    }
    return {
      summary: summary as TrajectorySummary | null,
      refresh: {
        status: status as ActiveRefreshStatus,
        jobId,
        retryAfterMs,
      },
    };
  }
  if (!response.ok) {
    const error = new Error(
      body && typeof body === "object" && "detail" in body
        ? String(body.detail)
        : response.statusText || "Request failed"
    );
    (error as Error & { status?: number }).status = response.status;
    throw error;
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("Malformed trajectory summary response");
  }
  // Public share endpoints intentionally remain plain stored-column reads.
  return { summary: body as TrajectorySummary, refresh: null };
}

async function readTrajectorySummaryResponse(
  response: Response
): Promise<TrajectorySummaryResource> {
  const body = await response.json().catch(() => null);
  return parseTrajectorySummaryResponse(response, body);
}

export async function fetchTrajectorySummary(
  url: string
): Promise<TrajectorySummaryResource> {
  return readTrajectorySummaryResponse(
    await fetch(url, { credentials: "include" })
  );
}

interface UseTrajectorySummaryOptions {
  trialId: string;
  apiBaseUrl?: string;
  enabled?: boolean;
  canRegenerate?: boolean;
}

/** Owns the summary read, paid refresh event, cache transition, and polling. */
export function useTrajectorySummary({
  trialId,
  apiBaseUrl = "/api",
  enabled = true,
  canRegenerate = false,
}: UseTrajectorySummaryOptions) {
  const summaryUrl = enabled
    ? `${apiBaseUrl}/trials/${trialId}/trajectory/summary`
    : null;
  const summaryQuery = useSWR<TrajectorySummaryResource>(
    summaryUrl,
    fetchTrajectorySummary,
    {
      revalidateOnFocus: false,
      refreshInterval: (latest) => {
        const refresh = latest?.refresh;
        return refresh && refresh.status !== "failed"
          ? refresh.retryAfterMs
          : 0;
      },
    }
  );

  const refreshMutation = useSWRMutation<TrajectorySummaryResource>(
    canRegenerate ? summaryUrl : null,
    async (url: string) =>
      readTrajectorySummaryResponse(
        await fetch(url, { method: "POST", credentials: "include" })
      )
  );

  async function regenerate() {
    if (!summaryUrl || !canRegenerate) {
      throw new Error("Trajectory summary regeneration is not available");
    }
    const resource = await refreshMutation.trigger(undefined, {
      throwOnError: false,
    });
    if (!resource) return undefined;
    await summaryQuery.mutate(resource, { revalidate: false });
    return resource;
  }

  return {
    ...summaryQuery,
    regenerate,
    regenerationError: refreshMutation.error,
    isStartingRegeneration: refreshMutation.isMutating,
  };
}
